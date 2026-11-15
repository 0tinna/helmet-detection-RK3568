"""Token protected local controls for a Qt board application.

The HTTP listener is loopback only. Detection resources belong to Pipeline;
this controller requests stop and waits, never releases a running device.
WiFi commands are implemented by the explicit backend, not by HTTP input.
"""
import argparse
import base64
from contextlib import nullcontext
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import threading
import time
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import cv2

try:
    from ..pipeline_runtime import Pipeline
except ImportError:
    # Allows app/service.py to run in the existing flat board src tree.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline_runtime import Pipeline


MAX_BODY_BYTES = 8192
PROFILES = {
    'replay': {'name': '录像回放演示 10 FPS', 'replay_fps': 10.0},
    'responsive': {'name': '回放响应测试 20 FPS', 'replay_fps': 20.0},
}
MODEL_NAME = re.compile(r'^helmet(?:_\d+x\d+|-640-640)\.rknn$')
SOURCE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.avi', '.mp4', '.mkv', '.mov'}


def _inside(path, parent):
    try:
        path.relative_to(parent)
        return path != parent
    except ValueError:
        return False


class DetectionController:
    def __init__(self, root, pipeline_factory=Pipeline):
        self.root = Path(root).resolve()
        self.pipeline_factory = pipeline_factory
        self.lock = threading.RLock()
        self.pipeline = None
        self.worker = None
        self.generation = None
        self.run_uuid = None
        self.closed = False
        self.stopping = False
        self.error = None
        self.jpeg = None
        self.frame = None
        self.selection = None

    def configuration(self):
        model_root, source_root = self.root / 'models', self.root / 'input'
        if (not _inside(model_root.resolve(), self.root)
                or not _inside(source_root.resolve(), self.root)):
            raise ValueError('installed directories must remain inside application root')
        models = [{'id': p.name, 'name': p.stem} for p in sorted(model_root.glob('*.rknn'))
                  if MODEL_NAME.fullmatch(p.name) and p.is_file()
                  and _inside(p.resolve(), model_root.resolve())]
        sources = []
        if source_root.is_dir():
            for p in sorted(source_root.rglob('*')):
                if (p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES
                        and _inside(p.resolve(), source_root.resolve())):
                    sources.append({'id': p.relative_to(source_root).as_posix(),
                                    'name': p.name + '（回放演示）', 'kind': 'replay'})
        for p in sorted(Path('/dev').glob('video[0-9]*')):
            if re.fullmatch(r'/dev/video\d+', str(p)) and p.is_char_device():
                sources.append({'id': str(p), 'name': p.name + '（未验收设备）', 'kind': 'camera'})
        return {'models': models, 'sources': sources,
                'profiles': [{'id': key, 'name': row['name']} for key, row in PROFILES.items()],
                'defaults': {'model': models[0]['id'] if models else None,
                             'source': sources[0]['id'] if sources else None, 'profile': 'replay'}}

    def _resolve(self, model, source, profile):
        if not isinstance(model, str) or not isinstance(source, str) or profile not in PROFILES:
            raise ValueError('invalid model, source or profile')
        config = self.configuration()
        if model not in {row['id'] for row in config['models']}:
            raise ValueError('model is not in the installed list')
        if source not in {row['id'] for row in config['sources']}:
            raise ValueError('source is not in the installed list')
        model_path = (self.root / 'models' / model).resolve()
        source_path = Path(source) if source.startswith('/dev/') else (self.root / 'input' / source).resolve()
        return model_path, source_path

    def _snapshot_directory(self):
        results = (self.root / 'results').resolve()
        snapshots = (self.root / 'results' / 'app-snapshots').resolve()
        if not _inside(results, self.root) or not _inside(snapshots, results):
            raise ValueError('snapshot output directories must remain inside application root')
        if (results.exists() and not results.is_dir()) or (snapshots.exists() and not snapshots.is_dir()):
            raise ValueError('snapshot output path must be a directory')
        return snapshots

    def _owned_workers_alive(self):
        if self.worker is not None and self.worker.is_alive():
            return True
        # A Pipeline shutdown timeout can leave a native device worker alive
        # after run() raises. It still owns its SDK/source and blocks restart.
        return self.pipeline is not None and any(
            t is not None and t.is_alive()
            for t in (getattr(self.pipeline, 'capture_thread', None),
                      getattr(self.pipeline, 'detect_thread', None)))

    def start(self, model=None, source=None, profile='replay'):
        defaults = self.configuration()['defaults']
        model = defaults['model'] if model is None else model
        source = defaults['source'] if source is None else source
        model_path, source_path = self._resolve(model, source, profile)
        # Check writable parents and the leaf before Pipeline can create or
        # save anything. A pre-existing symlink/junction must not escape root.
        snapshots = self._snapshot_directory()
        with self.lock:
            if self.closed:
                raise RuntimeError('application is closing')
            if self._owned_workers_alive():
                raise RuntimeError('detection worker still owns its devices')
            self.jpeg = self.frame = None
            self.error = None
            self.stopping = False
            generation = object()
            args = SimpleNamespace(model=str(model_path), source=str(source_path),
                                   conf=.35, nms=.45, skip=0, result_ttl=1.0,
                                   replay_fps=PROFILES[profile]['replay_fps'],
                                   no_display=True, duration=0, shutdown_timeout=2.0,
                                   save_dir=str(snapshots),
                                   save_interval=0)
            pipeline = self.pipeline_factory(args, output_callback=lambda p: self.on_packet(p, generation),
                                             event_callback=lambda event: None)
            self.pipeline, self.generation = pipeline, generation
            self.run_uuid = uuid.uuid4().hex
            self.selection = {'model': model, 'source': source, 'profile': profile}

            def run():
                try:
                    pipeline.run()
                except BaseException:
                    # Device/SDK messages can contain arbitrary values. Preserve
                    # a truthful failure without exporting exception payloads.
                    with self.lock:
                        if self.generation is generation:
                            self.error = 'detection failed; inspect local runtime and device setup'
                finally:
                    with self.lock:
                        if self.generation is generation:
                            self.jpeg = self.frame = None
                    # Pipeline can report a timed-out native worker. Retain a
                    # non-daemon owner until that worker exits; do not abandon
                    # SDK ownership merely because the UI has closed.
                    for owned in (getattr(pipeline, 'capture_thread', None),
                                  getattr(pipeline, 'detect_thread', None)):
                        if owned is not None and owned is not threading.current_thread():
                            while owned.is_alive():
                                owned.join(.1)

            self.worker = threading.Thread(target=run, name='helmet-app-detection', daemon=False)
            self.worker.start()
        return {'started': True, **self.selection}

    def status(self):
        with self.lock:
            pipeline = self.pipeline
            running = self._owned_workers_alive()
            stopping, error, closed = self.stopping, self.error, self.closed
            selection = dict(self.selection or {})
            generation = self.run_uuid
            try:
                if pipeline is None:
                    result = {'state': 'idle', 'reason': 'select an input and start',
                              'valid': False, 'source_epoch': None}
                else:
                    # Pipeline's lock is an RLock. Read state and epoch under
                    # that same lock so a short invalid/recovery transition
                    # cannot pair an old status with a newer source identity.
                    with getattr(pipeline, 'lock', nullcontext()):
                        result = dict(pipeline.status_snapshot())
                        result['source_epoch'] = getattr(pipeline, 'source_epoch', None)
            except BaseException:
                result = {'state': 'fault', 'reason': 'detection status unavailable',
                          'valid': False, 'source_epoch': None}
        if not running or stopping or closed or error:
            result.update(valid=False, alarm_state='unknown')
        if error:
            result.update(state='fault', reason=error)
        elif stopping and running:
            result.update(state='stopping', reason='waiting for device workers to stop')
        elif closed:
            result.update(state='closed', reason='application is closing')
        result.update(running=running, selection=selection, generation=generation)
        return result

    def _fresh(self, packet, pipeline):
        metadata = packet.metadata
        captured = packet.captured_at
        with getattr(pipeline, 'lock', nullcontext()):
            return (metadata.get('valid') is True and isinstance(captured, (int, float))
                    and math.isfinite(captured) and 0 <= time.monotonic() - captured <= pipeline.args.result_ttl
                    and metadata.get('source_epoch') == getattr(pipeline, 'source_epoch', None)
                    and pipeline.status_snapshot().get('valid') is True)

    def on_packet(self, packet, generation=None):
        with self.lock:
            pipeline = self.pipeline
            if (self.closed or self.stopping or pipeline is None
                    or (generation is not None and generation is not self.generation)):
                return
        try:
            if not self._fresh(packet, pipeline):
                with self.lock:
                    self.jpeg = self.frame = None
                return
            success, encoded = cv2.imencode('.jpg', packet.annotated_bgr)
            if not success:
                raise RuntimeError('JPEG encoding failed')
            jpeg = encoded.tobytes()
            # Encoding can block while the source changes or TTL expires.
            if not self._fresh(packet, pipeline):
                jpeg = None
        except BaseException:
            with self.lock:
                self.jpeg = self.frame = None
            raise
        with self.lock:
            if self.pipeline is pipeline and not self.closed and not self.stopping:
                self.jpeg, self.frame = jpeg, packet if jpeg is not None else None

    def preview_package(self):
        with self.lock:
            pipeline, packet, jpeg = self.pipeline, self.frame, self.jpeg
            unavailable = self.closed or self.stopping or not self._owned_workers_alive()
        if unavailable or packet is None or jpeg is None:
            return None
        try:
            valid = self._fresh(packet, pipeline)
        except BaseException:
            valid = False
        with self.lock:
            # A reader owns only its snapshot. If the application restarted,
            # never invalidate the new owner's cache from this old request.
            if self.pipeline is not pipeline or self.stopping or self.closed:
                return None
            if self.frame is not packet:
                # Publication is normal, not an outage. Read the latest packet
                # once while locked rather than clear it or spin until stable.
                packet, jpeg = self.frame, self.jpeg
                if packet is None or jpeg is None:
                    return None
                valid = True  # The old snapshot's verdict is irrelevant now.
            with getattr(pipeline, 'lock', nullcontext()):
                try:
                    valid = valid and self._fresh(packet, pipeline)
                except BaseException:
                    valid = False
                if not valid:
                    self.jpeg = self.frame = None
                    return None
                age_ms = (time.monotonic() - packet.captured_at) * 1000.0
                remaining = pipeline.args.result_ttl * 1000.0 - age_ms
                if remaining <= 0:
                    self.jpeg = self.frame = None
                    return None
                return {'jpeg': jpeg, 'frame_id': packet.frame_id, 'captured_at': packet.captured_at,
                        'source_epoch': packet.metadata['source_epoch'], 'valid': True,
                        'source_age_ms': age_ms, 'remaining_ttl_ms': remaining,
                        'generation': self.run_uuid}

    def preview(self):
        package = self.preview_package()
        return package['jpeg'] if package is not None else None

    def stop(self, timeout=2.0):
        with self.lock:
            self.stopping = True
            self.jpeg = self.frame = None
            pipeline, worker = self.pipeline, self.worker
            if pipeline is not None:
                pipeline.stop_event.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join(max(0.0, timeout))
        with self.lock:
            stopped = not self._owned_workers_alive()
        return {'stopped': stopped, 'state': 'stopped' if stopped else 'stopping'}

    def close(self, timeout=2.0):
        with self.lock:
            self.closed = True
        return self.stop(timeout)['stopped']


