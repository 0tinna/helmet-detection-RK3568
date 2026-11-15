"""Host validation boundaries; no SDK, QML or hardware acceptance is exercised."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import warnings
import zipfile

PATH = Path(__file__).resolve().parents[1] / 'docs/experiments/2026-11-15/verify_resident_launcher.py'
if PATH.exists():
    SPEC = importlib.util.spec_from_file_location('resident_validation', PATH)
    V = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(V)
else:
    V = None

IDENTITIES = {role: {'pid': pid, 'starttime': 400 + pid, 'uid': 0}
              for role, pid in (('manager', 21), ('service', 22), ('qt', 23), ('observer', 24))}
SESSION = 'a' * 32
UI = 'b' * 32
ROOT = '/userdata/helmet-validation/20261115-native-app-v4'
SELECTION = {'model': 'helmet_416x416.rknn', 'source': 'construction-replay.avi', 'profile': 'responsive'}


def app(visible=False, sequence=3, tab='detection', received_at=99.5):
    return {'session': SESSION, 'root': ROOT, 'version': 'resident-v4', 'phase': 'running',
            'intent': {'sequence': sequence, 'visible': visible, 'tab': tab, 'preview_after': 99.0},
            'ack': {'ui_instance': UI, 'sequence': sequence, 'visible': visible, 'tab': tab,
                    'received_at': received_at, 'qt': IDENTITIES['qt'], 'frame_requests': 5,
                    'frame_decodes': 4, 'preview_presentations': 4},
            'applied': True, 'ui': {'alive': True, 'instance': UI, 'identity': IDENTITIES['qt'], 'exit_code': None},
            'service': IDENTITIES['service'], 'progress': {'cleanup_completed': False},
            'failure': None, 'verification_enabled': True, 'last_render': None}


def det(count=0):
    return {'running': True, 'valid': True, 'state': 'healthy', 'selection': SELECTION.copy(),
            'generation': 'c' * 32, 'source_epoch': 0,
            'counters': {'captured': count, 'detected': count, 'output': count,
                         'read_failure': 0, 'detection_failure': 0}}


def status(start, finish, count):
    return {'method': 'GET', 'route': '/api/status', 'status': 200,
            'request_started': start, 'response_finished': finish, 'body': {'detection': det(count)}}


def terminal():
    return {'session': SESSION, 'root': ROOT, 'code_id': 'd' * 64,
            'exit_receipt': {'kind': 'resident-terminal-v4', 'session': SESSION, 'root': ROOT,
                             'version': 'resident-v4', 'code_id': 'd' * 64,
                             'manager': IDENTITIES['manager'], 'service': IDENTITIES['service'],
                             'ui': IDENTITIES['qt'], 'service_exit': 0, 'phase': 'completed',
                             'cleanup_completed': True, 'resource_lease_released': True,
                             'normal_stop': 'http_quit', 'terminal_at': 200.0},
            'owners': {role: {'identity': IDENTITIES[role], 'observed': None, 'checked_at': 201.0}
                       for role in ('manager', 'service', 'qt')},
            'lease': {'device': 7, 'inode': 8, 'holders': [], 'lock_available': True},
            'private_remaining': [], 'uinput_remaining': [], 'governor_before': 'ondemand',
            'governor_after': 'ondemand', 'stopping_observed': True, 'service_state': {
                'session': SESSION, 'cleanup_completed': True, 'detection_alive': False,
                'wifi_alive': False, 'detection_stopped': True, 'wifi_stopped': True},
            'ui_exit': {'session': SESSION, 'ui_instance': UI, 'identity': IDENTITIES['qt'],
                        'exit_code': -signal.SIGTERM, 'terminal_at': 200.0}, 'clients': []}


def release_fixture():
    names = ['src/app/' + x for x in ('resident.py', 'launch.py', 'service.py', 'wifi.py', 'qml/Main.qml')]
    installed = {x: 'a' * 64 for x in names + ['src/pipeline_runtime.py', 'src/sources.py']}
    installed['models/helmet_416x416.rknn'] = '7e0eefe4bfe86627bb6b58161cacca10912e0dbe1ba2513f58d091c40b26cd48'
    installed['input/construction-replay.avi'] = 'f22897e1ebb40cc919caab335ee4b436d28ebaf0a138eb8b16c63166184af5cc'
    fingerprint = hashlib.sha256()
    for name in ('resident.py', 'launch.py', 'service.py', 'wifi.py', 'qml/Main.qml'):
        fingerprint.update(name.encode('ascii') + b'\0' + bytes.fromhex('a' * 64))
    file_record = {'path': '/reviewed/file', 'sha256': 'a' * 64}
    return {'schema': 1, 'release_id': 'candidate', 'review': {'decision': 'approved-for-validation',
            'reviewer': 'independent-root-review', 'record_sha256': 'f' * 64}, 'root': ROOT,
            'coordinator': '/userdata/helmet-validation/resident-state', 'version': 'resident-v4',
            'code_id': fingerprint.hexdigest(), 'installed': installed,
            'sources': {name: {'path': '/reviewed/' + name, 'sha256': digest} for name, digest in installed.items() if name.startswith('src/')},
            'helpers': {'verifier': file_record.copy(), 'native_keyboard': file_record.copy()},
            'bundles': {name: {'path': '/reviewed/' + name + '.zip', 'sha256': 'b' * 64,
                              'members': installed.copy(), 'installed_map': {x: x for x in installed}}
                        for name in ('payload', 'internal', 'external')},
            'wrappers': {tab: {'path': '/opt/ui/src/apps/' + filename, 'target': ROOT + '/vendor-entry/' + filename,
                               'sha256': 'c' * 64} for tab, filename in (('detection', 'helmet_app'), ('wifi', 'helmet_wifi'))},
            'cfg': {'path': '/opt/ui/src/ATK-DLRK3568/apk3.cfg', 'sha256': 'a' * 64},
            'fixed_parameters': {'conf': .35, 'nms': .45, 'skip': 0, 'result_ttl': 1.0,
                                 'capture_request': [640, 480], 'model': 'helmet_416x416.rknn', 'profile': 'responsive'}}


def make_probe_raw(directory):
    """Synthetic, complete host artifact fixture, never runtime service receipts."""
    import cv2
    import numpy as np
    def write(name, value):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(value, allow_nan=False) + '\n').encode())
    def write_rows(name, values):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b''.join((json.dumps(value, allow_nan=False) + '\n').encode()
                                  for value in values))
    release = release_fixture()
    code_id = release['code_id']
    image = np.zeros((480, 720, 3), np.uint8)
    image[40:200, 40:200] = 255
    success, png = cv2.imencode('.png', image)
    assert success
    (directory / 'captures').mkdir()
    log_name = 'private/Ui-' + UI + '.log'
    (directory / 'private').mkdir()
    http_rows = []
    capture_rows = []
    render_lines_value = []
    for index, (sequence, tab) in enumerate(((3, 'detection'), (4, 'wifi'), (5, 'detection'))):
        application = app(True, sequence, tab, received_at=100.8 + index * .3)
        receipt = {'capture_id': 'show-' + str(sequence) + '-' + UI, 'ui_instance': UI,
                   'sequence': sequence, 'visible': True, 'tab': tab, 'saved': True,
                   'width': 720, 'height': 480, 'qt': IDENTITIES['qt'],
                   'received_at': 100.9 + index * .3, 'packet': None,
                   'valid_at_receipt': None, 'remaining_ttl_ms': 0.0}
        application['last_render'] = receipt
        text = json.dumps(application)
        http_rows.append({'method': 'GET', 'route': '/api/application', 'status': 200,
                          'stage': 'capture_calibration', 'request_started': 101.0 + index * .3,
                          'response_finished': 101.2 + index * .3,
                          'headers': [['Content-Type', 'application/json']],
                          'response_bytes_base64': base64.b64encode(text.encode()).decode(),
                          'response_text': text, 'body': application})
        png_name = 'captures/' + receipt['capture_id'] + '.png'
        (directory / png_name).write_bytes(png.tobytes())
        capture_rows.append({'purpose': 'calibration', 'application_index': index, 'status_index': None,
                             'receipt': receipt, 'png': png_name,
                             'png_sha256': hashlib.sha256(png.tobytes()).hexdigest(), 'ui_log': log_name})
        render_lines_value.append('qml: APP_RESIDENT_RENDER ' + json.dumps(
            {'backend_receipt': receipt, 'saved': True, 'sequence': sequence, 'ui_instance': UI,
             'frame_requests': 5, 'frame_decodes': 4, 'preview_presentations': 4}))
    idle_application = json.dumps({'detection': {
        'running': False, 'valid': False, 'state': 'idle', 'generation': None,
        'counters': {'captured': 0, 'detected': 0, 'output': 0,
                     'read_failure': 0, 'detection_failure': 0}}})
    http_rows.append({'method': 'GET', 'route': '/api/status', 'status': 200,
                      'stage': 'initial_idle', 'request_started': 102.0,
                      'response_finished': 102.1,
                      'headers': [['Content-Type', 'application/json']],
                      'response_bytes_base64': base64.b64encode(idle_application.encode()).decode(),
                      'response_text': idle_application,
                      'body': json.loads(idle_application)})
    write_rows('http.jsonl', http_rows)
    write_rows('captures.jsonl', capture_rows)
    (directory / log_name).write_bytes(('\n'.join(render_lines_value) + '\n').encode())
    write('release.json', release)
    write('identities.json', release)
    write('preflight.json', {'suspicious_owners': [], 'coordinator_record': None,
                            'leases': {}, 'governor': 'ondemand',
                            'camera': None, 'observed_at': 89.0})
    write('decoder.json', {'request_started': 90.0, 'response_finished': 90.1,
                          'versions': {'cv2': cv2.__version__, 'numpy': np.__version__}})
    clients = []
    for pid, tab, control, started in ((25, 'detection', None, 95.0),
                                       (26, 'wifi', None, 95.2),
                                       (27, 'detection', None, 95.4),
                                       (28, 'detection', 'quit', 95.6)):
        clients.append({'identity': {'pid': pid, 'starttime': 400 + pid, 'uid': 0},
                        'exit_code': 0, 'terminal_at': started + .2, 'observed': None,
                        'accepted': True, 'request_started': started, 'tab': tab,
                        'control': control, 'argv': ['wrapper'], 'stderr': '',
                        'stdout': '{"accepted":true}', 'same_owners': control is None})
    write_rows('clients.jsonl', clients)
    write('actions.jsonl', {'group': 'probe-cleanup', 'kind': 'normal-control',
                           'confirmed': False, 'started': 96.0, 'finished': 97.0,
                           'reason': 'declared_software_cleanup_not_UI_Quit'})
    processes = {role: {**value, 'cpu_ticks': 1, 'rss_bytes': 1024, 'threads': 1,
                        'fds': 3, 'mapping_count': 4} for role, value in IDENTITIES.items()}
    write('resources.jsonl', {'stage': 'initial_idle', 'request_started': 94.0,
                             'response_finished': 94.1, 'processes': processes,
                             'lease': {'device': 7, 'inode': 8, 'service_inherited': True,
                                       'service_alive': True},
                             'lease_observed': {'device': 7, 'inode': 8,
                                                'lock_available': False,
                                                'holders': [IDENTITIES[x] for x in ('manager', 'service', 'qt')]},
                             'temperature': [{'path': '/sys/thermal/temp', 'value': '40000'}],
                             'npu_frequency': '800000000', 'governor': 'ondemand',
                             'load': '0 0 0 1/1 1'})
    write('initial-record.json', {'session': SESSION, 'root': ROOT, 'code_id': code_id,
                                'version': 'resident-v4', 'application': application,
                                'manager': IDENTITIES['manager'], 'service': IDENTITIES['service'], 'ui': IDENTITIES['qt']})
    write('ready.json', {'ready': True, 'host': '127.0.0.1', 'port': 12345, 'pid': 22,
                         'identity': IDENTITIES['service'], 'session': SESSION, 'lease': {'device': 7, 'inode': 8}})
    write('start.json', {'kind': 'resident-start-v4', 'session': SESSION, 'root': ROOT, 'code_id': code_id,
                         'version': 'resident-v4',
                         'manager': IDENTITIES['manager'], 'service': IDENTITIES['service'], 'ui': IDENTITIES['qt']})
    ending = terminal()
    ending['code_id'] = ending['exit_receipt']['code_id'] = code_id
    write('terminal.json', ending)
    write('exit.json', ending['exit_receipt'])
    write('service-state.json', ending['service_state'])
    write('private/ui-start-' + UI + '.json', {'session': SESSION, 'ui_instance': UI,
                                             'identity': IDENTITIES['qt'], 'requested_at': 98.0})
    write('private/ui-exit-' + UI + '.json', ending['ui_exit'])
    write('network.json', {'requested': False, 'parent_before': {}, 'parent_after': {}, 'child': {}, 'child_terminal': {}})
    manifest = {'schema': 1, 'mode': 'probe', 'source': 'replay', 'run_id': 'calibration', 'release_id': 'candidate', 'code_id': code_id,
                'owners': IDENTITIES, 'session': SESSION, 'lease': {'device': 7, 'inode': 8},
                'failures': [], 'interrupts': [], 'offline': False, 'cleanup_kind': 'normal-control-quit', 'real_ui_quit': False}
    manifest['inventory'] = {p.relative_to(directory).as_posix(): {'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                                                              'bytes': p.stat().st_size}
                             for p in directory.rglob('*') if p.is_file()}
    write('manifest.json', manifest)
    return manifest


def refresh_inventory(directory, manifest):
    manifest['inventory'] = {
        path.relative_to(directory).as_posix(): {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'bytes': path.stat().st_size,
        }
        for path in directory.rglob('*')
        if path.is_file() and path.name not in ('manifest.json', 'derived-verification.json')
    }
    (directory / 'manifest.json').write_bytes(json.dumps(manifest).encode())


class ResidentValidationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(V, 'resident validator feature has not been implemented')

    def test_requires_current_actual_ack_not_accepted_intent(self):
        self.assertEqual(V.validate_ack(app(), SESSION, IDENTITIES, False, 'detection')['sequence'], 3)
        for change in ('accepted', 'old_session', 'old_ui', 'old_sequence', 'missing_counter'):
            value = app()
            if change == 'accepted':
                value['applied'], value['ack'] = False, None
            elif change == 'old_session':
                value['session'] = 'e' * 32
            elif change == 'old_ui':
                value['ack']['ui_instance'] = 'f' * 32
            elif change == 'old_sequence':
                value['ack']['sequence'] = 2
            else:
                del value['ack']['frame_decodes']
            with self.subTest(change=change), self.assertRaises(ValueError):
                V.validate_ack(value, SESSION, IDENTITIES, False, 'detection')

    def test_live_wrapper_client_cannot_begin_formal_interval(self):
        good = {'identity': {'pid': 25, 'starttime': 425, 'uid': 0}, 'exit_code': 0,
                'terminal_at': 99.0, 'observed': None, 'accepted': True}
        V.validate_client(good, 100.0)
        for change in ({'observed': good['identity']}, {'exit_code': 1}, {'terminal_at': 100.1}):
            with self.assertRaises(ValueError):
                V.validate_client({**good, **change}, 100.0)

    def test_exact_hidden_duration_and_upper_fps_denominator(self):
        result = V.validate_interval(status(100.0, 100.25, 0), status(130.25, 130.5, 305), 'replay')
        self.assertEqual(result['hidden_lower_seconds'], 30.0)
        self.assertEqual(result['fps_denominator_seconds'], 30.5)
        self.assertEqual(result['fps']['detected'], 10.0)
        self.assertFalse(result['long_hidden'])
        with self.assertRaises(ValueError):
            V.validate_interval(status(100, 101, 0), status(100.5, 102, 20), 'replay')

    def test_intermediate_failure_growth_config_or_counter_change_fails(self):
        for mutation in ('config', 'failure', 'regression', 'epoch'):
            final = status(132, 132.1, 320)
            middle = status(116, 116.1, 160)
            if mutation == 'config':
                middle['body']['detection']['selection']['profile'] = 'replay'
            elif mutation == 'failure':
                middle['body']['detection']['counters']['read_failure'] = 1
            elif mutation == 'regression':
                middle['body']['detection']['counters']['output'] = 0
                final['body']['detection']['counters']['output'] = 0
                middle = status(116, 116.1, 160)
                final['body']['detection']['counters']['output'] = 0
            else:
                middle['body']['detection']['source_epoch'] = 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                V.validate_statuses([status(100, 100.1, 0), middle, final], 'replay', max_gap=20)

    def test_sampling_gap_and_hidden_preview_counter_growth_fail(self):
        with self.assertRaises(ValueError):
            V.validate_schedule([status(100, 100.1, 0), status(104, 104.1, 40)], 3, 100, 104.1)
        rows = [{'body': app(received_at=99.8), 'request_started': 100.0, 'response_finished': 100.1},
                {'body': app(received_at=100.8), 'request_started': 101.0, 'response_finished': 101.1}]
        V.validate_hidden_apps(rows, SESSION, IDENTITIES)
        rows[1]['body']['ack']['frame_requests'] += 1
        with self.assertRaises(ValueError):
            V.validate_hidden_apps(rows, SESSION, IDENTITIES)

    def test_raw_http_body_text_bytes_status_and_nonfinite_are_checked(self):
        text = '{"detection":{"running":true}}'
        row = {'method': 'GET', 'route': '/api/status', 'status': 200,
               'request_started': 1.0, 'response_finished': 1.1, 'headers': [['Content-Type', 'application/json']],
               'response_bytes_base64': base64.b64encode(text.encode()).decode(),
               'response_text': text, 'body': json.loads(text)}
        V.validate_http(row)
        for change in ({'body': {}}, {'response_text': '{}'}, {'status': 503},
                       {'request_started': float('nan')}, {'headers': []}):
            with self.assertRaises(ValueError):
                V.validate_http({**row, **change})

    def test_stale_expired_and_preshow_display_packet_rejected(self):
        receipt = {'capture_id': 'show-3-' + UI, 'ui_instance': UI, 'sequence': 3,
                   'visible': True, 'tab': 'detection', 'saved': True, 'width': 720, 'height': 480,
                   'qt': IDENTITIES['qt'], 'received_at': 100.0, 'valid_at_receipt': True,
                   'remaining_ttl_ms': 500.0,
                   'packet': {'frame_id': 4, 'captured_at': 99.5, 'generation': 'c' * 32,
                              'source_epoch': 0, 'selection': SELECTION.copy()}}
        V.validate_render_metadata(receipt, app(True), det(), 'run')
        for mutation in ('epoch', 'generation', 'expired', 'preshow', 'idle'):
            value = copy.deepcopy(receipt)
            if mutation == 'epoch': value['packet']['source_epoch'] = 1
            elif mutation == 'generation': value['packet']['generation'] = 'e' * 32
            elif mutation == 'expired': value['received_at'] = 101.0
            elif mutation == 'preshow': value['packet']['captured_at'] = 98.9
            else: value['packet'] = None
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                V.validate_render_metadata(value, app(True), det(), 'run')

    def test_live_owner_inherited_lease_missing_cleanup_cannot_pass(self):
        V.validate_terminal(terminal(), IDENTITIES)
        for mutation in ('live', 'lease', 'private', 'receipt', 'stopping'):
            value = terminal()
            if mutation == 'live': value['owners']['service']['observed'] = IDENTITIES['service']
            elif mutation == 'lease': value['lease']['holders'] = [IDENTITIES['service']]
            elif mutation == 'private': value['private_remaining'] = ['token']
            elif mutation == 'receipt': value['exit_receipt']['cleanup_completed'] = False
            else: value['stopping_observed'] = False
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                V.validate_terminal(value, IDENTITIES)

    def test_inventory_rejects_empty_missing_extra_and_symlink_raw(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'http.jsonl').write_bytes(b'{}\n')
            inventory = {'http.jsonl': {'sha256': 'ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356', 'bytes': 3}}
            V.validate_inventory(directory, inventory, required={'http.jsonl'})
            for value in ({}, {**inventory, 'missing.json': {'sha256': 'a' * 64, 'bytes': 1}}):
                with self.assertRaises(ValueError): V.validate_inventory(directory, value, required={'http.jsonl'})
            (directory / 'extra.txt').write_text('unexpected')
            with self.assertRaises(ValueError): V.validate_inventory(directory, inventory, required={'http.jsonl'})

    def test_emergency_cleanup_never_substitutes_for_real_ui_action(self):
        actions = [{'group': name, 'kind': 'native-keyboard', 'started': 1.0 + i,
                    'finished': 1.1 + i, 'owner_checked': IDENTITIES, 'confirmed': True,
                    'evidence': i} for i, name in enumerate(('configure', 'start', 'hide', 'stop', 'quit'))]
        V.validate_action_gates(actions, IDENTITIES)
        for name in ('hide', 'stop', 'quit'):
            value = copy.deepcopy(actions)
            value[next(i for i, row in enumerate(value) if row['group'] == name)]['kind'] = 'emergency-control'
            with self.assertRaises(ValueError): V.validate_action_gates(value, IDENTITIES)

    def test_release_forged_identity_and_bad_keys_rejected(self):
        with self.assertRaises(ValueError): V.validate_release({'version': 'resident-v4', 'reviewed': True})
        with self.assertRaises(ValueError): V.safe_id('../escape')
        with self.assertRaises(ValueError): V.validate_keys({'groups': {'navigate': [{'key': 'Space'}]}}, probe=True)
        with self.assertRaises(ValueError): V.validate_keys({'groups': {'navigate': [{'chord': ['Alt', 'F4']}]}}, probe=True)

    def test_late_signal_after_publication_prevents_success(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'raw.txt').write_text('raw')
            interrupted = []
            def publish_boundary(): interrupted.append(15)
            result = V.publish(directory, {'mode': 'probe', 'failures': []},
                               {'passed': True, 'gates': {}}, interrupted,
                               final_boundary=publish_boundary)
            self.assertFalse(result['passed'])
            manifest = json.loads((directory / 'manifest.json').read_text())
            self.assertIn('interrupted:15', manifest['failures'])
            self.assertFalse(json.loads((directory / 'derived-verification.json').read_text())['passed'])

    def test_verify_missing_raw_exit_fails_without_mutating_raw(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'manifest.json').write_text('{"mode":"run"}')
            before = (directory / 'manifest.json').read_bytes()
            result = V.verify_directory(directory)
            self.assertFalse(result['passed'])
            self.assertEqual((directory / 'manifest.json').read_bytes(), before)
            self.assertFalse((directory / 'derived-verification.json').exists())

    def test_wrapper_action_uses_real_argument_list_and_terminal_identity(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            with mock.patch.object(V, 'current_identity', side_effect=[{'pid': 25, 'starttime': 425, 'uid': 0}, None]):
                result = V.invoke_client(directory, 'detection', None, None,
                                         command=[sys.executable, '-c', 'import time; time.sleep(.15); print("{\\"accepted\\":true}")'])
            self.assertEqual(result['exit_code'], 0)
            self.assertTrue(result['accepted'])
            self.assertIsNone(result['observed'])
            self.assertTrue((directory / 'clients.jsonl').is_file())

    def test_runtime_invalid_duration_fails_before_output_or_launcher(self):
        result = subprocess.run([sys.executable, str(PATH), 'run', '--run-id', 'bad', '--release', 'missing.json',
                                 '--duration', '29', '--keys', 'missing.json', '--source', 'replay'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'duration', result.stderr)

    def test_probe_and_run_help_expose_actual_release_and_keys_cli(self):
        for mode in ('probe', 'run', 'verify'):
            result = subprocess.run([sys.executable, str(PATH), mode, '--help'], capture_output=True)
            self.assertEqual(result.returncode, 0)
            if mode != 'verify':
                self.assertIn(b'--release', result.stdout)

    def test_empty_config_and_missing_capture_do_not_verify(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            with mock.patch.object(V, 'validate_release', return_value={'release_id': 'test', 'code_id': 'd' * 64}), \
                    mock.patch.object(V, 'read_json', side_effect=lambda path: {'schema': 1, 'mode': 'run', 'run_id': 'test'}
                                      if Path(path).name == 'manifest.json' else {}):
                result = V.verify_directory(directory)
            self.assertFalse(result['passed'])
            self.assertFalse(result['gates']['calibration'])

    def test_complete_host_raw_probe_recomputes_then_detects_body_and_identity_tampering(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            manifest = make_probe_raw(directory)
            before = {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
            result = V.verify_directory(directory)
            self.assertTrue(result['passed'], result)
            self.assertEqual(before, {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob('*') if p.is_file()})
            lines = (directory / 'http.jsonl').read_text().splitlines()
            record = json.loads(lines[0])
            record['body']['session'] = 'e' * 32
            lines[0] = json.dumps(record)
            (directory / 'http.jsonl').write_text('\n'.join(lines) + '\n')
            refresh_inventory(directory, manifest)
            self.assertFalse(V.verify_directory(directory)['passed'])

    def test_reviewed_release_actual_source_or_bundle_change_is_rejected(self):
        approved = release_fixture()
        V.validate_release(approved, copy.deepcopy(approved))
        for mutation in ('model', 'source', 'code', 'bundle'):
            changed = copy.deepcopy(approved)
            if mutation == 'model': changed['installed']['models/helmet_416x416.rknn'] = 'e' * 64
            elif mutation == 'source': changed['sources']['src/sources.py']['sha256'] = 'e' * 64
            elif mutation == 'code': changed['code_id'] = 'e' * 64
            else: changed['bundles']['internal']['members']['src/sources.py'] = 'e' * 64
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                V.validate_release(approved, changed)

    def test_empty_required_raw_category_fails_even_when_inventory_is_rehashed(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            manifest = make_probe_raw(directory)
            (directory / 'resources.jsonl').write_bytes(b'')
            manifest['inventory']['resources.jsonl'] = {'sha256': hashlib.sha256(b'').hexdigest(), 'bytes': 0}
            (directory / 'manifest.json').write_bytes(json.dumps(manifest).encode())
            self.assertFalse(V.verify_directory(directory)['gates']['inventory'])

    def test_run_keys_require_local_verified_calibration_not_only_document_boolean(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            with self.assertRaises(ValueError):
                V.validate_bound_calibration(directory, {'schema': 1}, release_fixture(), 'camera')

    def test_resource_rows_require_actual_fd_lease_holders(self):
        processes = {role: {**value, 'cpu_ticks': 1, 'rss_bytes': 1024, 'threads': 1, 'fds': 3,
                            'mapping_count': 4} for role, value in IDENTITIES.items()}
        rows = [{'request_started': 100.0, 'response_finished': 100.1, 'processes': processes,
                 'lease': {'device': 7, 'inode': 8, 'service_inherited': True, 'service_alive': True},
                 'temperature': [{'path': '/sys/thermal/temp', 'value': '40000'}], 'load': '0 0 0',
                 'npu_frequency': '800000000', 'governor': 'ondemand'},
                {'request_started': 101.0, 'response_finished': 101.1, 'processes': processes,
                 'lease': {'device': 7, 'inode': 8, 'service_inherited': True, 'service_alive': True},
                 'temperature': [{'path': '/sys/thermal/temp', 'value': '40000'}], 'load': '0 0 0',
                 'npu_frequency': '800000000', 'governor': 'ondemand'}]
        with self.assertRaises(ValueError):
            V.validate_resources(rows, IDENTITIES, {'device': 7, 'inode': 8}, 100, 101.1)

    def test_formal_start_requires_settled_hidden_ack_boundary(self):
        proof = {'body': app(), 'request_started': 99.6, 'response_finished': 99.7}
        V.validate_formal_ack(proof, status(100, 100.1, 0), SESSION, IDENTITIES)
        proof['response_finished'] = 100.2
        with self.assertRaises(ValueError):
            V.validate_formal_ack(proof, status(100, 100.1, 0), SESSION, IDENTITIES)

    def test_preview_must_decode_unresized_640_by_480_and_current_ttl(self):
        import cv2
        import numpy as np
        def row(width):
            image = np.zeros((480, width, 3), np.uint8)
            image[10:100, 10:100] = 255
            success, jpeg = cv2.imencode('.jpg', image)
            self.assertTrue(success)
            return {'status': 200, 'request_started': 100.0, 'response_finished': 100.1,
                    'body': {'valid': True, 'generation': 'c' * 32, 'source_epoch': 0,
                             'frame_id': 3, 'captured_at': 99.9, 'source_age_ms': 100.0,
                             'remaining_ttl_ms': 900.0, 'jpeg_base64': base64.b64encode(jpeg).decode()}}
        self.assertEqual(V.validate_preview(row(640), det())['dimensions'], [640, 480])
        with self.assertRaises(ValueError): V.validate_preview(row(320), det())
        expired = row(640)
        expired['response_finished'] = 101.0
        with self.assertRaises(ValueError): V.validate_preview(expired, det())

    def test_actual_keyboard_group_inputs_must_match_calibrated_sequence(self):
        actions = [{'group': 'hide', 'started': 100.0, 'finished': 101.0, 'kind': 'native-keyboard',
                    'inputs': [{'action': {'key': 'Enter', 'pause': .2}, 'sent_at': 100.2, 'owner_checked': IDENTITIES}]}]
        keys = {'groups': {'hide': [{'key': 'Enter', 'pause': .2}]}}
        V.validate_native_inputs(actions, keys, IDENTITIES)
        actions[0]['inputs'][0]['action']['key'] = 'Space'
        with self.assertRaises(ValueError): V.validate_native_inputs(actions, keys, IDENTITIES)

    def test_quit_private_witness_must_be_actual_fresh_current_stopping_record(self):
        record = {'session': SESSION, 'root': ROOT, 'version': 'resident-v4', 'code_id': 'd' * 64,
                  'phase': 'stopping', 'recorded_at': 100.3, 'manager': IDENTITIES['manager'],
                  'service': IDENTITIES['service'], 'ui': IDENTITIES['qt'],
                  'application': {'phase': 'stopping', 'session': SESSION}}
        row = {'request_started': 100.35, 'response_finished': 100.4, 'record': record}
        V.validate_quit_witness(row, 100.2, SESSION, IDENTITIES, 'd' * 64)
        for change in ({'phase': 'running'}, {'recorded_at': 99.0}, {'session': 'e' * 32}):
            value = copy.deepcopy(row)
            value['record'].update(change)
            with self.assertRaises(ValueError):
                V.validate_quit_witness(value, 100.2, SESSION, IDENTITIES, 'd' * 64)

    def test_missing_ready_fields_fail_even_with_complete_rehashed_inventory(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            manifest = make_probe_raw(directory)
            ready = json.loads((directory / 'ready.json').read_bytes())
            del ready['port']
            data = (json.dumps(ready) + '\n').encode()
            (directory / 'ready.json').write_bytes(data)
            manifest['inventory']['ready.json'] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
            (directory / 'manifest.json').write_bytes(json.dumps(manifest).encode())
            self.assertFalse(V.verify_directory(directory)['gates']['private_identity'])

    def test_missing_actual_ui_exit_raw_cannot_be_replaced_by_terminal_dictionary(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            manifest = make_probe_raw(directory)
            path = 'private/ui-exit-' + UI + '.json'
            (directory / path).unlink()
            del manifest['inventory'][path]
            (directory / 'manifest.json').write_bytes(json.dumps(manifest).encode())
            self.assertFalse(V.verify_directory(directory)['gates']['terminal'])

    def test_missing_snapshot_verification_field_is_unknown_not_default_true(self):
        value = app()
        del value['verification_enabled']
        with self.assertRaises(ValueError):
            V.validate_ack(value, SESSION, IDENTITIES, False, 'detection')

    def test_owner_accepts_early_manager_record_then_waits_for_ready_handoff(self):
        release = release_fixture()
        early = {'protocol': 1, 'version': V.VERSION, 'root': V.APP_ROOT.as_posix(),
                 'code_id': release['code_id'], 'mode': 'resident', 'verification_enabled': True,
                 'session': SESSION, 'workspace': str(V.APP_ROOT / 'app-state' / ('session-' + SESSION)),
                 'phase': 'starting', 'manager': IDENTITIES['manager'], 'service': None, 'ui': None}
        with mock.patch.object(V, 'read_json', return_value=early), \
                mock.patch.object(V, 'no_links'):
            owner = V.Owner(release)
        self.assertEqual(owner.session, SESSION)
        self.assertEqual(owner.identities, {'manager': IDENTITIES['manager']})
        self.assertIsNone(owner.ready)

    def test_owner_ready_handoff_binds_service_qt_lease_and_private_token(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / 'app'
            workspace = root / 'app-state' / ('session-' + SESSION)
            workspace.mkdir(parents=True)
            token = workspace / 'token'
            token.write_text('t' * 48, encoding='ascii')
            token.chmod(0o600)
            release = release_fixture()
            record = {'protocol': 1, 'version': V.VERSION, 'root': root.as_posix(),
                      'code_id': release['code_id'], 'mode': 'resident',
                      'verification_enabled': True, 'session': SESSION,
                      'workspace': str(workspace), 'phase': 'running',
                      'manager': IDENTITIES['manager'], 'service': IDENTITIES['service'],
                      'ui': IDENTITIES['qt'],
                      'lease': {'device': 7, 'inode': 8, 'service_inherited': True,
                                'service_alive': True},
                      'application': {'ui': {'instance': UI}}}
            ready = {'ready': True, 'host': '127.0.0.1', 'port': 12345,
                     'pid': IDENTITIES['service']['pid'], 'identity': IDENTITIES['service'],
                     'session': SESSION, 'lease': {'device': 7, 'inode': 8}}
            with mock.patch.object(V, 'APP_ROOT', root), \
                    mock.patch.object(V, 'read_json', return_value=record), \
                    mock.patch.object(V, 'no_links', return_value=SimpleNamespace(
                        st_uid=0, st_mode=stat.S_IFREG | 0o600, st_nlink=1)), \
                    mock.patch.object(V.os, 'getuid', return_value=0, create=True):
                owner = V.Owner(release)
                with mock.patch.object(owner, 'assert_control_alive', return_value=record), \
                        mock.patch.object(owner, 'assert_alive', return_value=record), \
                        mock.patch.object(V, 'read_json', return_value=ready), \
                        mock.patch.object(V, 'current_identity', return_value=IDENTITIES['observer']):
                    owner.wait_ready(timeout=.01)
        self.assertEqual(owner.identities, IDENTITIES)
        self.assertEqual(owner.lease, {'device': 7, 'inode': 8})
        self.assertEqual(owner.ready, ready)
        self.assertEqual(owner._token, 't' * 48)

    def test_terminal_accepts_actual_r1_completed_receipt_but_not_arbitrary_success(self):
        V.validate_terminal(terminal(), IDENTITIES)
        for mutation, value in (
                ('phase', 'running'),
                ('normal_stop', 'invented_success'),
                ('normal_stop', True),
                ('service_exit', 1),
                ('ui_exit', -9)):
            changed = terminal()
            if mutation == 'ui_exit':
                changed['ui_exit']['exit_code'] = value
            else:
                changed['exit_receipt'][mutation] = value
            with self.subTest(mutation=mutation, value=value), self.assertRaises(ValueError):
                V.validate_terminal(changed, IDENTITIES)

    def test_probe_journals_require_real_rows_tabs_cleanup_and_decoder(self):
        mutations = []
        for name in ('resources.jsonl', 'actions.jsonl'):
            mutations.append((name, lambda directory, name=name: (directory / name).write_bytes(b'\n')))
        mutations.extend((
            ('decoder.json', lambda directory: (directory / 'decoder.json').write_bytes(b'{}\n')),
            ('preflight.json', lambda directory: (directory / 'preflight.json').write_bytes(b'[]\n')),
            ('clients.jsonl', lambda directory: (directory / 'clients.jsonl').write_text(
                '\n'.join(line for line in (directory / 'clients.jsonl').read_text().splitlines()
                          if '"tab": "wifi"' not in line) + '\n')),
            ('captures.jsonl', lambda directory: (directory / 'captures.jsonl').write_text(
                (directory / 'captures.jsonl').read_text().splitlines()[0] + '\n')),
        ))
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as name:
                directory = Path(name)
                manifest = make_probe_raw(directory)
                mutate(directory)
                refresh_inventory(directory, manifest)
                self.assertFalse(V.verify_directory(directory)['passed'])

    def test_bound_navigation_probe_allows_only_the_extra_detection_show(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            manifest = make_probe_raw(directory)
            clients = [json.loads(line) for line in (directory / 'clients.jsonl').read_text().splitlines()]
            clients.insert(1, copy.deepcopy(clients[0]))
            (directory / 'clients.jsonl').write_text(
                ''.join(json.dumps(row) + '\n' for row in clients))
            captures = [json.loads(line) for line in (directory / 'captures.jsonl').read_text().splitlines()]
            captures.insert(1, copy.deepcopy(captures[0]))
            (directory / 'captures.jsonl').write_text(
                ''.join(json.dumps(row) + '\n' for row in captures))
            (directory / 'keys.json').write_text('{"schema":1}\n')
            V.validate_probe_journals(directory, manifest['owners'], manifest['lease'],
                                      V.read_json(directory / 'preflight.json')['governor'])

    def test_ack_render_and_long_hidden_heartbeat_have_observation_time_bounds(self):
        with self.assertRaises(ValueError):
            V.validate_ack(app(), SESSION, IDENTITIES, False, 'detection', received_before=99.0)
        receipt = {'capture_id': 'show-3-' + UI, 'ui_instance': UI, 'sequence': 3,
                   'visible': True, 'tab': 'detection', 'saved': True, 'width': 720, 'height': 480,
                   'qt': IDENTITIES['qt'], 'received_at': 1000.0, 'valid_at_receipt': None,
                   'remaining_ttl_ms': 0.0, 'packet': None}
        with self.assertRaises(ValueError):
            V.validate_render_metadata(receipt, app(True), None, 'probe', completed_before=100.5)
        stale = [{'request_started': float(t), 'response_finished': t + .1, 'body': app()}
                 for t in range(100, 1901)]
        with self.assertRaises(ValueError):
            V.validate_hidden_apps(stale, SESSION, IDENTITIES)
        heartbeat = []
        for t in range(100, 1901):
            heartbeat.append({'request_started': float(t), 'response_finished': t + .1,
                              'body': app(received_at=t - .2)})
        V.validate_hidden_apps(heartbeat, SESSION, IDENTITIES)

    def test_active_timeline_cannot_reset_same_generation_counters(self):
        before = [status(100, 100.1, 520), status(101, 101.1, 540)]
        hidden = [status(102, 102.1, 560), status(103, 103.1, 920)]
        after = [status(133, 133.1, 940), status(134, 134.1, 960)]
        V.validate_active_timeline(before, hidden, after, 'replay')
        for position, value in (('hidden-first', 0), ('after-first', 0), ('after-second', 900)):
            target = hidden[0] if position == 'hidden-first' else after[0 if position == 'after-first' else 1]
            changed = copy.deepcopy([before, hidden, after])
            changed[1 if position == 'hidden-first' else 2][0 if position != 'after-second' else 1]['body']['detection']['counters']['captured'] = value
            with self.subTest(position=position), self.assertRaises(ValueError):
                V.validate_active_timeline(*changed, 'replay')

    def test_ui_confirmation_must_follow_trigger_native_input(self):
        action = {'started': 100.0, 'finished': 101.0,
                  'inputs': [{'sent_at': 100.8}]}
        proof = {'request_started': 100.1, 'response_finished': 100.2}
        with self.assertRaises(ValueError):
            V.validate_action_proof_timing(action, proof)
        V.validate_action_proof_timing(action, {'request_started': 100.9, 'response_finished': 101.0})

    def test_offline_child_must_match_observer_identity_and_terminal_observation(self):
        parent = {'namespace': 'net:[101]', 'interfaces': [{'name': 'lo', 'flags': 1}],
                  'addresses': [{'ifname': 'lo'}]}
        network = {'requested': True, 'parent_before': parent, 'parent_after': copy.deepcopy(parent),
                   'child': {'namespace': 'net:[202]', 'interfaces': [{'name': 'lo', 'flags': 1}],
                             'addresses': [{'ifname': 'lo'}]},
                   'child_terminal': {'identity': IDENTITIES['observer'], 'exit_code': 0,
                                      'observed': None, 'checked_at': 110.0}}
        V.validate_offline(network, True, IDENTITIES['observer'])
        network['child_terminal'] = {'exit_code': 0}
        with self.assertRaises(ValueError):
            V.validate_offline(network, True, IDENTITIES['observer'])

    def test_confirmation_deadline_preview_cadence_and_resource_completion_are_strict(self):
        class Clock:
            def __init__(self):
                self.value = 0.0
            def monotonic(self):
                return self.value
            def sleep(self, amount):
                self.value += amount

        for method in ('wait_ack', 'wait_status'):
            clock = Clock()
            owner = SimpleNamespace(session=SESSION, identities=IDENTITIES)
            observation = V.Observation(owner, Path('.'), {'source': 'replay'})
            def late_get(route, stage):
                clock.value = 6.0
                body = app() if route == '/api/application' else {'detection': det()}
                if route == '/api/application':
                    body['ack']['received_at'] = 4.95
                return 0, {'method': 'GET', 'route': route, 'status': 200, 'body': body,
                           'request_started': 4.9, 'response_finished': 5.0}
            observation.get = late_get
            with self.subTest(method=method), mock.patch.object(V, 'time', clock):
                with self.assertRaises(ValueError):
                    observation.wait_ack(False, 'detection', 'review') if method == 'wait_ack' else \
                        observation.wait_status(True, 'review')

        clock = Clock()
        clock.value = 100.0
        owner = SimpleNamespace(session=SESSION, identities=IDENTITIES)
        observation = V.Observation(owner, Path('.'), {'source': 'replay'})
        preview_starts = []
        def scheduled_get(route, stage):
            started = clock.value
            clock.value += .03
            if route == '/api/preview':
                preview_starts.append(started)
            return 0, {'method': 'GET', 'route': route, 'status': 200,
                       'request_started': started, 'response_finished': clock.value,
                       'body': {'detection': det()}}
        observation.get = scheduled_get
        with mock.patch.object(V, 'time', clock), \
                mock.patch.object(V, 'resource_sample', return_value={}), \
                mock.patch.object(V, 'append_json'), \
                mock.patch.object(V, 'validate_preview'):
            observation.observe_visible('visible_before', 12)
        self.assertGreaterEqual(len(preview_starts), 3)
        self.assertTrue(all(0 < b - a <= 5.5 for a, b in zip(preview_starts, preview_starts[1:])),
                        preview_starts)

        clock.value = 100.0
        owner = SimpleNamespace(identities=IDENTITIES, last_lease={}, assert_alive=lambda: {'lease': {}})
        processes = {value['pid']: {**value, 'cpu_ticks': 1, 'rss_bytes': 1024,
                                   'threads': 1, 'fds': 3, 'mapping_count': 4}
                     for value in IDENTITIES.values()}
        def slow_sys_read(path):
            clock.value += .1
            return '1'
        with mock.patch.object(V, 'time', clock), \
                mock.patch.object(V, 'proc_read', side_effect=lambda pid: processes[pid]), \
                mock.patch.object(V, 'current_identity',
                                  side_effect=lambda pid: {key: processes[pid][key] for key in V.IDENTITY_FIELDS}), \
                mock.patch.object(V, 'npu_root', return_value=Path('/synthetic/npu')), \
                mock.patch.object(V, 'sys_text', side_effect=slow_sys_read), \
                mock.patch.object(Path, 'glob', return_value=[]):
            resource = V.resource_sample(owner, 'review')
        self.assertGreaterEqual(resource['response_finished'], clock.value)

    def test_zip_validator_rejects_unsafe_symlink_and_duplicate_directory_entries(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'unsafe.zip'
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('safe.py', 'pass\n')
                directory = zipfile.ZipInfo('../escape/')
                directory.create_system = 3
                directory.external_attr = (stat.S_IFDIR | 0o755) << 16
                archive.writestr(directory, b'')
            with self.assertRaises(ValueError):
                V.validated_zip_members(path)
            with zipfile.ZipFile(path, 'w') as archive:
                link = zipfile.ZipInfo('link/')
                link.create_system = 3
                link.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(link, 'target')
            with self.assertRaises(ValueError):
                V.validated_zip_members(path)
            with zipfile.ZipFile(path, 'w') as archive:
                directory = zipfile.ZipInfo('duplicate/')
                directory.create_system = 3
                directory.external_attr = (stat.S_IFDIR | 0o755) << 16
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    archive.writestr(directory, b'')
                    archive.writestr(directory, b'')
            with self.assertRaises(ValueError):
                V.validated_zip_members(path)
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('safe.py', 'pass\n')
            self.assertEqual(set(V.validated_zip_members(path)), {'safe.py'})

    def test_final_boundary_abort_cannot_leave_rederivable_success(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / 'raw.txt').write_text('raw')
            interrupted = []
            def aborted():
                interrupted.append(signal.SIGTERM)
                raise OSError('controlled abort')
            V.publish(directory, {'schema': 1, 'mode': 'probe', 'failures': []},
                      {'passed': True, 'gates': {'schema': True}}, interrupted, aborted)
            manifest = json.loads((directory / 'manifest.json').read_text())
            derived = json.loads((directory / 'derived-verification.json').read_text())
            self.assertFalse(derived['passed'])
            self.assertIn(signal.SIGTERM, manifest['interrupts'])
            self.assertTrue(any(x.startswith('interrupted:') for x in manifest['failures']))

    def test_release_schema_and_fixed_numeric_types_are_exact(self):
        approved = release_fixture()
        for mutation in ('schema', 'ttl'):
            changed = copy.deepcopy(approved)
            if mutation == 'schema':
                changed['schema'] = True
            else:
                changed['fixed_parameters']['result_ttl'] = True
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                V.validate_release(changed)


if __name__ == '__main__':
    unittest.main()
