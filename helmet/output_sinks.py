"""Optional source-independent HTTP preview and bounded event clip recording.

Both sinks consume Pipeline OutputPacket objects and JSON-safe state events.
The preview never serves its previous healthy JPEG after an invalid state.
Clips are resampled on output timestamps and disclose missing pre/post coverage.
"""
from collections import deque
import json
import math
from pathlib import Path
import queue
import select
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import uuid

import cv2
import numpy as np


PAGE = b'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Helmet detection</title><style>
body{background:#101820;color:#edf5fa;font:16px system-ui;margin:24px;max-width:1100px}
img{width:100%;max-height:75vh;object-fit:contain;background:#18232d}
pre{white-space:pre-wrap} .fault{color:#ffbd6c}
</style><h1>Helmet detection</h1><p id="state">Waiting for detection</p>
<img id="preview" src="/stream.mjpg" alt="Detection preview"><pre id="stats"></pre>
<script>let previewAvailable=false;let streamFailed=false;let imageReady=false;let lastReconnect=-Infinity;
const preview=document.querySelector('#preview');
preview.onerror=()=>{streamFailed=true;imageReady=false;preview.style.visibility='hidden';};
preview.onload=()=>{imageReady=true;preview.style.visibility=previewAvailable?'visible':'hidden';};
async function poll(){try{let r=await fetch('/status.json',{cache:'no-store'});
let s=await r.json();document.querySelector('#state').textContent=
(s.state||'startup')+(s.reason?' - '+s.reason:'');
document.querySelector('#state').className=s.valid?'':'fault';
if(s.valid&&(!previewAvailable||(streamFailed&&Date.now()-lastReconnect>=1000))){
imageReady=false;preview.style.visibility='hidden';preview.src='/stream.mjpg?reconnect='+Date.now();
lastReconnect=Date.now();streamFailed=false;}
previewAvailable=!!s.valid;preview.style.visibility=s.valid&&imageReady?'visible':'hidden';
document.querySelector('#stats').textContent=JSON.stringify(s,null,2);
}catch(e){document.querySelector('#state').textContent='Connection unavailable';
preview.style.visibility='hidden';previewAvailable=false;imageReady=false;}setTimeout(poll,500)}poll();
</script></html>'''


class WebPreview:
    def __init__(self, host='127.0.0.1', port=8080, jpeg_quality=80,
                 status_provider=None, result_ttl=1.0, max_clients=4):
        if not math.isfinite(result_ttl) or result_ttl <= 0 or not isinstance(max_clients, int) or max_clients <= 0:
            raise ValueError('positive TTL and client capacity required')
        self._condition = threading.Condition()
        self._jpeg = None
        self._generation = 0
        self._closed = False
        self._quality = jpeg_quality
        self._provider = status_provider
        self._ttl = result_ttl
        self._stream_slots = threading.BoundedSemaphore(max_clients)
        self._alarm_count = 0
        self._status = {'state': 'startup', 'valid': False,
                        'alarm_state': 'unknown', 'frame_id': None}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_bytes(self, code, content_type, body):
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                route = self.path.split('?', 1)[0]
                try:
                    if route == '/':
                        self.send_bytes(200, 'text/html; charset=utf-8', PAGE)
                    elif route == '/status.json':
                        status, _ = owner._snapshot()
                        body = json.dumps(status, allow_nan=False).encode()
                        self.send_bytes(200, 'application/json', body)
                    elif route == '/frame.jpg':
                        _, jpeg = owner._snapshot()
                        self.send_bytes(200 if jpeg else 503,
                                        'image/jpeg' if jpeg else 'text/plain',
                                        jpeg or b'Detection unavailable')
                    elif route == '/stream.mjpg':
                        if not owner._stream_slots.acquire(blocking=False):
                            self.send_bytes(503, 'text/plain', b'Preview client limit reached')
                            return
                        generation = -1
                        last_data = time.monotonic()
                        try:
                            self.connection.settimeout(2)
                            self.send_response(200)
                            self.send_header('Content-Type',
                                             'multipart/x-mixed-replace; boundary=frame')
                            self.send_header('Cache-Control', 'no-store')
                            self.end_headers()
                            while True:
                                with owner._condition:
                                    changed = owner._condition.wait_for(
                                        lambda: owner._closed or
                                        owner._generation != generation, timeout=.1)
                                    if owner._closed:
                                        return
                                    current_generation = owner._generation
                                readable, _, _ = select.select([self.connection], [], [], 0)
                                if readable and not self.connection.recv(1, 2):
                                    return
                                _, jpeg = owner._snapshot()
                                if jpeg is None or not changed:
                                    if time.monotonic() - last_data > 2:
                                        return
                                    generation = current_generation
                                    continue
                                generation = current_generation
                                self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n'
                                                 b'Content-Length: ' + str(len(jpeg)).encode()
                                                 + b'\r\n\r\n' + jpeg + b'\r\n')
                                self.wfile.flush()
                                last_data = time.monotonic()
                        finally:
                            owner._stream_slots.release()
                    else:
                        self.send_bytes(404, 'text/plain', b'Not found')
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={'poll_interval': 0.1}, daemon=True)
        self._thread.start()

    def _snapshot(self):
        # Call outside the lock: the provider may itself acquire pipeline locks.
        live = self._provider() if self._provider is not None else {}
        with self._condition:
            status = dict(self._status)
            status.update(live)
            captured = status.get('captured_at')
            if status.get('valid') and captured is not None and time.monotonic() - captured > self._ttl:
                status.update(valid=False, state='degraded', alarm_state='unknown',
                              reason='result expired')
            jpeg = self._jpeg if status.get('valid', False) else None
            status['alarm_count'] = self._alarm_count
            if jpeg is None:
                for field in ('boxes', 'classes', 'scores'):
                    status.pop(field, None)
            return status, jpeg

    def on_packet(self, packet):
        metadata = dict(packet.metadata)
        metadata['captured_at'] = packet.captured_at
        valid = bool(metadata.get('valid', False))
        jpeg = None
        if valid:
            ok, encoded = cv2.imencode('.jpg', packet.annotated_bgr,
                                      [cv2.IMWRITE_JPEG_QUALITY, self._quality])
            if not ok:
                raise OSError('preview JPEG encoding failed')
            jpeg = encoded.tobytes()
        live = self._provider() if self._provider is not None else {}
        valid = valid and live.get('valid', True) and time.monotonic() - packet.captured_at <= self._ttl
        if not valid:
            metadata.update(valid=False, state=live.get('state', 'degraded'),
                            alarm_state='unknown')
            jpeg = None
        with self._condition:
            if self._closed:
                raise RuntimeError('preview already closed')
            self._status = metadata
            self._jpeg = jpeg
            self._generation += 1
            self._condition.notify_all()

    def on_event(self, event):
        if event.get('type') == 'alarm':
            with self._condition:
                self._alarm_count += 1
            return
        if event.get('type') != 'state':
            return
        with self._condition:
            self._status.update(event)
            if not event.get('valid', False):
                self._jpeg = None
                for field in ('boxes', 'classes', 'scores'):
                    self._status.pop(field, None)
            self._generation += 1
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


class EventClipRecorder:
    """Compressed pre-event ring; finite queued encodes on a worker thread.

    encoder='opencv' writes software MJPEG AVI. encoder='mpp' sends the same
    sampled BGR frames through GStreamer mpph264enc and writes H.264 MP4.
    No claim about hardware availability is made until the actual encode passes.
    """
    def __init__(self, directory, pre_seconds=5, post_seconds=5, fps=10,
                 encoder='opencv', max_pending=2):
        if pre_seconds < 0 or post_seconds <= 0 or fps <= 0:
            raise ValueError('clip pre>=0, post>0 and fps>0 required')
        if not all(math.isfinite(v) for v in (pre_seconds, post_seconds, fps)):
            raise ValueError('finite clip durations and fps required')
        if encoder not in ('opencv', 'mpp'):
            raise ValueError('unknown clip encoder')
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending <= 0:
            raise ValueError('positive integer clip queue capacity required')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.pre, self.post, self.fps = pre_seconds, post_seconds, fps
        self.encoder = encoder
        self._ring = deque()
        self._active = []
        self._jobs = queue.Queue(maxsize=max_pending)
        self._errors = []
        self._closed = False
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._state_events = deque()
        self._max_hold = max(1 / fps, .1)
        self._process = None
        self._worker = threading.Thread(target=self._encode_loop, daemon=True)
        self._worker.start()
        self._timer = threading.Thread(target=self._deadline_loop, daemon=True)
        self._timer.start()

    def _check(self):
        if self._closed:
            raise RuntimeError('clip recorder closed')
        if self._errors:
            raise RuntimeError('clip encoding failed: ' + self._errors[0])

    def on_packet(self, packet):
        with self._lock:
            self._on_packet(packet)

    def _on_packet(self, packet):
        self._check()
        stamp = float(packet.output_at)
        if self._ring and stamp < self._ring[-1]['time']:
            raise ValueError('clip packet timestamps must be monotonic')
        ok, jpeg = cv2.imencode('.jpg', packet.annotated_bgr,
                               [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise OSError('clip buffer JPEG failed')
        item = {'time': stamp, 'frame_id': packet.frame_id,
                'valid': bool(packet.metadata.get('valid', False)),
                'expires_at': min(float(packet.metadata.get('expires_at', stamp + self._max_hold)),
                                  stamp + max(self._max_hold, .5)),
                'jpeg': jpeg.tobytes()}
        self._ring.append(item)
        cutoff = stamp - self.pre
        while len(self._ring) > 1 and self._ring[1]['time'] < cutoff:
            self._ring.popleft()
        for clip in list(self._active):
            clip['frames'].append(item)
            if stamp >= clip['requested_end']:
                self._submit(clip, 'complete')
                self._active.remove(clip)

    def on_event(self, event):
        with self._lock:
            self._on_event(event)

    def _on_event(self, event):
        self._check()
        if event.get('type') == 'state':
            self._state_events.append(dict(event))
            while len(self._state_events) > 1000:
                self._state_events.popleft()
            return
        if event.get('type') != 'alarm' or not event.get('metadata', {}).get('valid'):
            return
        if len(self._active) >= self._jobs.maxsize:
            raise RuntimeError('too many concurrent event clips')
        stamp = float(event['monotonic'])
        self._active.append({
            'event_id': uuid.uuid4().hex, 'event': event,
            'requested_start': stamp - self.pre,
            'requested_end': stamp + self.post,
            'frames': list(self._ring),
            'state_events': list(self._state_events),
            'deadline': time.monotonic() + self.post,
        })

    def _deadline_loop(self):
        while not self._stopping.wait(.02):
            with self._lock:
                for clip in list(self._active):
                    if time.monotonic() >= clip['deadline']:
                        try:
                            self._submit(clip, 'deadline')
                        except Exception as error:
                            self._errors.append(str(error))
                        self._active.remove(clip)

    def _submit(self, clip, reason):
        clip['finish_reason'] = reason
        clip['state_events'] = list(self._state_events)
        try:
            self._jobs.put_nowait(clip)
        except queue.Full:
            raise RuntimeError('event clip encode queue is full')

    def _encode_loop(self):
        while True:
            clip = self._jobs.get()
            try:
                if clip is None:
                    return
                self._encode(clip)
            except Exception as error:
                self._errors.append(str(error))
            finally:
                self._jobs.task_done()

    def _encode(self, clip):
        frames = clip.pop('frames')
        clip.pop('deadline', None)
        if not frames:
            raise ValueError('no output frames available for event clip')
        start = clip['requested_start']
        end = clip['requested_end']
        count = max(1, int(round(max(end - start, 0) * self.fps)))
        suffix = '.mp4' if self.encoder == 'mpp' else '.avi'
        scores = clip['event'].get('metadata', {}).get('scores', [])
        confidence = max((float(v) for v in scores if isinstance(v, (int, float))
                          and math.isfinite(v) and 0 <= v <= 1), default=None)
        confidence_tag = 'unknown' if confidence is None else '%04d' % round(confidence * 1000)
        name = 'event_%d_conf%s_%s' % (time.time_ns(), confidence_tag, clip['event_id'][:8])
        destination = self.directory / (name + suffix)
        first = cv2.imdecode(np.frombuffer(frames[0]['jpeg'], np.uint8), cv2.IMREAD_COLOR)
        height, width = first.shape[:2]
        selected = []
        encoded_valid = []
        encoded_ids = []
        index = 0
        for number in range(count):
            target = start + number / self.fps
            while index + 1 < len(frames) and frames[index + 1]['time'] <= target:
                index += 1
            item = frames[index]
            state = None
            for event in clip['state_events']:
                if event.get('monotonic', float('inf')) <= target:
                    state = event.get('valid', False)
            valid = (item['valid'] and item['time'] <= target
                     and target <= item['expires_at'] and state is not False)
            selected.append(item if valid else None)
            encoded_valid.append(valid)
            encoded_ids.append(item['frame_id'] if valid else None)
        def decode(item):
            if item is None:
                masked = np.zeros((height, width, 3), np.uint8)
                cv2.putText(masked, 'DETECTION UNAVAILABLE', (3, height // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, .35, (0, 180, 255), 1)
                return masked
            return cv2.imdecode(np.frombuffer(item['jpeg'], np.uint8), cv2.IMREAD_COLOR)
        if self.encoder == 'opencv':
            writer = cv2.VideoWriter(str(destination), cv2.VideoWriter_fourcc(*'MJPG'),
                                     self.fps, (width, height))
            try:
                if not writer.isOpened():
                    raise OSError('MJPEG video writer failed')
                for item in selected:
                    frame = decode(item)
                    if frame.shape[:2] != (height, width):
                        raise ValueError('event clip dimensions changed')
                    writer.write(frame)
            finally:
                writer.release()
        else:
            # Board image lacks rawvideoparse/videoparse. JPEG parser is present
            # and provides explicit frame boundaries for a portable pipe input.
            command = ['gst-launch-1.0', '-q', '-e', 'fdsrc', 'fd=0', '!',
                       'jpegparse', '!', 'jpegdec', '!', 'videoconvert', '!', 'videorate', '!',
                       'video/x-raw,format=NV12,framerate=%d/1000' % round(self.fps * 1000),
                       '!', 'mpph264enc', 'bps=2000000', 'gop=10', '!', 'h264parse', '!', 'mp4mux', '!',
                       'filesink', 'location=' + str(destination)]
            log_path = destination.with_suffix('.encoder.log')
            with log_path.open('wb') as log:
                process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                           stdout=subprocess.DEVNULL, stderr=log)
                self._process = process
                try:
                    for item in selected:
                        frame = decode(item)
                        if frame.shape[:2] != (height, width):
                            raise ValueError('event clip dimensions changed')
                        ok, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                        if not ok:
                            raise OSError('MPP input JPEG encode failed')
                        process.stdin.write(jpeg.tobytes())
                    process.stdin.close()
                    if process.wait(timeout=30) != 0:
                        raise RuntimeError('MPP encoder failed; see ' + str(log_path))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    self._process = None
        if not destination.exists() or destination.stat().st_size == 0:
            raise OSError('event clip output missing')
        def interval_complete(begin, finish):
            # Coverage is a continuous union of available fresh output intervals,
            # not merely values sampled at video frame centers. A future packet
            # cannot repair an earlier missing interval.
            if any(event.get('valid') is False and begin <= event.get('monotonic', -math.inf) < finish
                   for event in clip['state_events']):
                return False
            cursor = begin
            for item in frames:
                if not item['valid'] or item['time'] >= finish:
                    continue
                if item['time'] > cursor + 1e-8:
                    return False
                cursor = max(cursor, item['expires_at'])
                if cursor >= finish - 1e-8:
                    return True
            return cursor >= finish - 1e-8
        clip.update({
            'file': destination.name, 'encoder': self.encoder, 'fps': self.fps,
            'event_max_confidence': confidence,
            'encoded_frames': count, 'encoded_duration': count / self.fps,
            'first_output_at': frames[0]['time'], 'last_output_at': frames[-1]['time'],
            'pre_complete': interval_complete(start, clip['event']['monotonic']),
            'post_complete': interval_complete(clip['event']['monotonic'], end),
            'source_frame_ids': [f['frame_id'] for f in frames],
            'encoded_source_frame_ids': encoded_ids, 'masked_frames': encoded_ids.count(None),
            'max_annotation_hold_seconds': max(self._max_hold, .5),
            'freshness_rule': 'recorded expires_at, capped at max(frame_period,0.5s); invalid state masks interval',
            'encoder_input': 'JPEG90 parsed to NV12' if self.encoder == 'mpp' else 'decoded BGR',
            'invalid_frame_ids': [f['frame_id'] for f in frames if not f['valid']],
        })
        destination.with_suffix('.json').write_text(
            json.dumps(clip, indent=2, allow_nan=False), encoding='utf-8')

    def close(self):
        if self._closed:
            return
        self._stopping.set()
        self._timer.join(timeout=1)
        with self._lock:
            for clip in self._active:
                try:
                    self._submit(clip, 'shutdown')
                except Exception as error:
                    self._errors.append(str(error))
            self._active.clear()
            self._closed = True
        try:
            self._jobs.put(None, timeout=5)
        except queue.Full:
            self._errors.append('event encode queue did not drain on shutdown')
            if self._process is not None:
                self._process.kill()
            # Preserve a terminal sentinel even if pending work must be dropped.
            try:
                self._jobs.get_nowait()
                self._jobs.task_done()
            except queue.Empty:
                pass
            self._jobs.put_nowait(None)
        self._worker.join(timeout=35)
        if self._worker.is_alive():
            if self._process is not None:
                self._process.kill()
            self._worker.join(timeout=5)
            raise RuntimeError('event encoder did not stop within deadline')
        if self._errors:
            raise RuntimeError('clip encoding failed: ' + self._errors[0])