def _wifi_result(kind, value):
    """Allow listed data only, never arbitrary backend exception/secret fields."""
    if not isinstance(value, dict):
        raise ValueError('invalid WiFi backend result')
    keys = {'status': ('state', 'ssid', 'ip'),
            'connect': ('connected', 'state', 'ssid', 'ip', 'saved', 'rollback_restored')}
    if kind == 'scan':
        networks = value.get('networks', [])
        if not isinstance(networks, list):
            raise ValueError('invalid scan results')
        result = {'networks': [{key: row.get(key) for key in
                               ('ssid', 'security', 'signal', 'bssid', 'frequency', 'hidden', 'connectable')}
                              for row in networks[:256] if isinstance(row, dict)]}
    else:
        result = {key: value.get(key) for key in keys[kind]}
    result['error'] = 'WiFi operation failed' if value.get('error') else None
    return result


class WifiActions:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.Lock()
        self.worker = None
        self.closed = False
        self.action = {'id': 0, 'kind': None, 'busy': False, 'status': 'idle', 'result': None}

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.action, allow_nan=False))

    def start(self, kind, payload):
        if kind not in ('scan', 'connect'):
            raise ValueError('unknown WiFi operation')
        if kind == 'scan' and payload:
            raise ValueError('scan does not accept parameters')
        if kind == 'connect':
            if set(payload) - {'ssid', 'password', 'security', 'remember'}:
                raise ValueError('unknown WiFi parameters')
            if (not isinstance(payload.get('ssid'), str) or not 1 <= len(payload['ssid'].encode('utf-8')) <= 32
                    or not isinstance(payload.get('password', ''), str)
                    or len(payload.get('password', '').encode('utf-8')) > 128
                    or payload.get('security') not in ('open', 'WPA2-PSK')
                    or type(payload.get('remember', False)) is not bool):
                raise ValueError('invalid SSID or supported authentication parameters')
        with self.lock:
            if self.closed:
                raise RuntimeError('application is closing')
            if self.worker is not None and self.worker.is_alive():
                raise RuntimeError('WiFi operation is busy')
            self.action = {'id': self.action['id'] + 1, 'kind': kind, 'busy': True,
                           'status': 'running', 'result': None}

            def run():
                status = 'completed'
                try:
                    if kind == 'scan':
                        raw = self.backend.scan()
                    else:
                        raw = self.backend.connect(payload['ssid'], payload.get('password', ''),
                                                   payload['security'], payload.get('remember', False))
                    result = _wifi_result(kind, raw)
                    if result.get('error') or (kind == 'connect' and result.get('connected') is not True):
                        status = 'failed'
                    # Whitelisted SSID data can equal the entered secret. Redact
                    # any reflected password recursively before publishing.
                    secret = payload.get('password', '')
                    if secret:
                        def redact(item):
                            if isinstance(item, str):
                                return item.replace(secret, '[redacted]')
                            if isinstance(item, list):
                                return [redact(row) for row in item]
                            if isinstance(item, dict):
                                return {key: redact(row) for key, row in item.items()}
                            return item
                        result = redact(result)
                except BaseException:
                    status, result = 'failed', {'error': 'WiFi operation failed'}
                finally:
                    payload.clear()
                with self.lock:
                    self.action.update(busy=False, status=status, result=result)

            # Pending WiFi rollback must retain a process owner even when the
            # window closes. The backend bounds operations; it is not killed.
            self.worker = threading.Thread(target=run, name='helmet-app-wifi', daemon=False)
            self.worker.start()
            return dict(self.action)

    def close(self, timeout=2.0):
        with self.lock:
            self.closed = True
            worker = self.worker
        close_backend = getattr(self.backend, 'close', None)
        if close_backend is not None:
            try:
                # Backend close only requests cancellation/rollback; it must
                # not release another thread's live control socket.
                close_backend()
            except BaseException:
                return False
        if worker is not None and worker is not threading.current_thread():
            worker.join(max(0.0, timeout))
        return worker is None or not worker.is_alive()


