"""Real HTTP and encoded replay clips; no camera or RKNN required."""
import http.client
import importlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import shutil
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def packet(frame_id, stamp, valid=True):
    frame = np.full((96, 128, 3), frame_id * 20, np.uint8)
    return SimpleNamespace(
        frame_id=frame_id, original_bgr=frame, annotated_bgr=frame,
        captured_at=stamp, processed_at=stamp, output_at=stamp,
        metadata={'frame_id': frame_id, 'valid': valid, 'state': 'healthy',
                  'alarm_state': 'clear', 'model': 'test.rknn'},
    )


class OutputTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('output_sinks')
        except ImportError:
            self.fail('reusable WebPreview and EventClipRecorder are missing')

    def get(self, server, route):
        conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=2)
        conn.request('GET', route)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def test_http_publishes_actual_jpeg_and_status(self):
        server = self.module.WebPreview(host='127.0.0.1', port=0)
        try:
            server.on_packet(packet(3, time.monotonic()))
            status, body = self.get(server, '/frame.jpg')
            self.assertEqual(status, 200)
            image = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(image.shape, (96, 128, 3))
            self.assertAlmostEqual(float(image.mean()), 60, delta=2)
            status, body = self.get(server, '/status.json')
            self.assertEqual(json.loads(body)['frame_id'], 3)
            self.assertTrue(json.loads(body)['valid'])
            self.assertEqual(self.get(server, '/unknown')[0], 404)
        finally:
            server.close()

    def test_fault_clears_old_healthy_frame_and_alarm(self):
        server = self.module.WebPreview(host='127.0.0.1', port=0)
        try:
            server.on_packet(packet(3, time.monotonic()))
            server.on_event({'type': 'state', 'state': 'fault', 'valid': False,
                             'alarm_state': 'unknown', 'reason': 'source lost'})
            _, body = self.get(server, '/status.json')
            self.assertFalse(json.loads(body)['valid'])
            self.assertEqual(json.loads(body)['state'], 'fault')
            self.assertEqual(self.get(server, '/frame.jpg')[0], 503)
        finally:
            server.close()

    def test_live_status_provider_expires_stored_frame(self):
        live = {'state': 'healthy', 'valid': True, 'counters': {'detected': 8}}
        server = self.module.WebPreview(host='127.0.0.1', port=0,
                                        status_provider=lambda: dict(live))
        try:
            server.on_packet(packet(3, time.monotonic()))
            live.update(state='degraded', valid=False, alarm_state='unknown')
            self.assertEqual(self.get(server, '/frame.jpg')[0], 503)
            _, body = self.get(server, '/status.json')
            self.assertEqual(json.loads(body)['counters']['detected'], 8)
            self.assertFalse(json.loads(body)['valid'])
        finally:
            server.close()

    def test_local_deadline_expires_jpeg_without_state_callback(self):
        server = self.module.WebPreview(host='127.0.0.1', port=0, result_ttl=.02)
        try:
            server.on_packet(packet(3, time.monotonic() - .1))
            self.assertEqual(self.get(server, '/frame.jpg')[0], 503)
        finally:
            server.close()

    def test_fresh_standalone_web_frame_expires_later(self):
        server = self.module.WebPreview(host='127.0.0.1', port=0, result_ttl=.02)
        try:
            server.on_packet(packet(3, time.monotonic()))
            time.sleep(.05)
            self.assertEqual(self.get(server, '/frame.jpg')[0], 503)
            _, body = self.get(server, '/status.json')
            self.assertFalse(json.loads(body)['valid'])
        finally:
            server.close()

    @unittest.skipUnless(shutil.which('node'), 'Node required to execute actual page JavaScript')
    def test_page_reconnects_stream_after_fault_recovery(self):
        import subprocess
        script = self.module.PAGE.decode().split('<script>')[1].split('</script>')[0]
        harness = """
const image={style:{},_src:'/stream.mjpg',set src(v){this._src=v;this.assignments++;},get src(){return this._src;},assignments:0};
const state={};const stats={};let callback;let index=0;
global.document={querySelector:s=>s==='#preview'?image:s==='#state'?state:stats};
global.setTimeout=f=>{callback=f};
global.fetch=async()=>({json:async()=>[{state:'healthy',valid:true},{state:'degraded',valid:false},{state:'healthy',valid:true}][index++]});
global.Date={now:()=>100};
SCRIPT
setImmediate(async()=>{await new Promise(r=>setImmediate(r));await callback();await callback();console.log(JSON.stringify({assignments:image.assignments,src:image.src}));});
""".replace('SCRIPT', script)
        result = subprocess.run(['node', '-e', harness], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(result.stdout)
        self.assertGreater(state['assignments'], 0)
        self.assertIn('/stream.mjpg?', state['src'])

    @unittest.skipUnless(shutil.which('node'), 'Node required to execute actual page JavaScript')
    def test_page_hides_pending_and_failed_reconnected_image(self):
        import subprocess
        script = self.module.PAGE.decode().split('<script>')[1].split('</script>')[0]
        harness = """
const image={style:{},src:'/stream.mjpg'};const state={};const stats={};let callback;let index=0;
global.document={querySelector:s=>s==='#preview'?image:s==='#state'?state:stats};
global.setTimeout=f=>{callback=f};global.Date={now:()=>100};
global.fetch=async()=>({json:async()=>[{state:'healthy',valid:true},{state:'degraded',valid:false},{state:'healthy',valid:true}][index++]});
SCRIPT
setImmediate(async()=>{await new Promise(r=>setImmediate(r));await callback();await callback();
const pending=image.style.visibility;image.onerror();console.log(JSON.stringify({pending,error:image.style.visibility}));});
""".replace('SCRIPT', script)
        result = subprocess.run(['node', '-e', harness], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertEqual(values['pending'], 'hidden')
        self.assertEqual(values['error'], 'hidden')

    def test_header_failure_releases_stream_client_slot(self):
        server = self.module.WebPreview(host='127.0.0.1', port=0, max_clients=1)
        try:
            with patch.object(BaseHTTPRequestHandler, 'end_headers', side_effect=BrokenPipeError):
                conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=2)
                conn.request('GET', '/stream.mjpg')
                with self.assertRaises(http.client.RemoteDisconnected):
                    conn.getresponse()
                conn.close()
            self.assertTrue(server._stream_slots.acquire(blocking=False))
            server._stream_slots.release()
        finally:
            server.close()

    def test_expiry_preserves_live_fault_reason(self):
        live = {'state': 'fault', 'valid': False, 'reason': 'engine failed'}
        server = self.module.WebPreview(host='127.0.0.1', port=0,
                                        status_provider=lambda: live, result_ttl=.01)
        try:
            server.on_packet(packet(1, time.monotonic() - 1))
            _, body = self.get(server, '/status.json')
            self.assertEqual(json.loads(body)['state'], 'fault')
            self.assertEqual(json.loads(body)['reason'], 'engine failed')
        finally:
            server.close()

    def test_clip_queue_must_have_positive_finite_capacity(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                self.module.EventClipRecorder(folder, max_pending=0)

    def test_stream_disconnect_does_not_block_shutdown(self):
        server = self.module.WebPreview(host='127.0.0.1', port=0)
        conn = http.client.HTTPConnection('127.0.0.1', server.port, timeout=2)
        server.on_packet(packet(2, time.monotonic()))
        conn.request('GET', '/stream.mjpg')
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b'Content-Type: image/jpeg', response.read(80))
        conn.close()
        start = time.monotonic()
        server.close()
        self.assertLess(time.monotonic() - start, 2)

    def test_event_clip_has_pre_post_coverage_and_is_playable(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = self.module.EventClipRecorder(folder, pre_seconds=2,
                                                     post_seconds=2, fps=2)
            for i in range(7):
                p = packet(i, i / 2)
                p.metadata['expires_at'] = p.captured_at + .5
                recorder.on_packet(p)
            recorder.on_event({'type': 'alarm', 'state': 'healthy',
                               'monotonic': 3.0, 'frame_id': 6,
                               'metadata': {'valid': True, 'scores': [0.9]}})
            for i in range(7, 11):
                p = packet(i, i / 2)
                p.metadata['expires_at'] = p.captured_at + .5
                recorder.on_packet(p)
            recorder.close()
            clips = list(Path(folder).glob('*.avi'))
            self.assertEqual(len(clips), 1)
            self.assertIn('_conf0900_', clips[0].name)
            metadata = json.loads(clips[0].with_suffix('.json').read_text())
            self.assertEqual(metadata['requested_start'], 1.0)
            self.assertEqual(metadata['requested_end'], 5.0)
            self.assertTrue(metadata['pre_complete'])
            self.assertTrue(metadata['post_complete'])
            self.assertEqual(metadata['encoded_frames'], 8)
            cap = cv2.VideoCapture(str(clips[0]))
            try:
                self.assertTrue(cap.isOpened())
                decoded = 0
                while cap.read()[0]:
                    decoded += 1
                self.assertEqual(decoded, 8)
            finally:
                cap.release()

    def test_clip_marks_early_stop_and_rejects_stale_alarm(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = self.module.EventClipRecorder(folder, pre_seconds=5,
                                                     post_seconds=5, fps=2)
            recorder.on_packet(packet(1, 1.0))
            recorder.on_event({'type': 'alarm', 'monotonic': 1.0,
                               'metadata': {'valid': False}})
            recorder.close()
            self.assertEqual(list(Path(folder).glob('*.avi')), [])
        with tempfile.TemporaryDirectory() as folder:
            recorder = self.module.EventClipRecorder(folder, pre_seconds=5,
                                                     post_seconds=5, fps=2)
            recorder.on_packet(packet(1, 1.0))
            recorder.on_event({'type': 'alarm', 'monotonic': 1.0,
                               'metadata': {'valid': True}})
            recorder.on_packet(packet(2, 2.0))
            recorder.close()
            record = json.loads(next(Path(folder).glob('*.json')).read_text())
            self.assertFalse(record['pre_complete'])
            self.assertFalse(record['post_complete'])
            self.assertEqual(record['finish_reason'], 'shutdown')

    def test_clip_outage_gap_is_masked_and_not_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = self.module.EventClipRecorder(folder, pre_seconds=2,
                                                     post_seconds=2, fps=2)
            recorder.on_packet(packet(1, 0.0))
            recorder.on_packet(packet(2, 3.0))
            recorder.on_event({'type': 'alarm', 'monotonic': 3.0,
                               'metadata': {'valid': True}})
            recorder.on_event({'type': 'state', 'state': 'degraded',
                               'valid': False, 'monotonic': 3.1})
            recorder.on_packet(packet(3, 20.0))
            recorder.close()
            record = json.loads(next(Path(folder).glob('*.json')).read_text())
            self.assertFalse(record['pre_complete'])
            self.assertFalse(record['post_complete'])
            self.assertGreater(record['masked_frames'], 0)
            self.assertIn(None, record['encoded_source_frame_ids'])

    def test_clip_finalizes_after_deadline_without_new_packets(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = self.module.EventClipRecorder(folder, pre_seconds=.01,
                                                     post_seconds=.03, fps=2)
            now = time.monotonic()
            recorder.on_packet(packet(1, now))
            recorder.on_event({'type': 'alarm', 'monotonic': now,
                               'metadata': {'valid': True}})
            recorder.on_event({'type': 'state', 'state': 'degraded',
                               'valid': False, 'monotonic': now + .001})
            deadline = time.monotonic() + 1
            while not list(Path(folder).glob('*.json')) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(len(list(Path(folder).glob('*.json'))), 1)
            record = json.loads(next(Path(folder).glob('*.json')).read_text())
            self.assertFalse(record['post_complete'])
            recorder.close()

    def test_clip_bounded_jitter_coverage_uses_actual_result_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = self.module.EventClipRecorder(folder, pre_seconds=.5,
                                                     post_seconds=.5, fps=10)
            for i in range(16):
                p = packet(i, i * .16)
                p.metadata['expires_at'] = p.captured_at + .5
                recorder.on_packet(p)
                if i == 6:
                    recorder.on_event({'type': 'alarm', 'monotonic': .96,
                                       'metadata': {'valid': True, 'scores': [.9]}})
            recorder.close()
            result = json.loads(next(Path(folder).glob('*.json')).read_text())
            self.assertTrue(result['pre_complete'])
            self.assertTrue(result['post_complete'])
            self.assertEqual(result['masked_frames'], 0)

    def test_clip_subframe_fault_in_pre_window_is_incomplete(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = self.module.EventClipRecorder(folder, pre_seconds=.3,
                                                     post_seconds=.3, fps=10)
            for i in range(7):
                p = packet(i, i / 10)
                p.metadata['expires_at'] = p.captured_at + .5
                recorder.on_packet(p)
                if i == 0:
                    recorder.on_event({'type': 'state', 'valid': False, 'monotonic': .03})
                    recorder.on_event({'type': 'state', 'valid': True, 'monotonic': .04})
                if i == 3:
                    recorder.on_event({'type': 'alarm', 'monotonic': .3,
                                       'metadata': {'valid': True}})
            recorder.close()
            result = json.loads(next(Path(folder).glob('*.json')).read_text())
            self.assertFalse(result['pre_complete'])

    def test_clip_future_packet_cannot_fill_last_interval_gap(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = self.module.EventClipRecorder(folder, pre_seconds=0,
                                                     post_seconds=.3, fps=10)
            for i, stamp in enumerate([0, .1, .2, .32]):
                p = packet(i, stamp)
                p.metadata['expires_at'] = stamp + .02
                recorder.on_packet(p)
                if i == 0:
                    recorder.on_event({'type': 'alarm', 'monotonic': 0,
                                       'metadata': {'valid': True}})
            recorder.close()
            result = json.loads(next(Path(folder).glob('*.json')).read_text())
            self.assertFalse(result['post_complete'])


if __name__ == '__main__':
    unittest.main()
