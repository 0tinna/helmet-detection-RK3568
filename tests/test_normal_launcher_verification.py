"""Host regressions for normal-launcher evidence and controlled IO boundaries."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import hashlib
import signal
from unittest import mock
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
HELPERS = ROOT / 'docs/experiments/2026-11-15'


def load(name):
    path = HELPERS / (name + '.py')
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


keyboard = load('native_keyboard')
verifier = load('verify_normal_launcher')


def status(start, finish, count=100):
    return {'method': 'GET', 'route': '/api/status', 'status': 200,
            'request_started': start, 'response_finished': finish,
            'body': {'detection': {'running': True, 'valid': True, 'state': 'healthy',
                'generation': 'same-run', 'source_epoch': 1,
                'selection': {'model': 'helmet_416x416.rknn',
                              'source': 'construction-replay.avi', 'profile': 'responsive'},
                'counters': {'detected': count, 'output': count - 10,
                             'captured': count + 50, 'consumed': count, 'read_failure': 0,
                             'detection_failure': 0, 'overwritten': 0, 'active_skip': 0,
                             'displayed': 0, 'output_overwritten': 0}}}}


class IntervalTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(verifier, 'new normal launcher verification helper is missing')

    def test_http_uncertainty_uses_lower_duration_and_upper_fps_denominator(self):
        result = verifier.validate_interval(status(10, 12), status(1812, 1815, 18130))
        self.assertEqual(result['duration_lower_seconds'], 1800)
        self.assertEqual(result['duration_upper_seconds'], 1805)
        self.assertEqual(result['counter_deltas'], {'detected': 18030, 'output': 18030, 'captured': 18030})
        self.assertAlmostEqual(result['conservative_fps']['detected'], 18030 / 1805)
        self.assertTrue(result['thirty_minutes_observed'])

    def test_short_scope_does_not_claim_thirty_minutes(self):
        result = verifier.validate_interval(status(10, 12), status(40, 42, 400))
        self.assertEqual(result['duration_lower_seconds'], 28)
        self.assertFalse(result['thirty_minutes_observed'])

    def test_rejects_nan_overlap_and_reverse_http_time(self):
        for first, last in ((status(float('nan'), 12), status(40, 42)),
                            (status(10, 12), status(11, 13)),
                            (status(12, 10), status(40, 42))):
            with self.subTest(first=first):
                with self.assertRaises(ValueError):
                    verifier.validate_interval(first, last)

    def test_rejects_any_counter_regression_not_just_fps_counters(self):
        first, last = status(10, 12), status(40, 42, 400)
        first['body']['detection']['counters']['overwritten'] = 2
        with self.assertRaises(ValueError):
            verifier.validate_interval(first, last)

    def test_rejects_identity_selection_fault_and_missing_counter(self):
        for field, value in (('generation', 'restart'), ('source_epoch', 2),
                             ('selection', {}), ('state', 'fault'), ('valid', False)):
            last = status(40, 42, 400)
            last['body']['detection'][field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    verifier.validate_interval(status(10, 12), last)
        last = status(40, 42, 400)
        del last['body']['detection']['counters']['output']
        with self.assertRaises(ValueError):
            verifier.validate_interval(status(10, 12), last)


class FakeDevice:
    """Only the Linux kernel boundary is replaced; encoding/lifecycle stay real."""
    def __init__(self, short_on=None):
        self.writes = []
        self.ioctls = []
        self.closed = []
        self.short_on = short_on

    def open(self, path, flags):
        return 41

    def write(self, fd, data):
        self.writes.append(data)
        return len(data) - 1 if len(self.writes) == self.short_on else len(data)

    def ioctl(self, fd, operation, value=0):
        self.ioctls.append(operation)

    def close(self, fd):
        self.closed.append(fd)


class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(keyboard, 'new temporary keyboard helper is missing')

    def test_codec_decodes_literal_linux_32_and_64_bit_events(self):
        for word_bytes, raw in ((4, struct.pack('<iiHHi', 7, 250000, 1, 15, 1)),
                                (8, struct.pack('<qqHHi', 7, 250000, 1, 15, 1))):
            self.assertEqual(keyboard.decode_event(raw, word_bytes), (7, 250000, 1, 15, 1))
            self.assertEqual(keyboard.encode_event(1, 15, 1, 7.25, word_bytes), raw)

    def test_rejects_arbitrary_keys_and_system_chords_before_device_io(self):
        io = FakeDevice()
        with keyboard.VirtualKeyboard(lambda: None, io=io, device_lookup=lambda name: []) as device:
            for key in ('A', 'a;reboot', 'KEY_POWER', '', 'WiFi'):
                with self.assertRaises(ValueError):
                    device.press(key)
            for modifier, key in (('Ctrl', 'Alt'), ('Alt', 'Tab'), ('Shift', 'Enter')):
                with self.assertRaises(ValueError):
                    device.chord(modifier, key)
        self.assertEqual(len(io.writes), 1)  # device descriptor only

    def test_shift_tab_orders_modifiers_and_releases_all_keys(self):
        io = FakeDevice()
        with keyboard.VirtualKeyboard(lambda: None, io=io, device_lookup=lambda name: []) as device:
            device.chord('Shift', 'Tab')
        events = [keyboard.decode_event(raw) for raw in io.writes[1:]]
        self.assertEqual([(e[2], e[3], e[4]) for e in events],
                         [(1, 42, 1), (1, 15, 1), (0, 0, 0),
                          (1, 15, 0), (1, 42, 0), (0, 0, 0)])
        self.assertIn(keyboard.UI_DEV_DESTROY, io.ioctls)
        self.assertEqual(io.closed, [41])

    def test_short_write_releases_potentially_pressed_key_and_destroys_device(self):
        io = FakeDevice(short_on=2)
        with self.assertRaises(OSError):
            with keyboard.VirtualKeyboard(lambda: None, io=io, device_lookup=lambda name: []) as device:
                device.press('Tab')
        events = [keyboard.decode_event(raw) for raw in io.writes[2:]]
        self.assertIn((1, 15, 0), [(e[2], e[3], e[4]) for e in events])
        self.assertIn(keyboard.UI_DEV_DESTROY, io.ioctls)
        self.assertEqual(io.closed, [41])

    def test_lost_owner_blocks_creation_and_later_input(self):
        io = FakeDevice()
        def gone():
            raise RuntimeError('owner identity changed')
        with self.assertRaises(RuntimeError):
            with keyboard.VirtualKeyboard(gone, io=io, device_lookup=lambda name: []):
                pass
        self.assertEqual(io.writes, [])
        checks = []
        def owner():
            checks.append(True)
            if len(checks) > 1:
                raise RuntimeError('owner identity changed')
        with self.assertRaises(RuntimeError):
            with keyboard.VirtualKeyboard(owner, io=io, device_lookup=lambda name: []) as device:
                device.press('Tab')
        self.assertEqual(len(io.writes), 1)


class OwnershipAndTerminalTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(verifier, 'new normal launcher verification helper is missing')

    def test_proc_stat_parser_handles_parentheses_and_starttime(self):
        fields = ['S', '5'] + ['0'] * 17 + ['1234', '0', '29']
        row = verifier.parse_proc_stat('71 (qmlscene (real)) ' + ' '.join(fields))
        self.assertEqual(row['pid'], 71)
        self.assertEqual(row['starttime'], 1234)
        self.assertEqual(row['rss_pages'], 29)

    def test_pid_reuse_and_wrong_service_identity_fail_before_input(self):
        identities = {'launcher': {'pid': 71, 'starttime': 1234},
                      'service': {'pid': 72, 'starttime': 1235},
                      'ui': {'pid': 73, 'starttime': 1236}}
        live = {value['pid']: {**value, 'state': 'S'} for value in identities.values()}
        verifier.validate_live_identities(identities, live.get)
        live[72]['starttime'] = 9000
        with self.assertRaises(RuntimeError):
            verifier.validate_live_identities(identities, live.get)
        live[72] = None
        with self.assertRaises(RuntimeError):
            verifier.validate_live_identities(identities, live.get)

    def test_session_owner_requires_matching_ready_receipt_and_exact_ui_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            ready = {'ready': True, 'host': '127.0.0.1', 'port': 34567, 'pid': 72}
            receipt = {'launcher': 71, 'service': 72, 'ui': 73, 'tab': 'detection', 'offline_localhost': True}
            (session / 'ready.json').write_text(json.dumps(ready))
            (session / 'processes.json').write_text(json.dumps(receipt))
            live = {71: {'pid': 71, 'starttime': 1001, 'state': 'S', 'ppid': 1,
                         'argv': ['python3', str(verifier.APP_ROOT / 'src/app/launch.py'), '--root', str(verifier.APP_ROOT)]},
                    72: {'pid': 72, 'starttime': 1002, 'state': 'S', 'ppid': 71,
                         'argv': ['python3', str(verifier.APP_ROOT / 'src/app/service.py'), '--root', str(verifier.APP_ROOT)]},
                    73: {'pid': 73, 'starttime': 1003, 'state': 'S', 'ppid': 71,
                         'argv': ['qmlscene', str(session / 'App.qml')]}}
            owner = verifier.SessionOwner(session, 71, reader=live.get)
            owner.assert_alive()
            live[73]['argv'] = ['qmlscene', str(session / 'other.qml')]
            with self.assertRaises(RuntimeError):
                owner.assert_alive()
            live[73]['argv'] = ['qmlscene', str(session / 'App.qml')]
            ready['pid'] = 99
            (session / 'ready.json').write_text(json.dumps(ready))
            with self.assertRaises(RuntimeError):
                owner.assert_alive()
            with self.assertRaises(RuntimeError):
                verifier.SessionOwner(session, 71, reader=live.get)

    def test_terminal_rejects_live_owner_private_material_or_failed_exit(self):
        record = {'wrapper_exit': 0, 'exit_receipt': {'service_exit': 0, 'ui_exit': 0,
                    'cleanup_completed': True}, 'private_files_remaining': [],
                  'processes': {role: {'alive': False} for role in ('launcher', 'service', 'ui')},
                  'uinput_remaining': [], 'governor_before': 'rknpu_ondemand',
                  'governor_after': 'rknpu_ondemand', 'stop_confirmed': True}
        verifier.validate_terminal(record)
        for key, value in (('private_files_remaining', ['token']), ('wrapper_exit', 1),
                           ('uinput_remaining', ['event7']), ('stop_confirmed', False),
                           ('governor_after', 'performance')):
            bad = copy.deepcopy(record)
            bad[key] = value
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    verifier.validate_terminal(bad)
        bad = copy.deepcopy(record)
        bad['processes']['service']['alive'] = True
        with self.assertRaises(ValueError):
            verifier.validate_terminal(bad)

    def test_keys_require_probe_provenance_and_reject_close_during_configuration(self):
        good = {'probe_run_id': 'probe-1', 'probe_screen_sha256': 'a' * 64,
                'actions': [{'key': 'Home'}, {'modifier': 'Shift', 'key': 'Tab'}, {'key': 'Enter'}]}
        self.assertEqual(len(verifier.validate_keys(good)), 3)
        for value in ({'actions': [{'key': 'Enter'}]},
                      {**good, 'actions': [{'modifier': 'Alt', 'key': 'F4'}]},
                      {**good, 'actions': [{'key': 'Tab', 'pause_seconds': float('nan')}]},
                      {**good, 'actions': [{'key': 'A'}]}):
            with self.assertRaises(ValueError):
                verifier.validate_keys(value)

    def test_safe_output_never_reuses_or_escapes_existing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = verifier.create_output('run-1', base)
            self.assertEqual(result, base / 'run-1')
            (result / 'preserved').write_text('history')
            with self.assertRaises(FileExistsError):
                verifier.create_output('run-1', base)
            self.assertEqual((result / 'preserved').read_text(), 'history')
            for bad in ('../escape', '', '.hidden', 'name/child', 'x;cmd', '汉字'):
                with self.assertRaises(ValueError):
                    verifier.create_output(bad, base)

    def test_render_requires_actual_decodable_png_and_saved_valid_preview(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            screen = Path(tmp) / 'screen.png'
            receipt = {'saved': True, 'valid': True, 'previewVisible': True, 'tab': 0}
            with self.assertRaises(ValueError):
                verifier.validate_render(screen, receipt, 'run')
            screen.write_bytes(b'not an image')
            with self.assertRaises(ValueError):
                verifier.validate_render(screen, receipt, 'run')
            cv2.imwrite(str(screen), np.zeros((4, 6, 3), dtype=np.uint8))
            self.assertEqual(verifier.validate_render(screen, receipt, 'run')['dimensions'], [6, 4])
            for field in ('saved', 'valid', 'previewVisible'):
                with self.assertRaises(ValueError):
                    verifier.validate_render(screen, {**receipt, field: False}, 'run')
            verifier.validate_render(screen, {**receipt, 'valid': False, 'previewVisible': False}, 'probe')


class HTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(verifier, 'new normal launcher verification helper is missing')

    def test_real_http_boundary_limits_routes_and_preserves_503_without_token(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                calls.append((self.command, self.path, self.headers.get('X-Helmet-Token'), None))
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"error":"fresh detection preview unavailable"}')
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                calls.append((self.command, self.path, self.headers.get('X-Helmet-Token'), raw))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"stopped":true}')
        server = HTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            client = verifier.LocalAPI(server.server_port, 'private-test-secret', lambda: None)
            row = client.request('GET', '/api/preview')
            self.assertEqual(row['status'], 503)
            self.assertEqual(row['body'], {'error': 'fresh detection preview unavailable'})
            self.assertLessEqual(row['request_started'], row['response_finished'])
            self.assertNotIn('private-test-secret', json.dumps(row))
            for method, route, payload in (('POST', '/api/start', {}), ('GET', '/api/wifi/status', None),
                                            ('POST', '/api/stop', {'model': 'x'})):
                with self.assertRaises(ValueError):
                    client.request(method, route, payload)
            client.request('POST', '/api/stop', {})
            self.assertEqual(calls, [('GET', '/api/preview', 'private-test-secret', None),
                                     ('POST', '/api/stop', 'private-test-secret', b'{}')])
        finally:
            server.shutdown()
            worker.join()
            server.server_close()

    def test_expired_preview_cannot_establish_business_boundary(self):
        body = {'valid': True, 'frame_id': 7, 'captured_at': 10, 'source_epoch': 1,
                'generation': 'same-run', 'source_age_ms': 999, 'remaining_ttl_ms': 1,
                'jpeg_base64': base64.b64encode(b'broken').decode('ascii')}
        row = {'status': 200, 'request_started': 10.99, 'response_finished': 11.01, 'body': body}
        with self.assertRaises(ValueError):
            verifier.validate_preview(row, status(10.9, 10.95)['body']['detection'])


class CompleteEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(verifier, 'new normal launcher verification helper is missing')

    def refresh_inventory(self, directory, manifest=None):
        manifest = json.loads((directory / 'manifest.json').read_text()) if manifest is None else manifest
        manifest['evidence_sha256'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.iterdir() if path.is_file() and path.name not in ('manifest.json', 'verification.json')}
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        return manifest

    def write_http(self, directory, rows):
        for row in rows:
            if row.get('body') is not None:
                row['response_text'] = json.dumps(row['body'])
        (directory / 'http.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.refresh_inventory(directory)

    def fixture(self, directory):
        """Hand-constructed two-second short observation, not hardware evidence."""
        import cv2
        import numpy as np
        pixels = np.zeros((4, 6, 3), dtype=np.uint8)
        cv2.imwrite(str(directory / 'screen.png'), pixels)
        ok, encoded = cv2.imencode('.jpg', pixels)
        self.assertTrue(ok)
        jpeg = encoded.tobytes()
        (directory / 'preview-0.jpg').write_bytes(jpeg)
        preview = {'index': 0, 'method': 'GET', 'route': '/api/preview', 'status': 200,
                   'request_started': 9.9, 'response_finished': 10.0,
                   'body': {'valid': True, 'frame_id': 7, 'captured_at': 9.8,
                            'source_epoch': 1, 'generation': 'same-run', 'source_age_ms': 150,
                            'remaining_ttl_ms': 850, 'jpeg_base64': base64.b64encode(jpeg).decode('ascii')},
                   'jpeg_file': 'preview-0.jpg'}
        rows = [preview, {**status(10.1, 10.2), 'index': 1},
                {**status(11.1, 11.2, 110), 'index': 2},
                {**status(12.2, 12.3, 120), 'index': 3},
                {'index': 4, 'method': 'POST', 'route': '/api/stop', 'status': 200,
                 'request_started': 12.4, 'response_finished': 12.5, 'body': {'stopped': True}, 'request_body': {}},
                {'index': 5, 'method': 'GET', 'route': '/api/status', 'status': 200,
                 'request_started': 12.6, 'response_finished': 12.7, 'body': {'detection': {'running': False}}}]
        with (directory / 'http.jsonl').open('w') as stream:
            for row in rows:
                row['response_text'] = json.dumps(row['body'])
                stream.write(json.dumps(row) + '\n')
        identities = {role: {'pid': 100 + index, 'starttime': 500 + index}
                      for index, role in enumerate(('launcher', 'service', 'ui', 'observer'))}
        resources = [{'request_started': start, 'response_finished': start + .01,
                      'processes': {role: {**identity, 'cpu_ticks': index + 10,
                                          'rss_bytes': 100000, 'threads': 2, 'fds': 5}
                                    for role, identity in identities.items()},
                      'temperature': [{'path': '/sys/class/thermal/thermal_zone0/temp', 'value': '45000'}],
                      'npu_frequency': '300000000', 'governor': 'rknpu_ondemand'}
                     for index, start in enumerate((10.2, 11.2, 12.3))]
        with (directory / 'resources.jsonl').open('w') as stream:
            for row in resources:
                stream.write(json.dumps(row) + '\n')
        terminal = {'wrapper_exit': 0, 'exit_receipt': {'service_exit': 0, 'ui_exit': 0,
                    'cleanup_completed': True}, 'private_files_remaining': [],
                    'processes': {role: {**identities[role], 'alive': False, 'current': None}
                                  for role in ('launcher', 'service', 'ui')},
                    'uinput_remaining': [], 'governor_before': 'rknpu_ondemand',
                    'governor_after': 'rknpu_ondemand', 'stop_confirmed': True}
        (directory / 'terminal.json').write_text(json.dumps(terminal))
        (directory / 'exit.json').write_text(json.dumps(terminal['exit_receipt']))
        (directory / 'ready.json').write_text(json.dumps({'host': '127.0.0.1', 'port': 34567,
            'pid': 101, 'ready': True}))
        (directory / 'processes.json').write_text(json.dumps({'launcher': 100, 'service': 101,
            'ui': 102, 'tab': 'detection', 'offline_localhost': True}))
        (directory / 'launcher.log').write_text('normal wrapper\n')
        (directory / 'service.log').write_text('normal service\n')
        (directory / 'ui.log').write_text('qml: APP_RENDER {"saved":true,"tab":0,"valid":true,"previewVisible":true}\n')
        keys = {'probe_run_id': 'probe-1', 'probe_screen_sha256': 'a' * 64,
                'actions': [{'key': 'Home'}, {'key': 'Enter'}]}
        (directory / 'input.jsonl').write_text(''.join(json.dumps({'index': index, 'monotonic': 8 + index * .15,
            'action': action, 'device_name': 'helmet-normal-fixture',
            'owner_identities': {role: identities[role] for role in ('launcher', 'service', 'ui')}}) + '\n'
            for index, action in enumerate(keys['actions'])))
        manifest = {'kind': 'normal-launcher-observation-v1', 'mode': 'run', 'run_id': 'fixture',
                    'command': ['/opt/ui/src/apps/helmet_app', '--smoke-seconds', '44'],
                    'wrapper_target': '/userdata/helmet-validation/20261115-native-app-v2/vendor-entry/helmet_app',
                    'selection': verifier.SELECTION, 'identities': identities, 'failures': [],
                    'keys': keys, 'uinput_name': 'helmet-normal-fixture',
                    'probe_evidence': {'passed': True, 'mode': 'probe', 'run_id': 'probe-1', 'render': {'sha256': 'a' * 64}},
                    'source_hashes_before': {'src/app/service.py': 'a' * 64},
                    'source_hashes_after': {'src/app/service.py': 'a' * 64},
                    'measurement': {'first_status_index': 1, 'last_status_index': 3,
                                    'first_fresh_preview_index': 0, 'requested_duration_seconds': 2},
                    'evidence_sha256': {}}
        self.refresh_inventory(directory, manifest)
        return manifest, rows

    def test_complete_short_evidence_passes_with_thirty_minute_gate_false(self):
        self.assertTrue(hasattr(verifier, 'verify_directory'), 'complete offline verifier is missing')
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.fixture(directory)
            result = verifier.verify_directory(directory)
            self.assertTrue(result['passed'], result)
            self.assertEqual(result['scope'], 'short normal-launcher replay observation')
            self.assertFalse(result['gates']['thirty_minutes_observed'])

    def test_read_failure_at_baseline_or_between_polls_cannot_pass(self):
        self.assert_failure_counter_rejected('read_failure')

    def test_detection_failure_at_baseline_or_between_polls_cannot_pass(self):
        self.assert_failure_counter_rejected('detection_failure')

    def assert_failure_counter_rejected(self, counter):
        for stage in ('baseline', 'growth', 'startup'):
            with self.subTest(counter=counter, stage=stage), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                manifest, rows = self.fixture(directory)
                for index in (1, 2, 3):
                    rows[index]['body']['detection']['counters'][counter] = 1 if stage == 'baseline' or index > 1 else 0
                if stage == 'startup':
                    early = status(9, 9.1)
                    early['body']['detection']['valid'] = False
                    early['body']['detection']['state'] = 'degraded'
                    early['body']['detection']['counters'][counter] = 1
                    for index in (1, 2, 3):
                        rows[index]['body']['detection']['counters'][counter] = 0
                    rows.insert(0, early)
                    for index, row in enumerate(rows):
                        row['index'] = index
                    manifest['measurement'] = {'first_status_index': 2, 'last_status_index': 4,
                                              'first_fresh_preview_index': 1, 'requested_duration_seconds': 2}
                    (directory / 'manifest.json').write_text(json.dumps(manifest))
                self.write_http(directory, rows)
                result = verifier.verify_directory(directory)
                self.assertFalse(result['passed'], result)
                self.assertIn(counter, ' '.join(result['failures']))

    def test_legitimate_drop_and_output_counters_are_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest, rows = self.fixture(directory)
            for offset, index in enumerate((1, 2, 3)):
                for name in ('overwritten', 'output_overwritten', 'active_skip'):
                    rows[index]['body']['detection']['counters'][name] = 2 + offset
            self.write_http(directory, rows)
            self.assertTrue(verifier.verify_directory(directory)['passed'])

    def test_hidden_middle_restart_failed_http_and_wrong_selection_never_pass(self):
        self.assertTrue(hasattr(verifier, 'verify_directory'), 'complete offline verifier is missing')
        for mutation in ('restart', 'http_failure', 'selection'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                manifest, rows = self.fixture(directory)
                if mutation == 'restart':
                    rows[2]['body']['detection']['generation'] = 'other-run'
                elif mutation == 'http_failure':
                    rows[2]['status'], rows[2]['body'] = 503, {'error': 'unavailable'}
                else:
                    rows[2]['body']['detection']['selection']['model'] = 'helmet_640x640.rknn'
                self.write_http(directory, rows)
                self.assertFalse(verifier.verify_directory(directory)['passed'])

    def test_failed_exit_missing_resources_private_export_and_post_start_fail(self):
        self.assertTrue(hasattr(verifier, 'verify_directory'), 'complete offline verifier is missing')
        for mutation in ('exit', 'resources', 'token', 'start', 'duration', 'stop_payload'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                manifest, rows = self.fixture(directory)
                if mutation == 'exit':
                    terminal = json.loads((directory / 'terminal.json').read_text())
                    terminal['wrapper_exit'] = 1
                    (directory / 'terminal.json').write_text(json.dumps(terminal))
                elif mutation == 'resources':
                    (directory / 'resources.jsonl').unlink()
                elif mutation == 'token':
                    (directory / 'token').write_text('should never be exported')
                elif mutation == 'duration':
                    manifest['measurement']['requested_duration_seconds'] = 1800
                    (directory / 'manifest.json').write_text(json.dumps(manifest))
                elif mutation == 'start':
                    rows[4]['route'] = '/api/start'
                    self.write_http(directory, rows)
                else:
                    rows[4]['request_body'] = {'arbitrary': 'change'}
                    self.write_http(directory, rows)
                self.refresh_inventory(directory)
                self.assertFalse(verifier.verify_directory(directory)['passed'])

    def test_normal_run_timer_reserves_navigation_measurement_and_tail_with_limit(self):
        self.assertTrue(hasattr(verifier, 'smoke_duration'), 'normal timer budget helper is missing')
        self.assertEqual(verifier.smoke_duration(1800, [{'key': 'Tab', 'pause_seconds': 1}]), 1841)
        with self.assertRaises(ValueError):
            verifier.smoke_duration(3580, [{'key': 'Tab'}])

    def test_ui_start_requires_recorded_inputs_and_verified_probe_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest, rows = self.fixture(directory)
            (directory / 'input.jsonl').write_text('')
            self.refresh_inventory(directory)
            self.assertFalse(verifier.verify_directory(directory)['passed'])
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest, rows = self.fixture(directory)
            manifest['probe_evidence']['render']['sha256'] = 'b' * 64
            (directory / 'manifest.json').write_text(json.dumps(manifest))
            self.assertFalse(verifier.verify_directory(directory)['passed'])

    def test_governor_change_inside_resources_fails_even_when_restored_at_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.fixture(directory)
            rows = [json.loads(line) for line in (directory / 'resources.jsonl').read_text().splitlines()]
            rows[1]['governor'] = 'performance'
            (directory / 'resources.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
            self.refresh_inventory(directory)
            self.assertFalse(verifier.verify_directory(directory)['passed'])

    def test_wrong_running_selection_before_measurement_cannot_be_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest, rows = self.fixture(directory)
            wrong = status(9, 9.1)
            wrong['body']['detection']['selection']['model'] = 'helmet_640x640.rknn'
            rows.insert(0, wrong)
            for index, row in enumerate(rows):
                row['index'] = index
            manifest['measurement'] = {'first_status_index': 2, 'last_status_index': 4,
                                      'first_fresh_preview_index': 1, 'requested_duration_seconds': 2}
            (directory / 'manifest.json').write_text(json.dumps(manifest))
            self.write_http(directory, rows)
            self.assertFalse(verifier.verify_directory(directory)['passed'])

    def test_sparse_status_preview_or_resource_sampling_cannot_claim_long_observation(self):
        for mutation in ('status', 'preview', 'resources'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                manifest, rows = self.fixture(directory)
                # Move all events after the initial baseline 20s later. The
                # interval alone still meets duration=2 but sampling has a gap.
                for row in rows[2:]:
                    row['request_started'] += 20
                    row['response_finished'] += 20
                if mutation == 'preview':
                    # Fill the status schedule, retaining just the old preview.
                    rows = rows[:2] + [{**status(11.1 + index, 11.2 + index, 110), 'index': index + 2}
                                     for index in range(21)] + rows[3:]
                for index, row in enumerate(rows):
                    row['index'] = index
                manifest['measurement']['last_status_index'] = len(rows) - 3
                (directory / 'manifest.json').write_text(json.dumps(manifest))
                self.write_http(directory, rows)
                if mutation != 'resources':
                    resources = [json.loads(line) for line in (directory / 'resources.jsonl').read_text().splitlines()]
                    dense = []
                    for index in range(23):
                        resource = copy.deepcopy(resources[0])
                        resource['request_started'], resource['response_finished'] = 10.2 + index, 10.21 + index
                        for process in resource['processes'].values():
                            process['cpu_ticks'] += index
                        dense.append(resource)
                    (directory / 'resources.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in dense))
                else:
                    resources = [json.loads(line) for line in (directory / 'resources.jsonl').read_text().splitlines()]
                    resources[-1]['request_started'], resources[-1]['response_finished'] = 32.3, 32.31
                    (directory / 'resources.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in resources))
                self.refresh_inventory(directory)
                self.assertFalse(verifier.verify_directory(directory)['passed'])

    def test_inventory_must_cover_all_required_raw_files_and_saved_jpegs(self):
        for mutation in ('missing', 'empty', 'partial', 'jpeg_omitted'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                manifest, rows = self.fixture(directory)
                if mutation == 'missing':
                    del manifest['evidence_sha256']
                elif mutation == 'empty':
                    manifest['evidence_sha256'] = {}
                elif mutation == 'partial':
                    del manifest['evidence_sha256']['resources.jsonl']
                else:
                    del manifest['evidence_sha256']['preview-0.jpg']
                (directory / 'manifest.json').write_text(json.dumps(manifest))
                self.assertFalse(verifier.verify_directory(directory)['passed'])

    def test_inventory_rejects_bad_sha_escaped_path_and_raw_mutation(self):
        for mutation in ('bad_sha', 'escaped_path', 'raw_mutation'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                manifest, rows = self.fixture(directory)
                if mutation == 'bad_sha':
                    manifest['evidence_sha256']['resources.jsonl'] = 'a' * 63
                elif mutation == 'escaped_path':
                    manifest['evidence_sha256']['../escape'] = 'a' * 64
                else:
                    (directory / 'service.log').write_text('mutated raw log')
                (directory / 'manifest.json').write_text(json.dumps(manifest))
                self.assertFalse(verifier.verify_directory(directory)['passed'])

    def test_raw_response_text_must_agree_with_body_and_error_outcome(self):
        for mutation in ('disagree', 'missing', 'malformed', 'false_error'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                manifest, rows = self.fixture(directory)
                if mutation == 'disagree':
                    rows[1]['response_text'] = '{"error":"contradicts body"}'
                elif mutation == 'missing':
                    del rows[1]['response_text']
                elif mutation == 'malformed':
                    rows[1]['response_text'] = '{broken'
                else:
                    rows[1]['error'] = 'http_or_decode_failure'
                (directory / 'http.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
                self.refresh_inventory(directory)
                result = verifier.verify_directory(directory)
                self.assertFalse(result['passed'], result)

    def test_manifest_and_derived_verification_are_explicit_inventory_exclusions(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            manifest, rows = self.fixture(directory)
            (directory / 'verification.json').write_text('{"passed":false,"untrusted_derived_result":true}')
            self.assertTrue(verifier.verify_directory(directory)['passed'])
            manifest['evidence_sha256']['manifest.json'] = 'a' * 64
            (directory / 'manifest.json').write_text(json.dumps(manifest))
            self.assertFalse(verifier.verify_directory(directory)['passed'])


class ObserverFinalizationTests(unittest.TestCase):
    """Inject only the completed board-owned boundary; publication stays real."""
    def setUp(self):
        self.assertTrue(hasattr(verifier, '_observe_owned'), 'normal observer lacks a guarded final-publication boundary')
        verifier.STOP_REQUESTED = False

    def boundary(self, directory, during_cleanup=None):
        def completed(args):
            manifest, rows = CompleteEvidenceTests().fixture(directory)
            # Successful normal-stop/terminal receipts are real verifier inputs;
            # no Qt, Linux device, or SDK behavior is claimed by this boundary.
            if during_cleanup is not None:
                verifier.signal_stop(during_cleanup, None)
            return directory, manifest
        return completed

    def assert_retained_interrupt(self, directory):
        manifest = json.loads((directory / 'manifest.json').read_text())
        self.assertIn('observer_interrupted', manifest['failures'])
        self.assertFalse(json.loads((directory / 'verification.json').read_text())['passed'])
        terminal = json.loads((directory / 'terminal.json').read_text())
        self.assertTrue(terminal['exit_receipt']['cleanup_completed'])
        self.assertTrue(terminal['stop_confirmed'])

    def test_interrupt_during_successful_normal_cleanup_is_retained_and_nonzero(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                with mock.patch.object(verifier, '_observe_owned', side_effect=self.boundary(directory, signum)), redirect_stdout(io.StringIO()):
                    code = verifier.observe(SimpleNamespace(run_id='fixture'))
                self.assertEqual(code, 1)
                self.assert_retained_interrupt(directory)

    def test_interrupt_during_last_verification_write_is_retained_and_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            original_write = verifier.write_json
            injected = []
            def write_and_interrupt(path, value):
                original_write(path, value)
                if Path(path).name == 'verification.json' and not injected:
                    injected.append(True)
                    verifier.signal_stop(signal.SIGTERM, None)
            with mock.patch.object(verifier, '_observe_owned', side_effect=self.boundary(directory)), mock.patch.object(verifier, 'write_json', side_effect=write_and_interrupt), redirect_stdout(io.StringIO()):
                code = verifier.observe(SimpleNamespace(run_id='fixture'))
            self.assertEqual(code, 1)
            self.assert_retained_interrupt(directory)

    def test_final_artifact_io_error_restores_original_signal_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
            original_write = verifier.write_json
            def fail_derived_write(path, value):
                if Path(path).name == 'verification.json':
                    raise OSError('controlled publication IO failure')
                original_write(path, value)
            with mock.patch.object(verifier, '_observe_owned', side_effect=self.boundary(directory)), mock.patch.object(verifier, 'write_json', side_effect=fail_derived_write), redirect_stdout(io.StringIO()):
                with self.assertRaises(OSError):
                    verifier.observe(SimpleNamespace(run_id='fixture'))
            self.assertEqual({sig: signal.getsignal(sig) for sig in original_handlers}, original_handlers)

    def test_normal_final_publication_passes_without_hashing_derived_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with mock.patch.object(verifier, '_observe_owned', side_effect=self.boundary(directory)), redirect_stdout(io.StringIO()):
                code = verifier.observe(SimpleNamespace(run_id='fixture'))
            self.assertEqual(code, 0)
            manifest = json.loads((directory / 'manifest.json').read_text())
            self.assertNotIn('manifest.json', manifest['evidence_sha256'])
            self.assertNotIn('verification.json', manifest['evidence_sha256'])
            self.assertTrue(verifier.verify_directory(directory)['passed'])

    def test_interrupt_caught_during_handler_restoration_is_retained_and_nonzero(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
                real_signal = signal.signal
                injected = []
                def restore_and_interrupt(sig, handler):
                    if handler is not verifier.signal_stop and not injected:
                        injected.append(True)
                        installed = signal.getsignal(signum)
                        self.assertIs(installed, verifier.signal_stop)
                        installed(signum, None)
                    return real_signal(sig, handler)
                with mock.patch.object(verifier, '_observe_owned', side_effect=self.boundary(directory)), mock.patch.object(signal, 'signal', side_effect=restore_and_interrupt), redirect_stdout(io.StringIO()):
                    code = verifier.observe(SimpleNamespace(run_id='fixture'))
                self.assertTrue(injected)
                self.assertEqual({sig: signal.getsignal(sig) for sig in original_handlers}, original_handlers)
                self.assertEqual(code, 1)
                self.assert_retained_interrupt(directory)


class ProbeCalibrationTests(unittest.TestCase):
    """Calibration policy and startup IO boundaries; no board/device claims."""
    def setUp(self):
        verifier.STOP_REQUESTED = False

    def document(self, actions):
        return {'probe_run_id': 'prior-probe', 'probe_screen_sha256': 'a' * 64, 'actions': actions}

    def test_probe_rejects_activation_keys_accepted_by_formal_run(self):
        for key in ('Space', 'Enter'):
            with self.subTest(key=key):
                document = self.document([{'key': key}])
                self.assertEqual(verifier.validate_keys(document), [{'key': key}])
                with self.assertRaises(ValueError):
                    verifier.validate_probe_keys(document)

    def test_probe_safe_keys_keep_shift_tab_pauses_and_reject_other_chords(self):
        self.assertTrue(hasattr(verifier, 'validate_probe_keys'), 'probe calibration validator missing')
        actions = [{'key': key, 'pause_seconds': 2} for key in ('Tab', 'Home', 'Up', 'Down', 'Escape')]
        actions += [{'modifier': 'Shift', 'key': 'Tab'}]
        self.assertEqual(verifier.validate_probe_keys(self.document(actions)), actions)
        for action in ({'modifier': 'Alt', 'key': 'F4'}, {'modifier': 'Shift', 'key': 'Enter'}, {'key': 'A'}):
            with self.assertRaises(ValueError):
                verifier.validate_probe_keys(self.document([action]))

    def test_bound_calibration_and_bad_binding_use_existing_probe_proof(self):
        self.assertTrue(hasattr(verifier, 'load_bound_keys'), 'shared actual-probe binding missing')
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = directory / 'keys.json'
            actions = [{'key': 'Escape', 'pause_seconds': 2}, {'key': 'Home'}]
            path.write_text(json.dumps(self.document(actions)))
            proof = {'passed': True, 'mode': 'probe', 'run_id': 'prior-probe', 'render': {'sha256': 'a' * 64}}
            with mock.patch.object(verifier, 'OUTPUT_ROOT', directory), mock.patch.object(verifier, 'verify_directory', return_value=proof):
                document, actual_actions, actual_proof = verifier.load_bound_keys(path, probe=True)
                self.assertEqual(actual_actions, actions)
                self.assertEqual(actual_proof, proof)
            for bad in ({**proof, 'passed': False}, {**proof, 'mode': 'run'}, {**proof, 'render': {'sha256': 'b' * 64}}):
                with mock.patch.object(verifier, 'OUTPUT_ROOT', directory), mock.patch.object(verifier, 'verify_directory', return_value=bad):
                    with self.assertRaises(ValueError):
                        verifier.load_bound_keys(path, probe=True)

    def test_legacy_tab_cli_and_keys_only_cli_are_both_supported(self):
        observed = []
        def consume(args):
            observed.append((args.tabs, args.smoke_seconds, getattr(args, 'keys', None)))
            return 0
        with mock.patch.object(verifier, 'observe', side_effect=consume), redirect_stderr(io.StringIO()):
            self.assertEqual(verifier.main(['probe', '--run-id', 'legacy', '--tabs', '3']), 0)
            try:
                code = verifier.main(['probe', '--run-id', 'calibrated', '--keys', 'keys.json'])
            except SystemExit as error:
                code = error.code
            self.assertEqual(code, 0)
        self.assertEqual(observed, [(3, 8, None), (0, 8, Path('keys.json'))])

    def test_probe_budget_reserves_native_capture_and_stop_margin(self):
        self.assertTrue(hasattr(verifier, 'validate_probe_budget'), 'probe action timer budget missing')
        actions = [{'key': 'Escape', 'pause_seconds': 2}, {'key': 'Escape', 'pause_seconds': 2}]
        verifier.validate_probe_budget(30, actions)
        with self.assertRaises(ValueError):
            verifier.validate_probe_budget(5, actions)

    def bootstrap(self, stack, directory, popen):
        """Replace only host-incompatible startup IO, stopping at vendor spawn."""
        app = directory / 'app'
        (app / 'app-state').mkdir(parents=True)
        outputs = directory / 'runs'
        outputs.mkdir()
        npu = directory / 'npu'
        npu.mkdir()
        (npu / 'governor').write_text('rknpu_ondemand')
        create = verifier.create_output
        stack.enter_context(mock.patch.object(verifier, 'APP_ROOT', app))
        stack.enter_context(mock.patch.object(verifier, 'NPU_ROOT', npu))
        stack.enter_context(mock.patch.object(verifier, 'preflight', return_value=None))
        stack.enter_context(mock.patch.object(verifier, 'create_output', side_effect=lambda run_id: create(run_id, outputs)))
        stack.enter_context(mock.patch.object(verifier, 'source_hashes', return_value={'source': 'a' * 64}))
        stack.enter_context(mock.patch.object(verifier, 'proc_read', return_value={'pid': 100, 'starttime': 1}))
        stack.enter_context(mock.patch.object(verifier.os, 'uname', return_value=('host',) * 5, create=True))
        stack.enter_context(mock.patch.object(verifier.os, 'sysconf', return_value=100, create=True))
        stack.enter_context(mock.patch.object(verifier.subprocess, 'Popen', side_effect=popen))
        return outputs

    def args(self, **changes):
        return SimpleNamespace(**{'mode': 'probe', 'run_id': 'cold', 'tabs': 0, 'smoke_seconds': 8,
                                  'keys': None, **changes})

    def test_decoder_imports_complete_before_vendor_process_spawn(self):
        events = []
        class SpawnBoundary(BaseException):
            pass
        def popen(*args, **kwargs):
            events.append('spawn')
            raise SpawnBoundary()
        original_import = verifier.importlib.import_module
        def observed_import(name):
            module = original_import(name)
            if name in ('cv2', 'numpy'):
                events.append(name)
            return module
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            outputs = self.bootstrap(stack, Path(tmp), popen)
            stack.enter_context(mock.patch.object(verifier.importlib, 'import_module', side_effect=observed_import))
            with self.assertRaises(SpawnBoundary):
                verifier._observe_owned(self.args())
            self.assertEqual(events, ['cv2', 'numpy', 'spawn'])
            manifest = json.loads((outputs / 'cold/manifest.json').read_text())
            preparation = manifest['decoder_preparation']
            self.assertLessEqual(preparation['request_started'], preparation['response_finished'])
            self.assertLessEqual(preparation['response_finished'], manifest['observer_started'])
            self.assertTrue(preparation['versions']['cv2'])
            self.assertTrue(preparation['versions']['numpy'])

    def test_decoder_preparation_failure_returns_failure_without_launch(self):
        launches = []
        class Exited:
            pid = 100
            def poll(self):
                return 1
        def popen(*args, **kwargs):
            launches.append(args[0])
            return Exited()
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            self.bootstrap(stack, Path(tmp), popen)
            stack.enter_context(mock.patch.object(verifier.importlib, 'import_module', side_effect=ImportError('controlled cold import failure')))
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                code = verifier.main(['probe', '--run-id', 'cold', '--tabs', '0'])
            self.assertEqual(code, 1)
            self.assertEqual(launches, [])

    def test_mixed_tabs_and_keys_are_rejected_before_any_launch(self):
        launches = []
        class Exited:
            pid = 100
            def poll(self):
                return 1
        def popen(*args, **kwargs):
            launches.append(args[0])
            return Exited()
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            self.bootstrap(stack, Path(tmp), popen)
            with self.assertRaises(ValueError):
                verifier._observe_owned(self.args(tabs=1, keys=Path(tmp) / 'not-read.json'))
            self.assertEqual(launches, [])

    def test_probe_idle_is_checked_after_each_action_and_unexpected_start_fails(self):
        self.assertTrue(hasattr(verifier, 'check_probe_action'), 'per-action idle check missing')
        calls = []
        class API:
            def request(self, method, route, **kwargs):
                calls.append((method, route, kwargs))
                return {'method': 'GET', 'route': '/api/status', 'status': 200,
                        'body': {'detection': {'running': len(calls) > 1, 'generation': None}}}
        observation = API()
        verifier.check_probe_action(observation, 0)
        with self.assertRaises(RuntimeError):
            verifier.check_probe_action(observation, 1)
        self.assertEqual(calls, [('GET', '/api/status', {'after_input_index': 0}),
                                 ('GET', '/api/status', {'after_input_index': 1})])

    def test_complete_calibrated_probe_requires_its_per_action_idle_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fixture = CompleteEvidenceTests()
            manifest, rows = fixture.fixture(directory)
            rows = rows[1:]
            (directory / 'preview-0.jpg').unlink()
            for index, row in enumerate(rows):
                row['index'] = index
                if row['route'] == '/api/status':
                    row['body']['detection'].update(running=False, generation=None, valid=False, state='idle')
            rows[0]['after_input_index'], rows[1]['after_input_index'] = 0, 1
            actions = [{'key': 'Escape', 'pause_seconds': 2}, {'key': 'Home'}]
            manifest.update(mode='probe', selection=None, keys=self.document(actions),
                probe_evidence={'passed': True, 'mode': 'probe', 'run_id': 'prior-probe', 'render': {'sha256': 'a' * 64}},
                decoder_preparation={'request_started': 6, 'response_finished': 6.1,
                                     'versions': {'cv2': 'host-fixture', 'numpy': 'host-fixture'}}, observer_started=7)
            inputs = [json.loads(line) for line in (directory / 'input.jsonl').read_text().splitlines()]
            for index, row in enumerate(inputs):
                row['action'], row['monotonic'] = actions[index], (8, 11)[index]
            (directory / 'input.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in inputs))
            (directory / 'ui.log').write_text('qml: APP_RENDER {"saved":true,"tab":0,"valid":false,"previewVisible":false}\n')
            (directory / 'manifest.json').write_text(json.dumps(manifest))
            fixture.write_http(directory, rows)
            result = verifier.verify_directory(directory)
            self.assertTrue(result['passed'], result)
            del rows[0]['after_input_index']
            fixture.write_http(directory, rows)
            result = verifier.verify_directory(directory)
            self.assertFalse(result['passed'])
            self.assertIn('ordered idle check', ' '.join(result['failures']))


if __name__ == '__main__':
    unittest.main()