class LocalService:
    def __init__(self, controller, wifi, token=None, port=0):
        self.controller, self.wifi = controller, wifi
        self.token = token or secrets.token_urlsafe(32)
        self.actions = WifiActions(wifi)
        self.closed = False
        self.thread = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                # Neither token-bearing request URLs nor passwords enter logs.
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(2.0)

            def send_bytes(self, code, body, content_type='application/json'):
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                self.wfile.write(body)

            def send_json(self, code, value):
                self.send_bytes(code, json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))

            def authorized(self, route, query):
                try:
                    if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                        return False
                    allowed = {'127.0.0.1:%d' % owner.port, 'localhost:%d' % owner.port}
                    if self.headers.get('Host') not in allowed:
                        return False
                    origin = self.headers.get('Origin')
                    if origin and origin not in {'http://' + value for value in allowed}:
                        return False
                    token = self.headers.get('X-Helmet-Token')
                    if token is None and route in ('/api/frame', '/frame.jpg'):
                        values = query.get('token', [])
                        token = values[0] if len(values) == 1 else None
                    return isinstance(token, str) and hmac.compare_digest(token, owner.token)
                except (ValueError, TypeError):
                    return False

            def body(self):
                if self.headers.get('Transfer-Encoding'):
                    raise ValueError('transfer encoding is not supported')
                length = self.headers.get('Content-Length')
                if length is None or not re.fullmatch(r'\d+', length):
                    raise ValueError('content length is required')
                length = int(length)
                if length > MAX_BODY_BYTES:
                    raise OverflowError('body exceeds maximum size')
                if self.headers.get_content_type() != 'application/json':
                    raise ValueError('JSON content type is required')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('incomplete body')
                payload = json.loads(raw.decode('utf-8'), parse_constant=lambda _: None)
                if not isinstance(payload, dict):
                    raise ValueError('JSON object is required')
                return payload

            def do_GET(self):
                self.dispatch(False)

            def do_POST(self):
                self.dispatch(True)

            def dispatch(self, post):
                try:
                    url = urlsplit(self.path)
                    query = parse_qs(url.query, max_num_fields=8)
                    route = url.path
                    if not self.authorized(route, query):
                        self.send_json(403, {'error': 'local session authorization required'})
                        return
                    if owner.closed:
                        self.send_json(503, {'error': 'application is closing'})
                        return
                    if not post:
                        if route == '/api/status':
                            value = {'detection': owner.controller.status(), 'wifi_action': owner.actions.snapshot()}
                        elif route == '/api/config':
                            value = owner.controller.configuration()
                        elif route == '/api/action':
                            value = owner.actions.snapshot()
                        elif route == '/api/wifi/status':
                            value = _wifi_result('status', owner.wifi.status())
                        elif route == '/api/preview':
                            value = owner.controller.preview_package()
                            if value is None:
                                self.send_json(503, {'error': 'fresh detection preview unavailable'})
                                return
                            value['jpeg_base64'] = base64.b64encode(value.pop('jpeg')).decode('ascii')
                        elif route in ('/api/frame', '/frame.jpg'):
                            jpeg = owner.controller.preview()
                            if jpeg is None:
                                self.send_json(503, {'error': 'fresh detection preview unavailable'})
                            else:
                                self.send_bytes(200, jpeg, 'image/jpeg')
                            return
                        else:
                            self.send_json(404, {'error': 'unknown endpoint'})
                            return
                        self.send_json(200, value)
                        return
                    payload = self.body()
                    if route == '/api/start':
                        if set(payload) - {'model', 'source', 'profile'}:
                            raise ValueError('unknown detection parameters')
                        value = owner.controller.start(**payload)
                    elif route == '/api/stop':
                        if payload:
                            raise ValueError('stop does not accept parameters')
                        value = owner.controller.stop()
                    elif route in ('/api/wifi/scan', '/api/wifi/connect'):
                        value = owner.actions.start(route.rsplit('/', 1)[-1], payload)
                        self.send_json(202, value)
                        return
                    else:
                        self.send_json(404, {'error': 'unknown endpoint'})
                        return
                    self.send_json(200, value)
                except OverflowError:
                    self.send_json(413, {'error': 'request body too large'})
                except (ValueError, TypeError, UnicodeError):
                    self.send_json(400, {'error': 'invalid request parameters'})
                except RuntimeError:
                    self.send_json(409, {'error': 'operation busy or unavailable'})
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    return
                except BaseException:
                    self.send_json(500, {'error': 'local operation failed'})

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]

    def start(self):
        if self.closed or self.thread is not None:
            raise RuntimeError('service cannot be started twice')
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .05}, name='helmet-app-http', daemon=True)
        self.thread.start()

    def close(self, timeout=3.0):
        self.closed = True
        if self.thread is not None:
            self.server.shutdown()
            self.thread.join(timeout)
        self.server.server_close()
        deadline = time.monotonic() + timeout
        detection_stopped = self.controller.close(max(0, deadline - time.monotonic()))
        wifi_stopped = self.actions.close(max(0, deadline - time.monotonic()))
        return {'detection_stopped': detection_stopped, 'wifi_stopped': wifi_stopped}


def _private_file(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(text)
    os.chmod(path, 0o600)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Loopback-only board detection and WiFi controls')
    parser.add_argument('--root', required=True)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--ready-file', required=True)
    parser.add_argument('--wifi-interface', default='wlan0')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('port must be within 0..65535')
    try:
        from .wifi import WifiBackend
    except ImportError:
        from wifi import WifiBackend
    service = LocalService(DetectionController(args.root), WifiBackend(interface=args.wifi_interface), port=args.port)
    stopped = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda number, frame: stopped.set())
    try:
        _private_file(args.token_file, service.token + '\n')
        service.start()
        _private_file(args.ready_file, json.dumps({'host': '127.0.0.1', 'port': service.port,
                                                'pid': os.getpid(), 'ready': True}))
        stopped.wait()
    finally:
        result = service.close()
        if not result['wifi_stopped']:
            result['wifi_stopped'] = service.actions.close(timeout=40.0)
    return 0 if all(result.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
