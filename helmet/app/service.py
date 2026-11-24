"""Token protected local controls for a Qt board application.

The control HTTP listener is loopback only; optional LAN preview is read-only.
Detection resources belong to Pipeline;
this controller requests stop and waits, never releases a running device.
WiFi commands are implemented by the explicit backend, not by HTTP input.
"""
import argparse
import base64
from collections import OrderedDict
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
    from .resident import (ACK_OPTIONAL, COMMAND_FIELDS, COORDINATOR_ROOT, SAFE_ID, Coordinator,
                           ResidentConnection, process_identity, retain_resource_lease,
                           validate_install)
except ImportError:
    from resident import (ACK_OPTIONAL, COMMAND_FIELDS, COORDINATOR_ROOT, SAFE_ID, Coordinator,
                          ResidentConnection, process_identity, retain_resource_lease,
                          validate_install)

try:
    from .alert_hub import AlertHub
except ImportError:
    from alert_hub import AlertHub

try:
    from ..pipeline_runtime import Pipeline
except ImportError:
    # Allows app/service.py to run in the existing flat board src tree.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline_runtime import Pipeline


MAX_BODY_BYTES = 8192
MAX_PREVIEW_PACKAGES = 4
PREVIEW_JPEG_QUALITY = 80
PREVIEW_TOKEN = re.compile(r'^[A-Za-z0-9_-]{16,64}$')
RECEIPT_HEARTBEAT = 1.0
PROFILES = {
    'replay': {'name': '录像回放演示 10 FPS', 'replay_fps': 10.0},
    'responsive': {'name': '回放响应测试 20 FPS', 'replay_fps': 20.0},
}
MODEL_NAME = re.compile(r'^helmet(?:_\d+x\d+|-640-640)\.rknn$')
SOURCE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.avi', '.mp4', '.mkv', '.mov'}


