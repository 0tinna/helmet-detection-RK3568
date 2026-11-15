"""Exercise the real decoder, runtime, saving and status through injected NPU IO."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]


class Engine:
    def __init__(self, delay=0, fail=False, empty=False):
        self.delay = delay
        self.fail = fail
        self.empty = empty
        self.in_use = False
        self.released = False
        self.released_while_busy = False

    def infer(self, rgb):
        self.in_use = True
        try:
            time.sleep(self.delay)
            if self.fail:
                raise RuntimeError('injected NPU failure')
            outputs = [np.full((1, 21, size, size), -20, np.float32)
                       for size in (4, 2, 1)]
            if not self.empty:
                outputs[0][0, :7, 1, 1] = [0, 0, 0, 0, 12, 12, -12]
            return outputs
        finally:
            self.in_use = False

    def release(self):
        self.released_while_busy = self.in_use
        self.released = True


class CountingSource:
    """A capture frame encodes its sequence number in an untouched corner."""
    def __init__(self, outage=False):
        self.counter = 0
        self.started = time.monotonic()
        self.outage = outage

    def read(self, stop_event=None):
        if stop_event is not None and stop_event.wait(0.006):
            return False, None
        elapsed = time.monotonic() - self.started
        if self.outage and 0.05 <= elapsed < 0.12:
            return False, None
        self.counter += 1
        return True, np.full((40, 60, 3), self.counter % 256, np.uint8)

    def release(self):
        pass


class RuntimeTests(unittest.TestCase):
    def runtime(self):
        try:
            return importlib.import_module('helmet.pipeline_runtime')
        except ImportError:
            self.fail('packet-bound reusable runtime has not been implemented')

    def args(self, folder, **overrides):
        from helmet.helmet_realtime import build_parser
        args = build_parser().parse_args([
            '--source', folder, '--model', 'helmet_32x32.rknn',
            '--no-display', '--duration', '0.25', '--replay-fps', '80',
        ])
        for name, value in overrides.items():
            setattr(args, name, value)
        return args

    def image(self, folder):
        cv2.imwrite(str(Path(folder) / 'a.png'), np.full((40, 60, 3), 37, np.uint8))

    def test_help_needs_no_rknn_package(self):
        result = subprocess.run([sys.executable, str(ROOT / 'helmet' / 'helmet_realtime.py'),
                                 '--help'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--source', result.stdout)

    def test_results_keep_original_frame_and_distinct_counters(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            outputs = []
            engine = Engine(delay=0.05)
            pipeline = runtime.Pipeline(self.args(folder, skip=1, replay_fps=200, duration=0.35), engine=engine,
                                        output_callback=outputs.append)
            summary = pipeline.run()
            self.assertGreater(len(outputs), 1)
            ids = [item.frame_id for item in outputs]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(all(item.original_bgr[0, 0, 0] == 37 for item in outputs))
            self.assertTrue(all(item.metadata['frame_id'] == item.frame_id for item in outputs))
            counters = summary['counters']
            self.assertGreater(counters['overwritten'], 0)
            self.assertGreater(counters['active_skip'], 0)
            self.assertEqual(counters['consumed'], counters['detected'] + counters['active_skip'])
            self.assertEqual(counters['captured'], counters['consumed'] + counters['overwritten']
                             + counters['pending_capture'])
            self.assertEqual(counters['displayed'], 0)
            self.assertTrue(engine.released)
            self.assertFalse(engine.released_while_busy)

    def test_worker_failure_is_raised_and_reported(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            engine = Engine(fail=True)
            pipeline = runtime.Pipeline(self.args(folder), engine=engine)
            with self.assertRaisesRegex(runtime.PipelineError, 'injected NPU failure'):
                pipeline.run()
            self.assertEqual(pipeline.status_snapshot()['state'], 'fault')
            self.assertTrue(engine.released)

    def test_capture_exception_and_output_callback_exception_propagate(self):
        runtime = self.runtime()
        class BrokenSource:
            def read(self, stop_event=None):
                raise OSError('capture device failure')
            def release(self):
                pass
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            pipeline = runtime.Pipeline(self.args(folder), engine=Engine(), source=BrokenSource())
            with self.assertRaisesRegex(runtime.PipelineError, 'capture device failure'):
                pipeline.run()
            self.assertEqual(pipeline.status_snapshot()['state'], 'fault')
            def broken_sink(packet):
                raise OSError('output sink failure')
            pipeline = runtime.Pipeline(self.args(folder), engine=Engine(), output_callback=broken_sink)
            with self.assertRaisesRegex(runtime.PipelineError, 'output sink failure'):
                pipeline.run()
            self.assertEqual(pipeline.status_snapshot()['state'], 'fault')

    def test_status_sink_cannot_reintroduce_a_healthy_frame_after_source_interruption(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            outputs = []
            def event_sink(event):
                if event['type'] == 'state' and event['state'] == 'healthy':
                    time.sleep(0.07)
            runtime.Pipeline(self.args(folder), engine=Engine(), source=CountingSource(outage=True),
                             output_callback=outputs.append, event_callback=event_sink).run()
            self.assertTrue(outputs)
            self.assertFalse(outputs[0].metadata['valid'])

    def test_snapshot_encoding_failure_faults_and_suppresses_alarm(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            pipeline = runtime.Pipeline(self.args(folder, save_dir=str(Path(folder) / 'events'),
                                                   alarm_dwell=0), engine=Engine())
            with patch.object(cv2, 'imencode', return_value=(False, None)):
                with self.assertRaisesRegex(runtime.PipelineError, 'encoding failed'):
                    pipeline.run()
            self.assertEqual(pipeline.status_snapshot()['state'], 'fault')
            self.assertEqual(pipeline.status_snapshot()['alarm_state'], 'unknown')

    def test_older_result_is_never_drawn_on_a_newer_capture(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            outputs = []
            runtime.Pipeline(self.args(folder), engine=Engine(delay=0.025),
                             source=CountingSource(), output_callback=outputs.append).run()
            self.assertGreater(len(outputs), 2)
            self.assertTrue(all(int(item.original_bgr[-1, -1, 0]) == item.frame_id % 256
                                for item in outputs))

    def test_source_outage_emits_degraded_then_recovers(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            states = []
            pipeline = runtime.Pipeline(self.args(folder), engine=Engine(),
                                        source=CountingSource(outage=True),
                                        event_callback=states.append)
            summary = pipeline.run()
            state_names = [item['state'] for item in states if item['type'] == 'state']
            self.assertIn('degraded', state_names)
            degraded = state_names.index('degraded')
            self.assertIn('healthy', state_names[degraded + 1:])
            self.assertGreater(summary['counters']['read_failure'], 0)

    def test_slow_results_cannot_be_presented_as_normal_or_alarm(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            outputs = []
            pipeline = runtime.Pipeline(self.args(folder, result_ttl=0.01),
                                        engine=Engine(delay=0.03), output_callback=outputs.append)
            pipeline.run()
            self.assertTrue(outputs)
            self.assertTrue(all(item.state == 'degraded' for item in outputs))
            self.assertTrue(all(item.alarm_state == 'unknown' for item in outputs))
            self.assertTrue(all(not item.metadata['valid'] for item in outputs))

    def test_jsonl_session_and_unique_event_periodic_snapshots(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            save = str(Path(folder) / 'events')
            log = str(Path(folder) / 'frames.jsonl')
            session = str(Path(folder) / 'session.json')
            pipeline = runtime.Pipeline(self.args(folder, save_dir=save, save_interval=0.05,
                                                   jsonl=log, session_json=session,
                                                   alarm_dwell=0), engine=Engine())
            summary = pipeline.run()
            rows = [json.loads(line) for line in Path(log).read_text().splitlines()
                    if json.loads(line).get('record_type') == 'output']
            self.assertGreater(len(rows), 2)
            self.assertGreaterEqual(rows[0]['capture_to_output_ms'], rows[0]['infer_ms'])
            self.assertIn('prep_ms', rows[0])
            self.assertIn('p99', summary['full_session']['capture_to_output_ms'])
            self.assertIn('sliding_window', summary)
            self.assertIn('cpu_percent', summary['runtime_samples'][0])
            self.assertEqual(json.loads(Path(session).read_text())['counters'], summary['counters'])
            metadata = [json.loads(path.read_text()) for path in Path(save).glob('*.json')]
            self.assertTrue(any(row['reason'] == 'alarm' for row in metadata))
            self.assertTrue(any(row['reason'] == 'periodic' for row in metadata))
            self.assertEqual(len(metadata), len(list(Path(save).glob('*.jpg'))))
            self.assertTrue(all(row['model'] == 'helmet_32x32.rknn' for row in metadata))

    def test_full_session_inference_timings_include_results_overwritten_by_slow_output(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            log = str(Path(folder) / 'frames.jsonl')
            def slow_output(packet):
                time.sleep(0.04)
            pipeline = runtime.Pipeline(self.args(folder, replay_fps=200, jsonl=log),
                                        engine=Engine(), output_callback=slow_output)
            summary = pipeline.run()
            self.assertGreater(summary['counters']['output_overwritten'], 0)
            self.assertEqual(summary['full_session']['infer_ms']['count'], summary['counters']['detected'])
            self.assertEqual(summary['sliding_window']['timings']['infer_ms']['count'],
                             summary['counters']['detected'])
            rows = [json.loads(line) for line in Path(log).read_text().splitlines()]
            self.assertEqual(len([row for row in rows if row.get('record_type') == 'detection']),
                             summary['counters']['detected'])

    def test_missing_or_wrong_model_heads_are_fault_unknown_without_healthy_outputs(self):
        runtime = self.runtime()
        good = [np.zeros((1, 21, size, size), np.float32) for size in (4, 2, 1)]
        broken = good[0].copy()
        broken[0, 0, 0, 0] = np.nan
        for heads in ([], good[:1], [broken, *good[1:]],
                      [np.zeros((1, 21, 8, 8), np.float32), *good[1:]]):
            with self.subTest(shapes=[row.shape for row in heads]), tempfile.TemporaryDirectory() as folder:
                self.image(folder)
                class InvalidEngine(Engine):
                    def infer(self, image):
                        return heads
                engine = InvalidEngine()
                events, outputs = [], []
                pipeline = runtime.Pipeline(self.args(folder), engine=engine, event_callback=events.append,
                                            output_callback=outputs.append)
                with self.assertRaises(runtime.PipelineError):
                    pipeline.run()
                self.assertTrue(engine.released)
                self.assertEqual(pipeline.telemetry.counters['detected'], 0)
                self.assertEqual(pipeline.telemetry.counters['detection_failure'], 1)
                self.assertEqual(outputs, [])
                self.assertFalse(any(event.get('state') in ('healthy', 'no-detection') for event in events))
                self.assertTrue(any(event.get('state') == 'fault' and event.get('valid') is False
                                    and event.get('alarm_state') == 'unknown' for event in events))

    def test_expiry_is_rechecked_after_slow_state_sink_before_frame_delivery(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            outputs = []
            def event_sink(event):
                if event['type'] == 'state' and event['state'] == 'healthy':
                    time.sleep(0.04)
            runtime.Pipeline(self.args(folder, result_ttl=0.03), engine=Engine(),
                             output_callback=outputs.append, event_callback=event_sink).run()
            self.assertTrue(outputs)
            self.assertFalse(outputs[0].metadata['valid'])
            self.assertEqual(outputs[0].alarm_state, 'unknown')

    def test_status_snapshot_expires_while_an_output_sink_is_busy(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            snapshots = []
            def busy_output(packet):
                time.sleep(0.04)
                snapshots.append(pipeline.status_snapshot())
            pipeline = runtime.Pipeline(self.args(folder, result_ttl=0.03), engine=Engine(),
                                        output_callback=busy_output)
            pipeline.run()
            self.assertTrue(snapshots)
            self.assertTrue(all(row['state'] == 'degraded' and not row['valid']
                                and row['alarm_state'] == 'unknown' for row in snapshots))

    def test_sink_completion_latency_is_recorded_and_expired_sink_clears_preview(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            outputs, events = [], []
            def slow_sink(packet):
                outputs.append(packet)
                # Drive the declared expired boundary using the same monotonic
                # clock as production; Windows sleep may return before a fixed
                # millisecond assertion due to clock granularity.
                deadline = time.monotonic() + 0.05
                while time.monotonic() < deadline:
                    time.sleep(0.005)
            pipeline = runtime.Pipeline(self.args(folder, result_ttl=0.03, alarm_dwell=0),
                                        engine=Engine(), output_callback=slow_sink,
                                        event_callback=events.append)
            summary = pipeline.run()
            self.assertTrue(outputs)
            first = outputs[0].metadata
            self.assertGreaterEqual(first.get('sink_ms', 0), 35)
            self.assertGreaterEqual(first['capture_to_sink_complete_ms'], first['capture_to_output_ms'] + 35)
            self.assertFalse(first['valid_at_sink_complete'])
            self.assertGreaterEqual(first['callback_finished_at'], outputs[0].output_at)
            self.assertEqual(summary['full_session']['sink_ms']['count'], summary['counters']['output'])
            self.assertTrue(any(event['type'] == 'state' and event['state'] == 'degraded' for event in events))
            self.assertFalse(any(event['type'] == 'alarm' for event in events))

    def test_expired_sink_output_has_no_display_detections_or_periodic_artifacts(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            shown, packets = [], []
            def slow_sink(packet):
                packets.append(packet)
                time.sleep(0.055)
            pipeline = runtime.Pipeline(self.args(folder, result_ttl=0.025, duration=0.14,
                                                   no_display=False, save_interval=0.001,
                                                   save_dir=str(Path(folder) / 'events')),
                                        engine=Engine(), source=CountingSource(), output_callback=slow_sink)
            with patch.object(cv2, 'imshow', side_effect=lambda title, image: shown.append(image.copy())), \
                    patch.object(cv2, 'waitKey', return_value=0), patch.object(cv2, 'destroyAllWindows'):
                pipeline.run()
            self.assertTrue(shown)
            self.assertFalse(any(np.any((image[:, :, 2] > 220) & (image[:, :, 1] < 80)
                                        & (image[:, :, 0] < 80)) for image in shown),
                             'expired display images must contain no red detection boxes/banner')
            self.assertEqual(list(Path(folder, 'events').glob('*.jpg')), [])
            self.assertEqual(list(Path(folder, 'events').glob('*.json')), [])
            self.assertTrue(all(not packet.metadata['valid'] for packet in packets))

    def test_expiry_during_alarm_event_suppresses_later_audio_and_snapshot(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            audio_requests, shown, packets = [], [], []
            class Audio:
                def __init__(self, **kwargs):
                    pass
                def trigger(self):
                    audio_requests.append(time.monotonic())
                def poll(self):
                    pass
                def close(self):
                    pass
            def slow_event(event):
                if event['type'] == 'alarm':
                    time.sleep(0.055)
            pipeline = runtime.Pipeline(self.args(folder, result_ttl=0.025, duration=0.12,
                                                   alarm_dwell=0, alarm_beep=True, no_display=False,
                                                   save_dir=str(Path(folder) / 'events')),
                                        engine=Engine(), event_callback=slow_event, output_callback=packets.append)
            with patch.object(runtime, 'AudioAlarm', Audio), \
                    patch.object(cv2, 'imshow', side_effect=lambda title, image: shown.append(
                        (time.monotonic() - packets[-1].captured_at, image.copy()))), \
                    patch.object(cv2, 'waitKey', return_value=0), patch.object(cv2, 'destroyAllWindows'):
                pipeline.run()
            self.assertEqual(audio_requests, [])
            self.assertEqual(list(Path(folder, 'events').glob('*.jpg')), [])
            stale_images = [image for age, image in shown if age > 0.025]
            self.assertTrue(stale_images)
            self.assertFalse(any(np.any((image[:, :, 2] > 220) & (image[:, :, 1] < 80)
                                        & (image[:, :, 0] < 80)) for image in stale_images))

    def test_snapshot_expiring_during_jpeg_encoding_is_not_committed(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            original_encode = cv2.imencode
            def slow_encode(*args, **kwargs):
                time.sleep(0.055)
                return original_encode(*args, **kwargs)
            pipeline = runtime.Pipeline(self.args(folder, result_ttl=0.025, duration=0.12,
                                                   alarm_dwell=0, save_dir=str(Path(folder) / 'events')),
                                        engine=Engine())
            with patch.object(cv2, 'imencode', side_effect=slow_encode):
                pipeline.run()
            self.assertEqual(list(Path(folder, 'events').glob('*.jpg')), [])
            self.assertEqual(list(Path(folder, 'events').glob('*.json')), [])

    def test_real_video_reconnect_resets_alarm_dwell(self):
        runtime = self.runtime()
        from helmet.sources import VideoSource
        class Capture:
            def __init__(self, fails=False):
                self.fails, self.count = fails, 0
            def isOpened(self):
                return True
            def release(self):
                pass
            def read(self):
                self.count += 1
                if self.fails and self.count == 2:
                    time.sleep(0.085)
                    return False, None
                time.sleep(0.004)
                return True, np.full((40, 60, 3), self.count, np.uint8)
        with tempfile.TemporaryDirectory() as folder, patch.object(VideoSource, '_open', side_effect=[Capture(True), Capture()]):
            events, packets = [], []
            pipeline = runtime.Pipeline(self.args(folder, duration=0.2, alarm_dwell=0.04),
                                        engine=Engine(), source=VideoSource('0'),
                                        output_callback=packets.append, event_callback=events.append)
            summary = pipeline.run()
            self.assertGreater(summary['counters']['read_failure'], 0)
            self.assertGreater(pipeline.source_epoch, 0)
            self.assertIn('degraded', [event['state'] for event in events if event['type'] == 'state'])
            recovered = next(packet for packet in packets if packet.frame_id == 2)
            self.assertEqual(recovered.alarm_state, 'pending', 'dwell must start after the reconnect')

    def test_real_video_reconnect_rejects_inflight_pre_outage_result(self):
        runtime = self.runtime()
        from helmet.sources import VideoSource
        started, observed_failure = threading.Event(), threading.Event()
        class Capture:
            def __init__(self, fails=False):
                self.fails, self.count = fails, 0
            def isOpened(self):
                return True
            def release(self):
                pass
            def read(self):
                self.count += 1
                if self.fails and self.count == 2:
                    started.wait(1)
                    return False, None
                time.sleep(0.03 if not self.fails else 0.004)
                return True, np.full((40, 60, 3), self.count, np.uint8)
        class WaitingEngine(Engine):
            def infer(self, image):
                if not started.is_set():
                    started.set()
                    observed_failure.wait(0.2)
                return super().infer(image)
        def events(event):
            if event['type'] == 'state' and event['state'] == 'degraded':
                observed_failure.set()
        with tempfile.TemporaryDirectory() as folder, patch.object(VideoSource, '_open', side_effect=[Capture(True), Capture()]):
            packets = []
            pipeline = runtime.Pipeline(self.args(folder, duration=0.3), engine=WaitingEngine(),
                                        source=VideoSource('0'), event_callback=events,
                                        output_callback=packets.append)
            pipeline.run()
            first = next(packet for packet in packets if packet.frame_id == 1)
            self.assertFalse(first.metadata['valid'])
            self.assertEqual(first.alarm_state, 'unknown')

    def test_genuine_no_frame_source_respects_duration_without_npu_inference(self):
        runtime = self.runtime()
        class NoFrames:
            def read(self, stop_event=None):
                stop_event.wait(0.005)
                return False, None
            def release(self):
                pass
        with tempfile.TemporaryDirectory() as folder:
            engine = Engine(fail=True)
            pipeline = runtime.Pipeline(self.args(folder, duration=0.08), engine=engine, source=NoFrames())
            started = time.monotonic()
            summary = pipeline.run()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(summary['state'], 'stop')
            self.assertEqual(summary['counters']['captured'], 0)
            self.assertEqual(summary['counters']['detected'], 0)
            self.assertGreater(summary['counters']['read_failure'], 0)
            self.assertTrue(engine.released)

    def test_gui_teardown_failure_still_finalizes_fault_session(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            session = str(Path(folder) / 'session.json')
            engine, events = Engine(), []
            pipeline = runtime.Pipeline(self.args(folder, duration=0.08, no_display=False, session_json=session),
                                        engine=engine, event_callback=events.append)
            with patch.object(cv2, 'imshow'), patch.object(cv2, 'waitKey', return_value=0), \
                    patch.object(cv2, 'destroyAllWindows', side_effect=cv2.error('window close failed')):
                with self.assertRaisesRegex(Exception, 'window close failed') as raised:
                    pipeline.run()
            self.assertIsInstance(raised.exception, runtime.PipelineError)
            self.assertTrue(engine.released)
            self.assertEqual(json.loads(Path(session).read_text())['state'], 'fault')
            self.assertEqual([event for event in events if event['type'] == 'state'][-1]['state'], 'fault')

    def test_session_write_failure_emits_final_fault_event(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            events = []
            occupied = Path(folder) / 'existing_directory'
            occupied.mkdir()
            pipeline = runtime.Pipeline(self.args(folder, duration=0.08, session_json=str(occupied)),
                                        engine=Engine(), event_callback=events.append)
            with self.assertRaisesRegex(runtime.PipelineError, 'telemetry save'):
                pipeline.run()
            self.assertEqual([event for event in events if event['type'] == 'state'][-1]['state'], 'fault')

    def test_keyboard_pause_can_resume_when_no_new_result_is_available(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            outputs, states = [], []
            keys = iter([ord('p'), ord('p')])
            def keyboard(delay):
                return next(keys, 0)
            pipeline = runtime.Pipeline(self.args(folder, no_display=False), engine=Engine(),
                                        output_callback=outputs.append, event_callback=states.append)
            with patch.object(cv2, 'imshow'), patch.object(cv2, 'waitKey', keyboard), patch.object(cv2, 'destroyAllWindows'):
                pipeline.run()
            self.assertGreater(len(outputs), 2)
            names = [row['state'] for row in states if row['type'] == 'state']
            self.assertIn('healthy', names[names.index('degraded') + 1:])

    def test_runtime_rss_is_measured_on_supported_local_host(self):
        from helmet.telemetry import RuntimeSampler
        self.assertGreater(RuntimeSampler().sample()['rss_bytes'] or 0, 0)

    def test_output_save_failure_propagates(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            target = Path(folder) / 'occupied'
            target.write_text('file')
            pipeline = runtime.Pipeline(self.args(folder, save_dir=str(target), alarm_dwell=0),
                                        engine=Engine())
            with self.assertRaises(runtime.PipelineError):
                pipeline.run()

    def test_shutdown_does_not_release_an_engine_while_inference_is_running(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            engine = Engine(delay=0.25)
            pipeline = runtime.Pipeline(self.args(folder, duration=0.06, shutdown_timeout=0.02),
                                        engine=engine)
            started = time.monotonic()
            with self.assertRaisesRegex(runtime.PipelineError, 'shutdown'):
                pipeline.run()
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertFalse(engine.released_while_busy)
            pipeline.detect_thread.join(1)
            self.assertTrue(engine.released)

    def test_empty_results_are_no_detection_and_unknown_alarm(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            outputs = []
            runtime.Pipeline(self.args(folder), engine=Engine(empty=True),
                             output_callback=outputs.append).run()
            self.assertTrue(outputs)
            self.assertTrue(all(item.state == 'no-detection' for item in outputs))
            self.assertTrue(all(item.alarm_state == 'unknown' for item in outputs))

    def test_invalid_runtime_arguments_fail_before_engine_construction(self):
        runtime = self.runtime()
        with tempfile.TemporaryDirectory() as folder:
            self.image(folder)
            for override in ({'skip': -1}, {'duration': -1}, {'conf': 1.1},
                             {'result_ttl': 0}, {'replay_fps': float('nan')},
                             {'save_interval': 2}):
                with self.subTest(override=override), self.assertRaises(ValueError):
                    runtime.Pipeline(self.args(folder, **override), engine=Engine())


class AlarmTests(unittest.TestCase):
    def alarm(self):
        try:
            module = importlib.import_module('helmet.alarm')
        except ImportError:
            self.fail('distinct-detection alarm state machine has not been implemented')
        return module.AlarmStateMachine(dwell=1, cooldown=3, clear_interval=1, max_gap=2)

    def test_dwell_requires_distinct_frames_and_empty_never_means_safe(self):
        alarm = self.alarm()
        self.assertFalse(alarm.update(1, 0, danger=True, valid=True, has_detections=True))
        self.assertFalse(alarm.update(1, 2, danger=True, valid=True, has_detections=True))
        self.assertTrue(alarm.update(2, 1.1, danger=True, valid=True, has_detections=True))
        self.assertEqual(alarm.state, 'active')
        alarm.update(3, 1.2, danger=False, valid=True, has_detections=False)
        self.assertEqual(alarm.state, 'unknown')

    def test_clear_interval_cooldown_and_outage_reset_dwell(self):
        alarm = self.alarm()
        alarm.update(1, 0, True, True, True)
        self.assertTrue(alarm.update(2, 1, True, True, True))
        alarm.update(3, 1.2, False, True, True)
        self.assertEqual(alarm.state, 'active')
        alarm.update(4, 2.3, False, True, True)
        self.assertEqual(alarm.state, 'clear')
        alarm.update(5, 2.4, True, True, True)
        self.assertFalse(alarm.update(6, 3.5, True, True, True))
        self.assertTrue(alarm.update(7, 4.1, True, True, True))
        alarm.invalidate()
        alarm.update(8, 4.2, True, True, True)
        self.assertEqual(alarm.state, 'pending')

    def test_audio_nonzero_exit_is_an_event_and_process_is_bounded(self):
        try:
            module = importlib.import_module('helmet.alarm')
        except ImportError:
            self.fail('bounded audio runner has not been implemented')
        events = []
        audio = module.AudioAlarm([sys.executable, '-c', 'raise SystemExit(4)'],
                                  timeout=0.1, event_callback=events.append)
        audio.trigger()
        time.sleep(0.15)
        audio.poll()
        self.assertTrue(any(item['type'] == 'audio_failure' for item in events))
        audio.close()
        events.clear()
        audio = module.AudioAlarm([sys.executable, '-c', 'import time; time.sleep(10)'],
                                  timeout=0.03, event_callback=events.append)
        audio.trigger()
        time.sleep(0.06)
        audio.poll()
        self.assertTrue(any(item['type'] == 'audio_failure' for item in events))
        audio.close()


if __name__ == '__main__':
    unittest.main()