class ReceiptCoalescer:
    """Write lifecycle receipts on change, with a bounded idle heartbeat."""
    def __init__(self, interval=RECEIPT_HEARTBEAT, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self.signature = None
        self.published_at = 0.0

    @staticmethod
    def _signature(value):
        stable = {key: item for key, item in value.items() if key != 'received_at'}
        return json.dumps(stable, sort_keys=True, separators=(',', ':'), allow_nan=False)

    def publish(self, value, writer, force=False):
        signature = self._signature(value)
        now = self.clock()
        if not force and signature == self.signature and now - self.published_at < self.interval:
            return False
        # Commit only after the write succeeds so terminal cleanup retries a
        # failed receipt instead of suppressing it for a full heartbeat.
        writer(value)
        self.signature = signature
        self.published_at = now
        return True


def _inside(path, parent):
    try:
        path.relative_to(parent)
        return path != parent
    except ValueError:
        return False


def _camera_sources(device_root=Path('/dev'), sysfs_root=Path('/sys/class/video4linux'),
                    is_char_device=None):
    is_char_device = is_char_device or Path.is_char_device
    sources = []
    candidates = [path for path in device_root.glob('video[0-9]*')
                  if re.fullmatch(r'video\d+', path.name)]
    for path in sorted(candidates, key=lambda row: int(row.name[5:])):
        if not is_char_device(path):
            continue
        class_root = sysfs_root / path.name
        try:
            device_name = (class_root / 'name').read_text(
                encoding='utf-8', errors='replace').strip()
        except OSError:
            device_name = ''
        if device_name.startswith('rkisp'):
            continue
        try:
            interface_index = (class_root / 'index').read_text(
                encoding='ascii', errors='replace').strip()
        except OSError:
            interface_index = ''
        if interface_index and interface_index != '0':
            continue
        sources.append({'id': str(path),
                        'name': '实时摄像头 ' + (device_name or path.name), 'kind': 'camera'})
    return sources


class DetectionController:
    def __init__(self, root, pipeline_factory=Pipeline, alert_hub=None):
        self.root = Path(root).resolve()
        self.pipeline_factory = pipeline_factory
        self.alert_hub = alert_hub
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
        self.preview_packages = OrderedDict()
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
        sources.extend(_camera_sources())
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

    def _clear_preview(self):
        self.jpeg = self.frame = None
        self.preview_packages.clear()

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
            self._clear_preview()
            self.error = None
            self.stopping = False
            generation = object()
            args = SimpleNamespace(model=str(model_path), source=str(source_path),
                                   conf=.35, nms=.45, skip=0, result_ttl=1.0,
                                   replay_fps=PROFILES[profile]['replay_fps'],
                                   no_display=True, duration=0, shutdown_timeout=2.0,
                                   save_dir=str(snapshots),
                                   save_interval=0)
            run_uuid = uuid.uuid4().hex
            if self.alert_hub is not None:
                self.alert_hub.reset(run_uuid)
            pipeline = self.pipeline_factory(args, output_callback=lambda p: self.on_packet(p, generation),
                                             event_callback=lambda event: None)
            self.pipeline, self.generation = pipeline, generation
            self.run_uuid = run_uuid
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
                            self._clear_preview()
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
                    self._clear_preview()
                return
            success, encoded = cv2.imencode(
                '.jpg', packet.annotated_bgr,
                [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY])
            if not success:
                raise RuntimeError('JPEG encoding failed')
            jpeg = encoded.tobytes()
            # Encoding can block while the source changes or TTL expires.
            if not self._fresh(packet, pipeline):
                jpeg = None
        except BaseException:
            with self.lock:
                self._clear_preview()
            raise
        alert_jpeg = alert_generation = None
        with self.lock:
            if self.pipeline is pipeline and not self.closed and not self.stopping:
                self.jpeg, self.frame = jpeg, packet if jpeg is not None else None
                if jpeg is not None:
                    token = secrets.token_urlsafe(18)
                    self.preview_packages[token] = {
                        'jpeg': jpeg,
                        'packet': packet,
                        'generation': self.run_uuid,
                    }
                    while len(self.preview_packages) > MAX_PREVIEW_PACKAGES:
                        self.preview_packages.popitem(last=False)
                    alert_jpeg, alert_generation = jpeg, self.run_uuid
        if alert_jpeg is not None and self.alert_hub is not None:
            try:
                self.alert_hub.observe(packet, alert_jpeg, alert_generation)
            except BaseException:
                # Event evidence and notification failures never fault the
                # detector or erase an otherwise valid preview.
                pass

    def preview_package(self, after_capture=None, frame_token=None):
        if (after_capture is not None and (type(after_capture) not in (int, float)
                or not math.isfinite(after_capture) or after_capture < 0)):
            raise ValueError('invalid preview capture boundary')
        if frame_token is not None and not isinstance(frame_token, str):
            raise ValueError('invalid preview token')
        if isinstance(frame_token, str) and not PREVIEW_TOKEN.fullmatch(frame_token):
            raise ValueError('invalid preview token')
        with self.lock:
            pipeline, packet, jpeg = self.pipeline, self.frame, self.jpeg
            package_generation = self.run_uuid
            if frame_token is not None:
                entry = self.preview_packages.get(frame_token)
                if entry is None:
                    return None
                packet, jpeg = entry['packet'], entry['jpeg']
                package_generation = entry['generation']
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
            if frame_token is not None:
                entry = self.preview_packages.get(frame_token)
                if entry is None:
                    return None
                packet, jpeg = entry['packet'], entry['jpeg']
                package_generation = entry['generation']
            elif self.frame is not packet:
                # Publication is normal, not an outage. Read the latest packet
                # once while locked rather than clear it or spin until stable.
                packet, jpeg = self.frame, self.jpeg
                if packet is None or jpeg is None:
                    return None
                package_generation = self.run_uuid
                valid = True  # The old snapshot's verdict is irrelevant now.
            with getattr(pipeline, 'lock', nullcontext()):
                try:
                    valid = valid and self._fresh(packet, pipeline)
                except BaseException:
                    valid = False
                if not valid:
                    if frame_token is not None:
                        self.preview_packages.pop(frame_token, None)
                    elif self.frame is packet:
                        self._clear_preview()
                    return None
                age_ms = (time.monotonic() - packet.captured_at) * 1000.0
                remaining = pipeline.args.result_ttl * 1000.0 - age_ms
                if remaining <= 0:
                    if frame_token is not None:
                        self.preview_packages.pop(frame_token, None)
                    elif self.frame is packet:
                        self._clear_preview()
                    return None
                if after_capture is not None and packet.captured_at < after_capture:
                    return None
                return {'jpeg': jpeg, 'frame_token': frame_token or next(
                            (key for key, value in reversed(self.preview_packages.items())
                             if value['packet'] is packet), None),
                        'frame_id': packet.frame_id, 'captured_at': packet.captured_at,
                        'source_epoch': packet.metadata['source_epoch'], 'valid': True,
                        'source_age_ms': age_ms, 'remaining_ttl_ms': remaining,
                        'generation': package_generation}

    def preview(self):
        package = self.preview_package()
        return package['jpeg'] if package is not None else None

    def stop(self, timeout=2.0):
        with self.lock:
            self.stopping = True
            self._clear_preview()
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
    def __init__(self, controller, wifi, token=None, port=0, application=None,
                 lan_port=None, lan_host='0.0.0.0', alert_hub=None):
        self.controller, self.wifi = controller, wifi
        self.token = token or secrets.token_urlsafe(32)
        self.actions = WifiActions(wifi)
        self.closed = False
        self.stopping = False
        self.application = application
        self.close_lock = threading.RLock()
        self.shutdown_requested = threading.Event()
        self.thread = None
        self.lan = None
        self.lan_port, self.lan_host = lan_port, lan_host
        self.alert_hub = alert_hub
        self.alert_stopped = alert_hub is None
        if alert_hub is not None:
            bind_status = getattr(alert_hub, 'bind_status_provider', None)
            if bind_status is not None:
                bind_status(self.controller.status)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                # Neither token-bearing request URLs nor passwords enter logs.
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(2.0)

            def send_bytes(self, code, body, content_type='application/json', headers=None):
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Connection', 'close')
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
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
                            value = {'detection': owner.controller.status(), 'wifi_action': owner.actions.snapshot(),
                                     'service': owner.progress()}
                        elif route == '/api/config':
                            value = owner.controller.configuration()
                            value['detection'] = owner.controller.status()
                            selection = value['detection']['selection']
                            value['selection_available'] = {
                                key: selection.get(key) in {row['id'] for row in value[collection]}
                                for key, collection in (('model', 'models'), ('source', 'sources'), ('profile', 'profiles'))}
                        elif route == '/api/application':
                            value = owner.application_snapshot()
                        elif route == '/api/action':
                            value = owner.actions.snapshot()
                        elif route == '/api/wifi/status':
                            value = _wifi_result('status', owner.wifi.status())
                        elif route == '/api/preview':
                            boundary = query.get('after')
                            if boundary is not None and len(boundary) != 1:
                                raise ValueError('one capture boundary is required')
                            after = float(boundary[0]) if boundary is not None else None
                            value = owner.controller.preview_package(after_capture=after)
                            if value is None:
                                self.send_json(503, {'error': 'fresh detection preview unavailable'})
                                return
                            value['jpeg_base64'] = base64.b64encode(value.pop('jpeg')).decode('ascii')
                        elif route == '/api/preview-meta':
                            boundary = query.get('after')
                            if boundary is not None and len(boundary) != 1:
                                raise ValueError('one capture boundary is required')
                            after = float(boundary[0]) if boundary is not None else None
                            value = owner.controller.preview_package(after_capture=after)
                            if value is None:
                                self.send_json(503, {'error': 'fresh detection preview unavailable'})
                                return
                            value.pop('jpeg')
                        elif route in ('/api/frame', '/frame.jpg'):
                            frame_token = query.get('frame_token')
                            if frame_token is not None and len(frame_token) != 1:
                                raise ValueError('one frame token is required')
                            package = owner.controller.preview_package(
                                frame_token=frame_token[0] if frame_token is not None else None)
                            if package is None:
                                self.send_json(503, {'error': 'fresh detection preview unavailable'})
                            else:
                                self.send_bytes(200, package['jpeg'], 'image/jpeg',
                                                owner.frame_headers(package))
                            return
                        else:
                            self.send_json(404, {'error': 'unknown endpoint'})
                            return
                        self.send_json(200, value)
                        return
                    payload = self.body()
                    if owner.stopping and route not in ('/api/application/quit', '/api/application/ack',
                                                         '/api/application/render', '/api/shutdown', '/api/stop'):
                        raise RuntimeError('application is closing')
                    if (route in ('/api/start', '/api/wifi/scan', '/api/wifi/connect')
                            and owner.application is not None
                            and owner.application_snapshot().get('phase') not in ('starting', 'running')):
                        raise RuntimeError('resident no longer accepts new operations')
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
                    elif route.startswith('/api/application/'):
                        value = owner.application_command(route.rsplit('/', 1)[-1], payload)
                        self.send_json(202, value)
                        return
                    elif route == '/api/shutdown':
                        if payload:
                            raise ValueError('shutdown does not accept parameters')
                        owner.begin_shutdown()
                        self.send_json(202, {'accepted': True, 'service': owner.progress()})
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

    @staticmethod
    def frame_headers(package):
        return {'X-Helmet-Frame-Id': str(package['frame_id']),
                'X-Helmet-Generation': package['generation'],
                'X-Helmet-Source-Epoch': str(package['source_epoch']),
                'X-Helmet-Captured-At': '%.9f' % package['captured_at'],
                'X-Helmet-Remaining-Ttl-Ms': '%.3f' % package['remaining_ttl_ms']}

    def start(self):
        if self.closed or self.thread is not None:
            raise RuntimeError('service cannot be started twice')
        try:
            if self.alert_hub is not None:
                self.alert_hub.start()
                self.alert_stopped = False
            if self.lan_port is not None:
                try:
                    from .lan import LanPreview
                except ImportError:
                    from lan import LanPreview
                self.lan = LanPreview(self.controller, host=self.lan_host, port=self.lan_port)
                self.lan.start()
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                kwargs={'poll_interval': .05}, name='helmet-app-http', daemon=True)
            self.thread.start()
        except BaseException:
            if self.lan is not None:
                try:
                    self.lan.close()
                except BaseException:
                    pass
            if self.alert_hub is not None:
                try:
                    self.alert_stopped = self.alert_hub.close(3.0)
                except BaseException:
                    self.alert_stopped = False
            raise

    def application_snapshot(self):
        if self.application is None:
            return {'resident': False, 'phase': 'stopping' if self.stopping else 'standalone',
                    'progress': self.progress()}
        try:
            value = self.application.request('status')['application']
            value['progress'] = self.progress()
            return value
        except (OSError, ValueError, RuntimeError):
            # The service can still report its own real owners after manager loss.
            return {'session': self.application.session, 'phase': 'stopping' if self.stopping else 'failed',
                    'manager_available': False, 'progress': self.progress()}

    def application_command(self, command, payload):
        if self.application is None or command not in COMMAND_FIELDS or command == 'status':
            raise ValueError('unknown application command')
        required = {'session'} | COMMAND_FIELDS[command]
        optional = ACK_OPTIONAL - {'received_at'} if command == 'ack' else set()
        if (not required <= set(payload) or set(payload) - required - optional
                or payload['session'] != self.application.session
                or not isinstance(payload['request_id'], str) or not SAFE_ID.fullmatch(payload['request_id'])):
            raise ValueError('invalid application session or parameters')
        fields = {key: value for key, value in payload.items() if key != 'session'}
        if command == 'ack':
            fields['received_at'] = time.monotonic()
        elif command == 'render':
            fields.update(self.render_verdict(payload))
        if command == 'quit':
            try:
                value = self.application.request(command, **fields)
            except (OSError, RuntimeError):
                # A locally authorized valid quit can still stop an orphan.
                self.begin_shutdown()
                return {'accepted': True, 'application': self.application_snapshot()}
            # Validate session/idempotency before an irreversible local change.
            # New work also checks the resident intent during dispatch above.
            self.begin_shutdown()
            return value
        return self.application.request(command, **fields)

    def render_verdict(self, payload):
        """Completion time comes from the service clock after Qt saved the image.

        The pictured frame may precede the latest status frame. Compare its
        actual displayed identity with generation/epoch/selection/TTL, never
        replace it with a newer controller.frame_id.
        """
        packet = payload.get('packet')
        if packet is None:
            valid = None if payload.get('saved') is True and payload.get('visible') is True and not self.stopping else False
            return {'received_at': time.monotonic(), 'valid_at_receipt': valid, 'remaining_ttl_ms': 0.0}
        if not isinstance(packet, dict) or not isinstance(packet.get('captured_at'), (int, float)):
            raise ValueError('invalid displayed packet')
        captured = packet['captured_at']
        if type(captured) is bool or not math.isfinite(captured) or captured < 0:
            raise ValueError('invalid displayed capture time')
        app = self.application_snapshot()
        boundary = app.get('intent', {}).get('preview_after', math.inf)
        with self.controller.lock:
            pipeline = self.controller.pipeline
            with getattr(pipeline, 'lock', nullcontext()):
                status = self.controller.status()
                ttl = getattr(getattr(pipeline, 'args', None), 'result_ttl', 0)
                now = time.monotonic()
                valid = bool(payload.get('saved') is True and payload.get('visible') is True
                             and not self.stopping and status.get('running') and status.get('valid')
                             and packet.get('generation') == status.get('generation')
                             and packet.get('source_epoch') == status.get('source_epoch')
                             and packet.get('selection') == status.get('selection')
                             and boundary <= captured <= now and now - captured < ttl)
        return {'received_at': now, 'valid_at_receipt': valid,
                'remaining_ttl_ms': max(0.0, (ttl - (now - captured)) * 1000.0) if valid else 0.0}

    def progress(self):
        detection_alive = self.controller.status()['running']
        worker = self.actions.worker
        wifi_alive = worker is not None and worker.is_alive()
        if self.alert_hub is None:
            alert_status = {
                'mail': {'configured': False, 'state': 'disabled',
                         'worker_alive': False},
                'viewer': {'alive': False, 'port': None},
            }
        else:
            try:
                alert_status = self.alert_hub.status()
            except BaseException:
                alert_status = {
                    'mail': {'configured': False, 'state': 'unavailable',
                             'worker_alive': False},
                    'viewer': {'alive': False, 'port': None},
                }
        mail = alert_status.get('mail')
        if not isinstance(mail, dict):
            mail = {'configured': False, 'state': 'unavailable',
                    'worker_alive': False}
        viewer = alert_status.get('viewer')
        if not isinstance(viewer, dict):
            viewer = {'alive': False, 'port': None}
        alert_viewer_alive = viewer.get('alive') is True
        alert_port = viewer.get('port')
        return {'state': 'stopping' if self.stopping else 'running',
                'detection_alive': detection_alive, 'wifi_alive': wifi_alive,
                'detection_stopped': self.stopping and not detection_alive,
                'wifi_stopped': self.stopping and not wifi_alive,
                'alert_viewer_alive': alert_viewer_alive,
                'alert_port': alert_port,
                'alert_stopped': self.alert_stopped,
                'mail': mail,
                'http_alive': self.thread is not None and self.thread.is_alive() and not self.closed,
                'cleanup_completed': (self.closed and not detection_alive and not wifi_alive
                                      and self.alert_stopped),
                'lan_alive': self.lan is not None and self.lan.thread is not None
                    and self.lan.thread.is_alive() and not self.lan.closed.is_set(),
                'lan_port': self.lan.port if self.lan is not None else None,
                'received_at': time.monotonic()}

    def begin_shutdown(self):
        with self.close_lock:
            self.stopping = True
            # These flags close the race with HTTP starts before the cleanup loop.
            with self.controller.lock:
                self.controller.closed = True
            with self.actions.lock:
                self.actions.closed = True
            self.shutdown_requested.set()

    def close(self, timeout=3.0):
        with self.close_lock:
            self.begin_shutdown()
            deadline = time.monotonic() + max(0, timeout)
            detection_stopped = self.controller.close(max(0, deadline - time.monotonic()))
            wifi_stopped = self.actions.close(max(0, deadline - time.monotonic()))
            if detection_stopped and self.alert_hub is not None:
                try:
                    self.alert_stopped = self.alert_hub.close(
                        max(0, deadline - time.monotonic()))
                except BaseException:
                    self.alert_stopped = False
            elif detection_stopped:
                self.alert_stopped = True
            if detection_stopped and wifi_stopped and self.alert_stopped:
                if self.lan is not None:
                    self.lan.close()
                self.closed = True
                if self.thread is not None and self.thread.is_alive():
                    self.server.shutdown()
                    self.thread.join(max(0, deadline - time.monotonic()))
                self.server.server_close()
            return {'detection_stopped': detection_stopped,
                    'wifi_stopped': wifi_stopped,
                    'alert_stopped': self.alert_stopped}


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
    parser.add_argument('--lan-port', type=int, default=8091,
                        help='read-only LAN preview port; 0 disables it')
    parser.add_argument('--alert-port', type=int, default=8092,
                        help='authenticated event-only public port; 0 disables alerts')
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--ready-file', required=True)
    parser.add_argument('--wifi-interface', default='wlan0')
    parser.add_argument('--lease-fd', type=int)
    parser.add_argument('--lease-dir')
    parser.add_argument('--resident-session')
    parser.add_argument('--resident-dir')
    parser.add_argument('--lifecycle-file')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('port must be within 0..65535')
    if not 0 <= args.lan_port <= 65535:
        parser.error('LAN port must be within 0..65535')
    if not 0 <= args.alert_port <= 65535:
        parser.error('alert port must be within 0..65535')
    if (args.lease_fd is None) != (args.lease_dir is None):
        parser.error('inherited resource descriptor and lease directory are paired')
    if args.lease_dir is not None and Path(args.lease_dir) != COORDINATOR_ROOT:
        parser.error('resource lease must use the fixed resident coordinator')
    if (args.resident_session is None) != (args.resident_dir is None):
        parser.error('resident session and coordinator are paired')
    if args.resident_dir is not None and (Path(args.resident_dir) != COORDINATOR_ROOT
            or args.lease_fd is None or not re.fullmatch(r'[0-9a-f]{32}', args.resident_session)):
        parser.error('resident identity requires the fixed inherited resource lease')
    root = validate_install(Path(args.root), Path(__file__).resolve().parent)
    token_path, ready_path = Path(args.token_file), Path(args.ready_file)
    if (token_path.name != 'token' or ready_path.name != 'ready.json' or token_path.parent != ready_path.parent
            or token_path.parent.parent != root / 'app-state'
            or not re.fullmatch(r'session-[0-9a-f]{32}', token_path.parent.name)):
        parser.error('private token/ready files must be in the installed session workspace')
    if args.lifecycle_file is not None and Path(args.lifecycle_file) != token_path.parent / 'service-state.json':
        parser.error('lifecycle receipt must use the fixed private session file')
    if args.resident_session is not None and token_path.parent.name != 'session-' + args.resident_session:
        parser.error('private workspace must match the resident session')
    alert_hub = AlertHub.from_root(root, port=args.alert_port) if args.alert_port else None
    coordinator = Coordinator().prepare()
    lease = (retain_resource_lease(args.lease_fd, args.lease_dir) if args.lease_fd is not None
             else coordinator.acquire('resource.lock'))
    workspace = Coordinator(token_path.parent).prepare()
    try:
        from .wifi import WifiBackend
    except ImportError:
        from wifi import WifiBackend
    application = (ResidentConnection(root, args.resident_session, args.resident_dir)
                   if args.resident_session is not None else None)
    service = LocalService(
        DetectionController(root, alert_hub=alert_hub),
        WifiBackend(interface=args.wifi_interface),
        port=args.port,
        application=application,
        lan_port=args.lan_port or None,
        alert_hub=alert_hub,
    )
    stopped = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda number, frame: stopped.set())
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    failed = False
    result = {'detection_stopped': False, 'wifi_stopped': False,
              'alert_stopped': alert_hub is None}
    receipts = ReceiptCoalescer()

    def receipt(force=False):
        if args.lifecycle_file is not None:
            value = service.progress()
            value.update(session=args.resident_session, identity=process_identity(os.getpid()),
                         state='failed' if failed else value['state'])
            receipts.publish(value, lambda item: workspace.write_json('service-state.json', item),
                             force=force)

    try:
        _private_file(args.token_file, service.token + '\n')
        service.start()
        _private_file(args.ready_file, json.dumps({'host': '127.0.0.1', 'port': service.port,
                                                'lan_port': service.lan.port if service.lan else None,
                                                'alert_port': service.progress()['alert_port'],
                                                'alert_viewer_alive': service.progress()['alert_viewer_alive'],
                                                'pid': os.getpid(), 'ready': True,
                                                'identity': process_identity(os.getpid()),
                                                'session': args.resident_session,
                                                'lease': {'device': os.fstat(lease.fileno()).st_dev,
                                                          'inode': os.fstat(lease.fileno()).st_ino}}))
        while not stopped.is_set() and not service.shutdown_requested.is_set():
            if service.thread is None or not service.thread.is_alive():
                failed = True
                break
            receipt()
            stopped.wait(.25)
    except BaseException:
        failed = True
    finally:
        service.begin_shutdown()
        # Keep HTTP/stopping progress and the inherited lease until actual
        # native/WiFi owners end. No timeout converts a live worker to success.
        while not all(result.values()):
            result = service.close(timeout=.25)
            try:
                receipt()
            except (OSError, ValueError):
                failed = True
            if not all(result.values()):
                time.sleep(.05)
        try:
            receipt(force=True)
        except (OSError, ValueError):
            failed = True
        if workspace._stat('token') is not None:
            os.unlink('token', dir_fd=workspace.descriptor)
        lease.close()
        workspace.close()
        coordinator.close()
    return 0 if all(result.values()) and not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
