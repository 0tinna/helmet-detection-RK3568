"""Resident contracts; Linux kernel tests are an isolated, non-NPU fixture.

Run ``python3 tests/test_app_resident.py --linux-fixture --fixture-dir PATH``
on Linux; board PATH belongs below the explicit /userdata validation root. This
exercises real flock, inherited FDs, SO_PEERCRED and detached processes, not
Qt, a physical camera, WiFi association or RKNN inference.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import importlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]
APP_DIR = REPO / 'helmet/app'
RESIDENT_MODULE = 'helmet.app.resident'
if not APP_DIR.is_dir():
    for parent in Path(__file__).resolve().parents:
        choices = (parent / 'src/app', parent / 'app')
        candidate = next((path for path in choices if (path / 'resident.py').is_file()), None)
        if candidate is not None:
            APP_DIR, REPO, RESIDENT_MODULE = candidate.resolve(), candidate.resolve().parent, 'app.resident'
            break
sys.path.insert(0, str(REPO))
NODE = shutil.which('node')
FIXTURE_PARENT = None


def module(case):
    try:
        return importlib.import_module(RESIDENT_MODULE)
    except ImportError:
        case.fail('the resident manager is not implemented')


class ResidentStateTests(unittest.TestCase):
    def setUp(self):
        self.resident = module(self)
        self.session = 'a' * 32
        self.root = '/userdata/helmet-validation/fixture-v4'
        self.state = self.resident.ApplicationState(self.root, self.session)
        self.state.mark_running()
        self.state.ui_started('b' * 32)

    def test_two_icon_requests_share_owner_and_latest_ordered_intent(self):
        with ThreadPoolExecutor(max_workers=2) as clients:
            replies = list(clients.map(lambda tab: self.state.request(
                'show', request_id='icon-' + tab, tab=tab), ('detection', 'wifi')))
        sequences = sorted(reply['application']['intent']['sequence'] for reply in replies)
        self.assertEqual(sequences, [1, 2])
        latest = max(replies, key=lambda reply: reply['application']['intent']['sequence'])
        self.assertEqual(self.state.snapshot()['intent'], latest['application']['intent'])
        self.assertEqual(self.state.snapshot()['ui']['instance'], 'b' * 32)

    def test_replayed_request_is_idempotent_and_cannot_change_the_page(self):
        first = self.state.request('show', request_id='same-click', tab='wifi')
        replay = self.state.request('show', request_id='same-click', tab='wifi')
        self.assertEqual(replay['application']['intent'], first['application']['intent'])
        with self.assertRaises(ValueError):
            self.state.request('show', request_id='same-click', tab='detection')
        self.assertEqual(self.state.snapshot()['intent']['tab'], 'wifi')

    def test_ack_requires_current_ui_session_sequence_and_actual_visibility(self):
        self.state.request('show', request_id='open', tab='wifi')
        self.state.request('ack', request_id='ack-1', ui_instance='b' * 32,
                           sequence=1, visible=False, tab='wifi')
        self.assertFalse(self.state.snapshot()['applied'])
        self.state.request('ack', request_id='ack-2', ui_instance='b' * 32,
                           sequence=1, visible=True, tab='wifi')
        self.assertTrue(self.state.snapshot()['applied'])
        self.state.request('hide', request_id='hide')
        self.assertFalse(self.state.snapshot()['applied'])
        with self.assertRaises(ValueError):
            self.state.request('ack', request_id='future', ui_instance='b' * 32,
                               sequence=999, visible=True, tab='wifi')
        self.state.ui_exited(0)
        self.state.ui_started('c' * 32)
        with self.assertRaises(ValueError):
            self.state.request('ack', request_id='old-ui', ui_instance='b' * 32,
                               sequence=2, visible=False, tab='wifi')

    def test_ui_exit_zero_is_recoverable_and_does_not_mean_application_quit(self):
        self.state.request('show', request_id='first', tab='detection')
        self.state.ui_exited(0)
        self.assertEqual(self.state.snapshot()['phase'], 'running')
        self.assertFalse(self.state.snapshot()['ui']['alive'])
        self.assertFalse(self.state.needs_ui())
        self.state.request('show', request_id='again', tab='wifi')
        self.assertTrue(self.state.needs_ui())
        self.assertEqual(self.state.snapshot()['session'], self.session)

    def test_explicit_quit_is_irreversible_while_progress_remains_readable(self):
        self.state.request('quit', request_id='quit')
        self.assertEqual(self.state.request('status')['application']['phase'], 'stopping')
        for command, fields in [('show', {'tab': 'detection'}), ('hide', {})]:
            with self.subTest(command=command), self.assertRaises(RuntimeError):
                self.state.request(command, request_id=command, **fields)
        self.state.ui_exited(0)
        self.assertEqual(self.state.snapshot()['phase'], 'stopping')
        self.assertFalse(self.state.needs_ui())

    def test_service_failure_refuses_reopen_even_if_ui_is_gone(self):
        self.state.service_failed('service exited')
        self.state.ui_exited(0)
        self.assertEqual(self.state.snapshot()['phase'], 'failed')
        with self.assertRaises(RuntimeError):
            self.state.request('show', request_id='again', tab='wifi')
        self.assertFalse(self.state.needs_ui())

    def test_unknown_fields_wrong_session_and_nonfinite_or_nested_messages_reject(self):
        valid = {'protocol': 1, 'version': self.resident.APP_VERSION, 'root': self.root,
                 'session': self.session, 'command': 'show', 'request_id': 'click', 'tab': 'wifi'}
        for changes in ({'session': 'd' * 32}, {'root': self.root + '-other'},
                        {'version': 'old-v3'}, {'command': 'exec'}, {'tab': '../wifi'},
                        {'password': 'never-export-me'}, {'request_id': ['nested']},
                        {'protocol': True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.state.handle({**valid, **changes})
        for raw in (b'{"command":"status","command":"quit"}\n', b'{}\n{}\n',
                    b'{"sequence":NaN}\n', b'[]\n', b'x' * 4096):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                self.resident.decode_message(raw)
        self.assertNotIn('never-export-me', json.dumps(self.state.snapshot()))

    def test_mixed_install_root_or_release_cannot_control_a_live_session(self):
        record = {'root': self.root, 'version': self.resident.APP_VERSION, 'code_id': 'f' * 64}
        self.resident.match_install(record, self.root, 'f' * 64)
        for root, code, version in [(self.root + '-other', 'f' * 64, self.resident.APP_VERSION),
                                     (self.root, 'e' * 64, self.resident.APP_VERSION),
                                     (self.root, 'f' * 64, 'resident-v3')]:
            with self.subTest(root=root, code=code, version=version), self.assertRaises(ValueError):
                self.resident.match_install({**record, 'version': version}, root, code)

    def test_ack_records_service_receipt_time_and_current_qt_identity(self):
        identity = {'pid': 73, 'starttime': 12345, 'uid': 0}
        self.state.ui_started('c' * 32, identity)
        self.state.request('show', request_id='show', tab='detection')
        self.state.request('ack', request_id='ack', ui_instance='c' * 32, sequence=1,
                           visible=True, tab='detection', received_at=123.5,
                           frame_requests=8, frame_decodes=7, preview_presentations=6)
        ack = self.state.snapshot()['ack']
        self.assertEqual(ack['qt'], identity)
        self.assertEqual(ack['received_at'], 123.5)
        self.assertEqual(ack['frame_requests'], 8)
        self.assertEqual(ack['frame_decodes'], 7)
        for bad in (-1, True, 2 ** 40):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.state.request('ack', request_id='bad-' + str(bad), ui_instance='c' * 32,
                                   sequence=1, visible=True, tab='detection', frame_requests=bad)

    def test_deep_json_is_rejected_without_escaping_the_bounded_control_handler(self):
        raw = b'{"a":' + b'[' * 990 + b'0' + b']' * 990 + b'}\n'
        try:
            self.resident.decode_message(raw)
        except ValueError:
            pass
        except BaseException as error:
            self.fail('bounded JSON escaped the handler: ' + type(error).__name__)
        else:
            self.fail('deep JSON was accepted')


class LaunchArgumentsTests(unittest.TestCase):
    def launcher(self):
        try:
            return importlib.import_module('helmet.app.launch')
        except ImportError:
            self.fail('the launch client cannot parse/help on this host')

    def test_normal_icon_is_a_resident_show_client(self):
        args = self.launcher().parse_arguments(['--root', '/userdata/helmet-validation/v4', '--tab', 'wifi'])
        self.assertEqual(args.mode, 'resident')
        self.assertEqual(args.control, 'show')
        self.assertEqual(args.tab, 'wifi')

    def test_verification_flags_require_bounded_distinct_one_shot_dispatch(self):
        launcher = self.launcher()
        args = launcher.parse_arguments(['--smoke-seconds', '10', '--auto-demo', '--verify-keyboard'])
        self.assertEqual(args.mode, 'one-shot')
        for argv in (['--auto-demo'], ['--verify-scan'], ['--smoke-seconds', '-1'],
                     ['--smoke-seconds', '3601'], ['--smoke-seconds', '10', '--control', 'quit'],
                     ['--smoke-seconds', '10', '--verify-resident-ui']):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                launcher.parse_arguments(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_resident_ui_evidence_does_not_enable_automatic_detection_or_wifi(self):
        args = self.launcher().parse_arguments(['--verify-resident-ui'])
        self.assertEqual(args.mode, 'resident')
        self.assertFalse(args.auto_demo)
        self.assertFalse(args.verify_scan)


class OneShotJournalFailureTests(unittest.TestCase):
    def test_journal_failure_does_not_abandon_a_live_service_owner(self):
        """File and native-process boundaries are injected; the owner is real.

        This is software shutdown evidence, not a Linux FD or SDK claim.
        The owner thread releases only after a normal shutdown request.
        """
        resident = module(self)
        launcher = importlib.import_module('helmet.app.launch')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stop_requested, cleaned = threading.Event(), threading.Event()
            def owned_run():
                stop_requested.wait()
                cleaned.set()
            worker = threading.Thread(target=owned_run, daemon=False)
            worker.start()
            class ServiceOwner:
                pid = 73
                @property
                def returncode(self):
                    return None if worker.is_alive() else 0
                def poll(self):
                    return self.returncode
            service = ServiceOwner()
            leases = []
            io_descriptors, log_streams = [], []
            class Journal:
                writes = 0
                def __init__(self, directory=None):
                    self.directory = Path(directory) if directory is not None else root / 'coordinator'
                    self.directory.mkdir(parents=True, exist_ok=True)
                def prepare(self):
                    return self
                def read_record(self):
                    return None
                def assert_no_live_owners(self, value):
                    pass
                def acquire(self, name, timeout=0):
                    lease = resident.Lease(os.open(self.directory / name, os.O_RDWR | os.O_CREAT, 0o600))
                    leases.append(lease)
                    return lease
                def write_record(self, value):
                    Journal.writes += 1
                    if Journal.writes >= 2:
                        raise OSError('fixture journal unavailable')
                def open_file(self, name, flags, exclusive=False):
                    descriptor = os.open(self.directory / name, flags | os.O_CREAT | os.O_EXCL, 0o600)
                    io_descriptors.append(descriptor)
                    return descriptor
                def remove_socket(self):
                    pass
                def _stat(self, name):
                    return None
                def write_json(self, name, value):
                    raise OSError('fixture journal unavailable')
                def close(self):
                    pass
            def make_directory(path):
                Path(path).mkdir(parents=True, exist_ok=True)
                return Path(path)
            def normal_shutdown(*args, **kwargs):
                stop_requested.set()
                return 'normal_fixture_stop'
            def spawn_owner(*args, **kwargs):
                log_streams.append(kwargs['stdout'])
                return service
            args = launcher.parse_arguments(['--root', str(root), '--smoke-seconds', '2'])
            try:
                with patch.object(resident, 'Coordinator', Journal), \
                        patch.object(resident, 'validate_install', return_value=root), \
                        patch.object(resident, 'private_directory', side_effect=make_directory), \
                        patch.object(resident, 'process_identity', return_value={'pid': 73, 'starttime': 1, 'uid': 0}), \
                        patch.object(resident, 'request_service_shutdown', side_effect=normal_shutdown), \
                        patch.object(launcher.shutil, 'which', return_value='/fixture/qmlscene'), \
                        patch.object(launcher.subprocess, 'Popen', side_effect=spawn_owner), \
                        patch.object(launcher.signal, 'SIGHUP', 1, create=True), \
                        patch.object(launcher.signal, 'signal'):
                    try:
                        launcher.one_shot(args)
                    except OSError:
                        pass  # A failed journal is truthful failure, not success.
                self.assertTrue(cleaned.is_set(), 'journal I/O failure bypassed normal service shutdown')
                self.assertFalse(worker.is_alive(), 'return before the actual owner ended is unsafe')
            finally:
                stop_requested.set()
                worker.join(1)
                for stream in log_streams:
                    stream.close()
                for descriptor in io_descriptors:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                for lease in leases:
                    lease.close()


class OneShotDeadlineTests(unittest.TestCase):
    def test_live_ui_without_its_timer_requests_cleanup_and_retains_owner_leases(self):
        """Exercise one_shot with a live UI and a real blocked cleanup owner.

        Native/file/signal/clock boundaries are injected. Real flock, Qt and
        SDK cleanup require the separate Linux and hardware gates.
        """
        resident = module(self)
        launcher = importlib.import_module('helmet.app.launch')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stop_requested, release_owner = threading.Event(), threading.Event()
            observation, cleaned = threading.Event(), threading.Event()
            clock_lock = threading.Lock()
            clock_value = [100.0]
            def monotonic():
                with clock_lock:
                    return clock_value[0]
            def sleep(seconds):
                with clock_lock:
                    clock_value[0] += seconds
                    if clock_value[0] >= 105.1:
                        observation.set()
                time.sleep(.001)
            def owned_run():
                stop_requested.wait()
                release_owner.wait()
                cleaned.set()
            worker = threading.Thread(target=owned_run, daemon=False)
            worker.start()
            class ServiceOwner:
                pid = 73
                @property
                def returncode(self):
                    return None if worker.is_alive() else 0
                def poll(self):
                    return self.returncode
            service = ServiceOwner()
            ui_starts, ui_terminations, shutdown_times = [], [], []
            class LiveUi:
                pid = 74
                returncode = None
                def poll(self):
                    return self.returncode
                def terminate(self):
                    ui_terminations.append(service.poll())
                    self.returncode = -15
            ui = LiveUi()
            leases, receipts, phases, log_streams = {}, {}, [], []
            class Journal:
                def __init__(self, directory=None):
                    self.directory = Path(directory) if directory is not None else root / 'coordinator'
                    self.directory.mkdir(parents=True, exist_ok=True)
                def prepare(self):
                    return self
                def read_record(self):
                    return None
                def assert_no_live_owners(self, value):
                    pass
                def acquire(self, name, timeout=0):
                    lease = resident.Lease(os.open(self.directory / name, os.O_RDWR | os.O_CREAT, 0o600))
                    leases[name] = lease
                    return lease
                def write_record(self, value):
                    phases.append(value['phase'])
                def open_file(self, name, flags, exclusive=False):
                    return os.open(self.directory / name, flags | os.O_CREAT | os.O_EXCL, 0o600)
                def remove_socket(self):
                    pass
                def _stat(self, name):
                    return None
                def write_json(self, name, value):
                    receipts[name] = value
                def close(self):
                    pass
            def make_directory(path):
                Path(path).mkdir(parents=True, exist_ok=True)
                return Path(path)
            def normal_shutdown(*args, **kwargs):
                shutdown_times.append(monotonic())
                stop_requested.set()
                return 'normal_fixture_stop'
            def spawn_owner(argv, **kwargs):
                log_streams.append(kwargs['stdout'])
                if argv[0] == '/fixture/qmlscene':
                    ui_starts.append(monotonic())
                    return ui
                return service
            handlers, results, errors = {}, [], []
            def supervise():
                try:
                    results.append(launcher.one_shot(args))
                except BaseException as error:
                    errors.append(error)
                    observation.set()
            args = launcher.parse_arguments(['--root', str(root), '--smoke-seconds', '2'])
            supervisor = threading.Thread(target=supervise, daemon=False)
            with patch.object(resident, 'Coordinator', Journal), \
                    patch.object(resident, 'validate_install', return_value=root), \
                    patch.object(resident, 'private_directory', side_effect=make_directory), \
                    patch.object(resident, 'process_identity', side_effect=lambda pid: {'pid': pid, 'starttime': 1, 'uid': 0}), \
                    patch.object(resident, 'service_ready', return_value={'port': 12345}), \
                    patch.object(resident, 'service_token', return_value='fixture-token'), \
                    patch.object(resident, 'write_qml_wrapper', side_effect=lambda workspace, name, *rest: workspace.directory / name), \
                    patch.object(resident, 'request_service_shutdown', side_effect=normal_shutdown), \
                    patch.object(launcher, 'time', SimpleNamespace(monotonic=monotonic, sleep=sleep)), \
                    patch.object(launcher.shutil, 'which', return_value='/fixture/qmlscene'), \
                    patch.object(launcher.subprocess, 'Popen', side_effect=spawn_owner), \
                    patch.object(launcher.signal, 'SIGHUP', 1, create=True), \
                    patch.object(launcher.signal, 'signal', side_effect=lambda number, handler: handlers.update({number: handler})):
                try:
                    supervisor.start()
                    self.assertTrue(observation.wait(2), 'the controlled supervisor did not reach the observation point')
                    self.assertEqual(errors, [])
                    self.assertTrue(stop_requested.is_set(), 'a live Qt process without its timer bypassed the smoke deadline')
                    self.assertEqual(len(shutdown_times), 1)
                    self.assertGreaterEqual(shutdown_times[0] - ui_starts[0], 2.0)
                    self.assertLessEqual(shutdown_times[0] - ui_starts[0], 2.11)
                    self.assertIn('stopping', phases)
                    self.assertTrue(supervisor.is_alive(), 'cleanup returned while the real owner remained live')
                    self.assertTrue(worker.is_alive())
                    self.assertIsNone(ui.poll())
                    self.assertEqual(ui_terminations, [])
                    for name in ('manager.lock', 'resource.lock'):
                        os.fstat(leases[name].fileno())
                    release_owner.set()
                    supervisor.join(2)
                    self.assertFalse(supervisor.is_alive())
                    self.assertTrue(cleaned.is_set())
                    self.assertEqual(errors, [])
                    self.assertEqual(results, [1], 'forced Qt termination must retain the existing failed exit status')
                    self.assertEqual(ui_terminations, [0], 'Qt must end after the service owner')
                    self.assertTrue(receipts['exit.json']['cleanup_completed'])
                    self.assertTrue(receipts['exit.json']['resource_lease_released'])
                    self.assertEqual(phases[-1], 'failed')
                    self.assertIsNone(leases['manager.lock'].descriptor)
                    self.assertIsNone(leases['resource.lock'].descriptor)
                finally:
                    # A RED assertion must still normally release both test owners.
                    if supervisor.is_alive() and launcher.signal.SIGTERM in handlers:
                        handlers[launcher.signal.SIGTERM](launcher.signal.SIGTERM, None)
                    stop_requested.set()
                    release_owner.set()
                    worker.join(2)
                    if supervisor.ident is not None:
                        supervisor.join(2)
                    for stream in log_streams:
                        stream.close()
                    for lease in leases.values():
                        lease.close()


class DirectoryOwnershipTests(unittest.TestCase):
    """Injected POSIX metadata; actual Linux rename/FD behavior is separate."""
    def posix_lstat(self, owners, modes, app_uid):
        original = Path.lstat
        def lstat(path):
            path = Path(path)
            row = original(path)
            directory = stat.S_ISDIR(row.st_mode)
            mode = stat.S_IFDIR | 0o755 if directory else stat.S_IFREG | 0o644
            return SimpleNamespace(st_mode=modes.get(path, mode),
                                   st_uid=owners.get(path, 0 if directory else app_uid))
        return lstat

    def test_foreign_ancestor_rejects_before_private_state_creation_even_when_sticky(self):
        resident = module(self)
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / 'foreign-parent'
            parent.mkdir()
            for app_uid in (0, 1000):
                for permission in (0o755, 0o1777):
                    leaf = parent / ('state-' + str(app_uid) + '-' + oct(permission)[2:])
                    owners = {parent: 1001, leaf: app_uid}
                    modes = {parent: stat.S_IFDIR | permission, leaf: stat.S_IFDIR | 0o700}
                    with self.subTest(app_uid=app_uid, permission=oct(permission)), \
                            patch.object(resident, 'require_linux'), \
                            patch.object(resident.os, 'geteuid', return_value=app_uid, create=True), \
                            patch.object(Path, 'lstat', self.posix_lstat(owners, modes, app_uid)):
                        with self.assertRaises(ValueError):
                            resident.private_directory(leaf)
                        self.assertFalse(leaf.exists(), 'unsafe ancestry must reject before mkdir')

    def test_root_or_app_owned_sticky_ancestor_preserves_app_owned_private_state(self):
        resident = module(self)
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / 'trusted-sticky'
            parent.mkdir()
            for ancestor_uid in (0, 1000):
                leaf = parent / ('state-' + str(ancestor_uid))
                owners = {parent: ancestor_uid, leaf: 1000}
                modes = {parent: stat.S_IFDIR | 0o1777, leaf: stat.S_IFDIR | 0o700}
                with self.subTest(ancestor_uid=ancestor_uid), \
                        patch.object(resident, 'require_linux'), \
                        patch.object(resident.os, 'geteuid', return_value=1000, create=True), \
                        patch.object(Path, 'lstat', self.posix_lstat(owners, modes, 1000)):
                    self.assertEqual(resident.private_directory(leaf), leaf)

    def test_trusted_writable_ancestor_requires_sticky_protection(self):
        resident = module(self)
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / 'unprotected-parent'
            parent.mkdir()
            leaf = parent / 'state'
            modes = {parent: stat.S_IFDIR | 0o777, leaf: stat.S_IFDIR | 0o700}
            for ancestor_uid in (0, 1000):
                with self.subTest(ancestor_uid=ancestor_uid), \
                        patch.object(resident, 'require_linux'), \
                        patch.object(resident.os, 'geteuid', return_value=1000, create=True), \
                        patch.object(Path, 'lstat', self.posix_lstat({parent: ancestor_uid, leaf: 1000}, modes, 1000)):
                    with self.assertRaises(ValueError):
                        resident.private_directory(leaf)
                    self.assertFalse(leaf.exists())

    def test_acquire_rechecks_ancestor_ownership_before_opening_a_lease(self):
        resident = module(self)
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / 'changed-parent'
            leaf = parent / 'state'
            leaf.mkdir(parents=True)
            owners = {parent: 0, leaf: 0}
            modes = {leaf: stat.S_IFDIR | 0o700}
            coordinator = resident.Coordinator(leaf)
            coordinator.descriptor = 42  # Inject only the previously opened directory FD.
            with patch.object(resident.os, 'geteuid', return_value=0, create=True), \
                    patch.object(Path, 'lstat', self.posix_lstat(owners, modes, 0)), \
                    patch.object(coordinator, 'open_file', side_effect=AssertionError('lease IO reached an unsafe namespace')) as open_file:
                resident._check_ancestors(leaf)
                owners[parent] = 1001
                with self.assertRaises(ValueError):
                    coordinator.acquire('resource.lock')
                open_file.assert_not_called()

    def test_install_namespaces_require_app_ownership_and_safe_modes_including_qml(self):
        resident = module(self)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / 'release'
            app = root / 'src' / 'app'
            (app / 'qml').mkdir(parents=True)
            for name in ('resident.py', 'launch.py', 'service.py', 'wifi.py', 'qml/Main.qml'):
                (app / name).write_text('fixture', encoding='utf-8')
            directories = (root, root / 'src', app, app / 'qml')
            owners = dict.fromkeys(directories, 1000)
            modes = {}
            with patch.object(resident, 'require_linux'), \
                    patch.object(resident, 'VALIDATION_ROOT', base), \
                    patch.object(resident.os, 'geteuid', return_value=1000, create=True), \
                    patch.object(Path, 'lstat', self.posix_lstat(owners, modes, 1000)):
                self.assertEqual(resident.validate_install(root, app), root)
                for directory in directories:
                    for uid in (1001, 0):
                        owners[directory] = uid
                        with self.subTest(directory=directory.relative_to(base), uid=uid), self.assertRaises(ValueError):
                            resident.validate_install(root, app)
                    owners[directory] = 1000
                    modes[directory] = stat.S_IFDIR | 0o1777
                    with self.subTest(directory=directory.relative_to(base), mode='1777'), self.assertRaises(ValueError):
                        resident.validate_install(root, app)
                    modes.pop(directory)


def qml_functions(names):
    """Execute the shipped JS functions at their UI/HTTP boundary, not a copy."""
    text = (APP_DIR / 'qml/Main.qml').read_text(encoding='utf-8')
    bodies = []
    for name in names:
        found = re.search(r'function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', text)
        if not found:
            raise AssertionError('QML behavior missing: ' + name)
        depth, quote, escaped = 1, None, False
        index = found.end()
        while depth and index < len(text):
            char = text[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in ('"', "'", '`'):
                quote = char
            elif char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
            index += 1
        if depth:
            raise AssertionError('unterminated QML function: ' + name)
        bodies.append(text[found.start():index])
    return '\n'.join(bodies)


@unittest.skipUnless(NODE, 'Node unavailable for shipped QML JS boundary checks')
class QmlResidentTests(unittest.TestCase):
    def run_js(self, names, body, model_reset=False):
        prelude = """
const assert = require('assert');
var modelChoices=[], sourceChoices=[], profileChoices=[], detection={}, haveConfig=false;
var modelChoice={currentIndex:-1}, sourceChoice={currentIndex:-1}, profileChoice={currentIndex:-1};
var message='', requests=[], previewEpoch=0, frameExpires=0, framePending=false, lastStatus=0;
var frameLoadPending=false, previewLoadTarget=null, previewSwapPending=false;
var previewSwapTarget=null, previewSwapSerial=0;
var previewHideTimer={stop(){this.stopped=true},restart(){this.restarted=true}};
var minimumCaptureAt=0, previewGeneration=null, previewSourceEpoch=null, previewCapturedAt=0;
var previewFrameId=null, previewSelection={}, frameRequests=0, frameDecodes=0, previewPresentations=0;
var previewA={visible:false,opacity:0,z:0,source:''}, previewB={visible:false,opacity:0,z:0,source:''};
var preview=previewA, previewVisible=false, application={}, appStopping=false, appliedSequence=-1;
var appSession='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', uiInstance='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
var residentMode=true, applyingIntent=false, configPending=false, configEpoch=0;
var statusPending=false, keyboardOpen=false, password={text:''}, automaticDemo=false;
var passwordVisible=false, keyboardTarget=null, frameRequest=null, frameSerial=0;
var initialTab='detection', requestCounter=0, actionState={}, lastActionId='', networks=[];
var verificationResidentUi=false, verificationDir='', capturePending=false, captureSequence=-1;
var tabs={currentIndex:0};
var window={visible:true,show(){this.visible=true},showFullScreen(){this.visible=true},hide(){this.visible=false},raise(){},requestActivate(){}};
function request(method,route,payload,callback){requests.push({method,route,payload,callback})}
function updateWifi(){}
"""
        try:
            functions = qml_functions(names)
        except AssertionError as error:
            self.fail(str(error))
        script = prelude + functions + '\n' + body
        context_setup = """
const vm=require('vm');
var context={require:require,console:console};context.global=context;
"""
        if model_reset:
            context_setup += """
context._sources=[];
Object.defineProperty(context,'sourceChoices',{configurable:true,
 get(){return this._sources},set(value){this._sources=value;
 if(this.sourceChoice)this.sourceChoice.currentIndex=value.length?0:-1;}});
"""
        program = context_setup + '\nvm.createContext(context);vm.runInContext(' + json.dumps(script) + ',context);'
        result = subprocess.run([NODE, '-'], input=program,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_restore_preserves_running_absent_source_and_actual_model(self):
        self.run_js(['chooseIndex', 'selectionIds', 'applyConfiguration', 'invalidatePreview'], """
applyConfiguration({models:[{id:'helmet_416x416.rknn',name:'416'}],
 sources:[{id:'1.jpg',name:'replay'}],profiles:[{id:'responsive',name:'20 FPS'}],
 defaults:{model:'helmet_416x416.rknn',source:'1.jpg',profile:'responsive'},
 detection:{running:true,generation:'run-1',selection:{model:'helmet_416x416.rknn',source:'/dev/video2',profile:'responsive'}}});
assert.equal(sourceChoices[sourceChoice.currentIndex].id,'/dev/video2');
assert.equal(sourceChoices[sourceChoice.currentIndex].available,false);
assert.equal(modelChoices[modelChoice.currentIndex].id,'helmet_416x416.rknn');
assert.equal(detection.generation,'run-1');
assert.equal(requests.length,0,'restoring configuration must never start detection');
""")

    def test_idle_refresh_requires_explicit_reselection_when_old_input_is_lost(self):
        self.run_js(['chooseIndex', 'selectionIds', 'applyConfiguration', 'canStart', 'invalidatePreview'], """
haveConfig=true; modelChoices=[{id:'helmet_416x416.rknn'}];modelChoice.currentIndex=0;
sourceChoices=[{id:'removed.jpg'}];sourceChoice.currentIndex=0;
profileChoices=[{id:'replay'}];profileChoice.currentIndex=0;
applyConfiguration({models:[{id:'helmet_416x416.rknn'}],sources:[{id:'1.jpg'}],profiles:[{id:'replay'}],
 defaults:{model:'helmet_416x416.rknn',source:'1.jpg',profile:'replay'},detection:{running:false,selection:{}}});
assert.equal(sourceChoice.currentIndex,-1);
assert.equal(canStart(),false);
sourceChoice.currentIndex=0;
assert.equal(canStart(),true);
""")

    def test_combo_model_replacement_cannot_reset_a_lost_idle_source_to_the_first_image(self):
        self.run_js(['chooseIndex', 'selectionIds', 'applyConfiguration', 'canStart', 'invalidatePreview'], """
haveConfig=true;modelChoices=[{id:'helmet_416x416.rknn'}];modelChoice.currentIndex=0;
sourceChoices=[{id:'removed.jpg'}];sourceChoice.currentIndex=0;
profileChoices=[{id:'replay'}];profileChoice.currentIndex=0;
applyConfiguration({models:[{id:'helmet_416x416.rknn'}],sources:[{id:'1.jpg'}],profiles:[{id:'replay'}],
 defaults:{model:'helmet_416x416.rknn',source:'1.jpg',profile:'replay'},detection:{running:false,selection:{}}});
assert.equal(sourceChoice.currentIndex,-1);assert.equal(canStart(),false);
""", model_reset=True)

    def test_input_refresh_pending_disables_start_while_preserving_idle_choice(self):
        self.run_js(['canStart'], """
haveConfig=true;detection={running:false};modelChoices=[{id:'helmet_416x416.rknn'}];modelChoice.currentIndex=0;
sourceChoices=[{id:'chosen.jpg'}];sourceChoice.currentIndex=0;profileChoices=[{id:'replay'}];profileChoice.currentIndex=0;
configPending=true;assert.equal(canStart(),false);assert.equal(sourceChoices[sourceChoice.currentIndex].id,'chosen.jpg');
configPending=false;assert.equal(canStart(),true);
""")

    def test_configuration_generation_or_epoch_change_invalidates_the_old_decoded_preview(self):
        self.run_js(['chooseIndex', 'selectionIds', 'applyConfiguration', 'canStart', 'invalidatePreview'], """
detection={running:true,valid:true,generation:'old',source_epoch:0};preview.source='OLD';previewVisible=true;
applyConfiguration({models:[{id:'m'}],sources:[{id:'s'}],profiles:[{id:'replay'}],
 detection:{running:true,valid:false,generation:'new',source_epoch:2,selection:{model:'m',source:'s',profile:'replay'}}});
assert.equal(preview.source,'');assert.equal(previewVisible,false);assert.equal(detection.generation,'new');
""")

    def test_hide_and_reshow_discard_inflight_preview_and_restore_without_start(self):
        self.run_js(['nextRequestId', 'invalidatePreview', 'clearUiSecrets', 'ackApplication', 'applyApplication', 'refreshFrame'], """
var configs=0,polls=0;function configure(){configs++} function poll(){polls++}
detection={running:true,valid:true,generation:'run-1',source_epoch:3};lastStatus=Date.now();
refreshFrame();var old=requests.shift();assert.equal(old.route.indexOf('/api/preview'),0);
applyApplication({session:appSession,phase:'running',intent:{sequence:1,visible:false,tab:'detection',preview_after:0}});
assert.equal(window.visible,false);assert.equal(preview.source,'');
old.callback(true,{valid:true,generation:'run-1',source_epoch:3,captured_at:12,remaining_ttl_ms:900,jpeg_base64:'OLD'});
assert.equal(preview.source,'');
applyApplication({session:appSession,phase:'running',intent:{sequence:2,visible:true,tab:'detection',preview_after:20}});
assert.equal(window.visible,true);assert.equal(configs,1);assert.equal(polls,1);assert.equal(minimumCaptureAt,20);
lastStatus=Date.now();requests=[];refreshFrame();var fresh=requests.shift();
fresh.callback(true,{valid:true,generation:'run-1',source_epoch:3,captured_at:19,remaining_ttl_ms:900,jpeg_base64:'OLD'});
assert.equal(preview.source,'','pre-show captures must stay hidden');
lastStatus=Date.now();requests=[];refreshFrame();fresh=requests.shift();
fresh.callback(true,{valid:true,generation:'run-1',source_epoch:3,captured_at:21,remaining_ttl_ms:900,jpeg_base64:'NEW'});
assert.equal(previewB.source,'data:image/jpeg;base64,NEW');
assert.equal(detection.generation,'run-1');
""")

    def test_hidden_window_never_requests_or_decodes_a_frame(self):
        self.run_js(['invalidatePreview', 'refreshFrame'], """
window.visible=false;detection={running:true,valid:true,generation:'run-1',source_epoch:0};lastStatus=Date.now();
refreshFrame();assert.equal(requests.length,0);assert.equal(preview.source,'');
""")

    def test_hide_clears_gui_password_and_keyboard_without_cancelling_backend_wifi(self):
        self.run_js(['clearUiSecrets'], """
password.text='private-password';passwordVisible=true;keyboardOpen=true;keyboardTarget=password;
actionState={busy:true,kind:'connect'};clearUiSecrets();
assert.equal(password.text,'');assert.equal(passwordVisible,false);assert.equal(keyboardOpen,false);
assert.equal(keyboardTarget,null);assert.equal(actionState.busy,true);assert.equal(requests.length,0);
""")

    def test_async_native_capture_freezes_replacement_and_fails_if_displayed_packet_expires(self):
        self.run_js(['nextRequestId', 'invalidatePreview', 'refreshFrame', 'captureShow'], """
verificationResidentUi=true;verificationDir='/private/session';appliedSequence=3;haveConfig=true;
window.width=720;window.height=1120;window.contentItem={parent:{grabToImage(cb){global.captureCallback=cb}}};
detection={running:true,valid:true,generation:'run-1',source_epoch:0,frame_id:99,selection:{model:'m',source:'s',profile:'replay'}};
lastStatus=Date.now();previewVisible=true;preview.source='data:image/jpeg;base64,FRAME7';
previewGeneration='run-1';previewSourceEpoch=0;previewFrameId=7;previewCapturedAt=20;
previewSelection={model:'m',source:'s',profile:'replay'};frameExpires=Date.now()+500;
captureShow();assert.equal(capturePending,true);
refreshFrame();assert.equal(requests.length,0,'native capture must not replace the displayed frame');
frameExpires=Date.now()-1;
var saves=0;captureCallback({saveToFile(){saves++;return true}});
assert.equal(capturePending,false);assert.equal(saves,0,'expired capture must not be saved as fresh');
var receipt=requests.find(x=>x.route==='/api/application/render');
assert.equal(receipt.payload.packet.frame_id,7,'receipt binds actual Image.Ready packet, not latest status frame');
assert.equal(receipt.payload.saved,false);
""")

    def test_explicit_capture_never_saves_an_entered_gui_password(self):
        self.run_js(['nextRequestId', 'captureShow'], """
verificationResidentUi=true;verificationDir='/private/session';appliedSequence=1;haveConfig=true;
password.text='private-password';passwordVisible=true;tabs.currentIndex=1;detection={running:false};lastStatus=Date.now();
var grabs=0;window.contentItem={parent:{grabToImage(){grabs++}}};
captureShow();assert.equal(grabs,0);assert.equal(requests.length,0);assert.equal(capturePending,false);
""")

    def test_native_show_capture_waits_for_refreshed_config_and_current_status(self):
        self.run_js(['nextRequestId', 'captureShow'], """
verificationResidentUi=true;verificationDir='/private/session';appliedSequence=1;haveConfig=true;
tabs.currentIndex=1;detection={running:false};lastStatus=Date.now();
var grabs=0;window.contentItem={parent:{grabToImage(){grabs++}}};
configPending=true;captureShow();assert.equal(grabs,0);assert.equal(captureSequence,-1);
configPending=false;statusPending=true;captureShow();assert.equal(grabs,0);
statusPending=false;lastStatus=0;captureShow();assert.equal(grabs,0);
lastStatus=Date.now();captureShow();assert.equal(grabs,1);assert.equal(capturePending,true);
""")


@unittest.skipUnless(sys.platform.startswith('linux'), 'requires Linux flock/proc/SO_PEERCRED; no Windows kernel claim')
class LinuxResidentFixtureTests(unittest.TestCase):
    def setUp(self):
        self.resident = module(self)
        self.temp = tempfile.TemporaryDirectory(prefix='helmet-resident-fixture-', dir=FIXTURE_PARENT)
        self.path = Path(self.temp.name) / 'state'
        self.coordinator = self.resident.Coordinator(self.path)
        self.coordinator.prepare()
        self.children = []
        self.listeners = []
        self.fixture_manager = False

    def tearDown(self):
        for listener in self.listeners:
            listener.close()
        for child in self.children:
            if child.poll() is None and self.fixture_manager:
                record = self.coordinator.read_record()
                if record is not None and self.resident.identity_alive(record['manager']):
                    message = {'protocol': 1, 'version': self.resident.APP_VERSION,
                               'root': record['root'], 'session': record['session'],
                               'command': 'quit', 'request_id': 'fixture-cleanup'}
                    self.resident.exchange(self.coordinator, message, record['manager'])
            if child.poll() is None and child.stdin:
                try:
                    child.stdin.write(b'x')
                    child.stdin.flush()
                except BrokenPipeError:
                    pass
            child.wait(timeout=5)
        self.coordinator.close()
        self.temp.cleanup()

    def test_inherited_service_lease_blocks_restart_after_manager_fd_disappears(self):
        lease = self.coordinator.acquire('resource.lock')
        script = ('import os,sys; from ' + RESIDENT_MODULE + ' import retain_resource_lease; '
                  'lease=retain_resource_lease(int(sys.argv[1]),sys.argv[2]); '
                  'sys.stdout.write(str(os.getsid(0))+"\\n");sys.stdout.flush(); '
                  'sys.stdin.buffer.read(1)')
        child = subprocess.Popen([sys.executable, '-c', script, str(lease.fileno()), str(self.path)],
                                 pass_fds=(lease.fileno(),), start_new_session=True,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO)
        self.children.append(child)
        self.assertEqual(int(child.stdout.readline()), child.pid, 'real detached POSIX session')
        lease.close()  # Close, never LOCK_UN: the real service inherits this open file description.
        with self.assertRaises(RuntimeError):
            self.coordinator.acquire('resource.lock')
        with self.assertRaises(RuntimeError):
            self.coordinator.assert_no_live_owners(None)
        child.stdin.write(b'x')
        child.stdin.flush()
        self.assertEqual(child.wait(timeout=5), 0)
        with self.coordinator.acquire('resource.lock'):
            pass

    def test_foreign_owner_rename_cannot_recreate_coordinator_while_inherited_lease_is_live(self):
        if os.geteuid() != 0:
            self.skipTest('requires root with chown/setuid capabilities for an isolated foreign-owner rename')
        from select import select
        parent = Path(self.temp.name)
        original = parent.lstat()
        lease = self.coordinator.acquire('resource.lock')
        lease_row = os.fstat(lease.fileno())
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        child, probe_fd = None, None
        replacement = self.resident.Coordinator(self.path)
        try:
            script = ('import os,sys; from ' + RESIDENT_MODULE + ' import retain_resource_lease; '
                      'lease=retain_resource_lease(int(sys.argv[1]),sys.argv[2]); '
                      'print("retained",flush=True); sys.stdin.buffer.read(1); lease.close()')
            child = subprocess.Popen([sys.executable, '-c', script, str(lease.fileno()), str(self.path)],
                                     pass_fds=(lease.fileno(),), start_new_session=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO)
            self.children.append(child)
            self.assertTrue(select([child.stdout], [], [], 5)[0], 'inherited lease readiness timed out')
            self.assertEqual(child.stdout.readline(), b'retained\n')
            lease.close()
            with self.assertRaises(RuntimeError):
                self.coordinator.acquire('resource.lock')
            try:
                os.chmod(parent, 0o755)
                os.chown(parent, 1001, -1)
            except PermissionError:
                self.skipTest('root lacks chown capability for the isolated ownership fixture')
            rename_script = ('import os,sys\n'
                             'try:\n'
                             '    os.setgroups([]); os.setgid(1001); os.setuid(1001)\n'
                             'except PermissionError:\n'
                             '    raise SystemExit(77)\n'
                             'os.rename("state","retired",src_dir_fd=int(sys.argv[1]),dst_dir_fd=int(sys.argv[1]))\n'
                             'print(os.geteuid())\n')
            renamed = subprocess.run([sys.executable, '-c', rename_script, str(parent_fd)],
                                     pass_fds=(parent_fd,), capture_output=True, timeout=5, cwd=REPO)
            if renamed.returncode == 77:
                self.skipTest('root lacks setuid/setgid capability for the isolated foreign-owner rename')
            self.assertEqual(renamed.returncode, 0, renamed.stderr.decode('utf-8', errors='replace'))
            self.assertEqual(renamed.stdout, b'1001\n', 'the rename must execute as the actual foreign UID')
            self.assertFalse(self.path.exists())
            self.assertTrue((parent / 'retired').is_dir())
            # The old dir FD still reaches the original locked inode after rename.
            probe_fd = os.open('resource.lock', os.O_RDWR | os.O_NOFOLLOW,
                               dir_fd=self.coordinator.descriptor)
            probe_row = os.fstat(probe_fd)
            self.assertEqual((probe_row.st_dev, probe_row.st_ino), (lease_row.st_dev, lease_row.st_ino))
            with self.assertRaises(BlockingIOError):
                self.resident.fcntl.flock(probe_fd, self.resident.fcntl.LOCK_EX | self.resident.fcntl.LOCK_NB)
            self.assertIsNone(child.poll())
            with self.assertRaises(ValueError):
                replacement.prepare()
            self.assertFalse(self.path.exists(), 'the old coordinator pathname must not be recreated')
            with self.assertRaises(ValueError):
                self.coordinator.acquire('resource.lock')
            child.stdin.write(b'x')
            child.stdin.flush()
            self.assertEqual(child.wait(timeout=5), 0)
            self.resident.fcntl.flock(probe_fd, self.resident.fcntl.LOCK_EX | self.resident.fcntl.LOCK_NB)
        finally:
            # Restore only this owned fixture parent before ordinary fixture cleanup.
            if parent.lstat().st_uid != original.st_uid:
                os.chown(parent, original.st_uid, original.st_gid)
            os.chmod(parent, stat.S_IMODE(original.st_mode))
            if child is not None:
                if child.poll() is None:
                    try:
                        child.stdin.write(b'x')
                        child.stdin.flush()
                    except BrokenPipeError:
                        pass
                child.wait(timeout=5)
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()
            replacement.close()
            lease.close()
            if probe_fd is not None:
                os.close(probe_fd)
            os.close(parent_fd)

    def test_bad_permissions_symlink_record_and_symlink_socket_are_rejected(self):
        self.coordinator.close()
        os.chmod(self.path, 0o755)
        with self.assertRaises(ValueError):
            self.resident.Coordinator(self.path).prepare()
        os.chmod(self.path, 0o700)
        self.coordinator.prepare()
        outside = Path(self.temp.name) / 'outside'
        outside.write_text('untouched')
        for name, operation in [('record.json', self.coordinator.read_record),
                                ('resource.lock', lambda: self.coordinator.acquire('resource.lock')),
                                ('control.sock', self.coordinator.check_socket)]:
            target = self.path / name
            target.symlink_to(outside)
            try:
                with self.assertRaises(ValueError):
                    operation()
                self.assertEqual(outside.read_text(), 'untouched')
            finally:
                target.unlink()

    def test_record_read_retries_when_atomic_replace_unlinks_the_open_snapshot(self):
        self.coordinator.write_json('service-state.json', {'sequence': 1})
        self.coordinator.write_json('next.json', {'sequence': 2})
        old_inode = (self.path / 'service-state.json').stat().st_ino
        real_fstat = self.resident.os.fstat
        replaced = False

        def replace_after_open(descriptor):
            nonlocal replaced
            row = real_fstat(descriptor)
            if not replaced and row.st_ino == old_inode:
                os.replace('next.json', 'service-state.json',
                           src_dir_fd=self.coordinator.descriptor,
                           dst_dir_fd=self.coordinator.descriptor)
                replaced = True
                row = real_fstat(descriptor)
            return row

        with patch.object(self.resident.os, 'fstat', side_effect=replace_after_open):
            value = self.coordinator.read_json('service-state.json')

        self.assertTrue(replaced, 'the fixture must replace the entry after it is opened')
        self.assertEqual(value, {'sequence': 2})

    def test_pid_reuse_or_live_orphan_is_never_treated_as_a_free_installation(self):
        identity = self.resident.process_identity(os.getpid())
        stale = {**identity, 'starttime': identity['starttime'] - 1}
        self.assertFalse(self.resident.identity_alive(stale))
        record = {'manager': stale, 'service': identity, 'ui': None}
        with self.assertRaises(RuntimeError):
            self.coordinator.assert_no_live_owners(record)
        self.coordinator.assert_no_live_owners({'manager': stale, 'service': None, 'ui': None})

    def test_real_socket_checks_server_peer_and_orders_concurrent_icon_controls(self):
        state = self.resident.ApplicationState('/userdata/helmet-validation/fixture-v4', 'a' * 32)
        state.mark_running()
        state.ui_started('b' * 32)
        listener = self.resident.ControlServer(self.coordinator, state)
        listener.start()
        self.listeners.append(listener)
        expected = self.resident.process_identity(os.getpid())
        def click(tab):
            payload = state.message('show', request_id='click-' + tab, tab=tab)
            return self.resident.exchange(self.coordinator, payload, expected)
        with ThreadPoolExecutor(max_workers=2) as clients:
            replies = list(clients.map(click, ('detection', 'wifi')))
        self.assertEqual(sorted(value['application']['intent']['sequence'] for value in replies), [1, 2])
        wrong = {**expected, 'starttime': expected['starttime'] - 1}
        with self.assertRaises(ValueError):
            self.resident.exchange(self.coordinator, state.message('status'), wrong)
        with patch.object(self.resident, 'peer_identity', return_value={**expected, 'uid': expected['uid'] + 1}):
            with self.assertRaises(ValueError):
                self.resident.exchange(self.coordinator, state.message('status'), expected)

    def test_socket_rejects_bad_json_and_times_out_partial_clients(self):
        state = self.resident.ApplicationState('/userdata/helmet-validation/fixture-v4', 'a' * 32)
        state.mark_running()
        listener = self.resident.ControlServer(self.coordinator, state)
        listener.start()
        self.listeners.append(listener)
        for raw in (b'{}\n', b'[]\n', b'x' * 4096):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(3)
                client.connect(str(self.path / 'control.sock'))
                client.sendall(raw)
                value = json.loads(client.recv(4096))
                self.assertFalse(value['ok'])
                self.assertNotIn(raw.decode(errors='replace'), json.dumps(value))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(self.path / 'control.sock'))
            client.sendall(b'{')
            started = time.monotonic()
            value = json.loads(client.recv(4096))
            self.assertFalse(value['ok'])
            self.assertLess(time.monotonic() - started, 2.5)

    def test_actual_client_startup_and_two_icons_share_one_detached_fixture_service(self):
        # Only the native executable boundary/install location are substituted
        # in this test utility. The real client, startup flock, records, peer
        # validation, ordered control and inherited lease all execute on Linux.
        # The child is an FD holder, not Qt, WiFi or an SDK replacement in App code.
        self.fixture_manager = True
        install = Path(self.temp.name) / 'install'
        install.mkdir(mode=0o700)
        other = Path(self.temp.name) / 'other-install'
        other.mkdir(mode=0o700)
        original_coordinator = self.resident.Coordinator
        original_popen = subprocess.Popen
        fingerprint = self.resident.code_identity(APP_DIR)
        def fixture_coordinator(directory=None):
            return original_coordinator(self.path if directory is None else directory)
        def fixture_install(root, app):
            if Path(root) not in (install, other):
                raise ValueError('fixture installation is outside the isolated directory')
            return Path(root)
        def fixture_manager_command(record):
            self.assertTrue(self.resident.identity_alive(record['manager']))
            argv = (Path('/proc') / str(record['manager']['pid']) / 'cmdline').read_bytes().split(b'\0')
            self.assertIn(os.fsencode(Path(__file__).resolve()), argv)
            self.assertIn(b'--fixture-manager', argv)
            self.assertIn(os.fsencode(install), argv)
        def spawn_fixture(argv, **kwargs):
            self.assertEqual(argv[:2], [sys.executable, str(APP_DIR / 'resident.py')])
            self.assertTrue(kwargs['start_new_session'])
            child = original_popen([sys.executable, str(Path(__file__).resolve()), '--fixture-manager',
                                    str(self.path), str(install), fingerprint], cwd=REPO, **kwargs)
            self.children.append(child)
            return child
        with patch.object(self.resident, 'Coordinator', side_effect=fixture_coordinator), \
                patch.object(self.resident, 'validate_install', side_effect=fixture_install), \
                patch.object(self.resident, 'verify_manager_command', side_effect=fixture_manager_command), \
                patch.object(self.resident.subprocess, 'Popen', side_effect=spawn_fixture):
            with ThreadPoolExecutor(max_workers=2) as clicks:
                replies = list(clicks.map(lambda tab: self.resident.client(install, tab), ('detection', 'wifi')))
            first, second = (value['application'] for value in replies)
            self.assertEqual(first['session'], second['session'])
            self.assertEqual(first['service'], second['service'])
            before_ensure = self.resident.client(install, command='status')['application']
            preserved = self.resident.client(install, background=True)['application']
            self.assertEqual(preserved['session'], second['session'])
            self.assertEqual(preserved['service'], second['service'])
            self.assertEqual(preserved['intent'], before_ensure['intent'], 'boot ensure must not change the open window')
            record = self.coordinator.read_record()
            self.assertEqual(os.getsid(record['manager']['pid']), record['manager']['pid'])
            self.assertTrue(self.resident.identity_alive(record['service']))
            self.assertEqual(len(self.children), 1, 'one real manager process and one lease-holder child')
            with self.assertRaises(RuntimeError):
                self.coordinator.acquire('resource.lock')
            with self.assertRaises(ValueError):
                self.resident.client(other, 'wifi')
            again = self.resident.client(install, 'detection')['application']
            self.assertEqual(again['session'], first['session'])
            self.assertEqual(again['service'], first['service'])
            self.resident.client(install, command='quit')
            self.assertEqual(self.children[0].wait(timeout=5), 0)
        with self.coordinator.acquire('resource.lock'):
            pass


def fixture_lease_holder(descriptor, directory, session):
    """Bounded test process with no device, HTTP, Qt or WiFi initialization."""
    resident = importlib.import_module(RESIDENT_MODULE)
    lease = resident.retain_resource_lease(descriptor, directory)
    coordinator = resident.Coordinator(directory).prepare()
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if coordinator.read_json('fixture-release-' + session + '.json') is not None:
                return 0
            time.sleep(.02)
        return 1
    finally:
        lease.close()
        coordinator.close()


def fixture_manager(directory, install, fingerprint):
    resident = importlib.import_module(RESIDENT_MODULE)
    coordinator = resident.Coordinator(directory).prepare()
    coordinator.assert_no_live_owners(coordinator.read_record())
    manager_lease = coordinator.acquire('manager.lock')
    resource = coordinator.acquire('resource.lock')
    session = __import__('uuid').uuid4().hex
    runtime = resident.private_directory(Path(install) / 'app-state')
    workspace = resident.Coordinator(runtime / ('session-' + session)).prepare()
    state = resident.ApplicationState(install, session)
    state.request('show', request_id='fixture-initial', tab='detection')
    manager_id = resident.process_identity(os.getpid())
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--fixture-lease-holder',
                              str(resource.fileno()), str(directory), session], pass_fds=(resource.fileno(),),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_id = resident.process_identity(child.pid)
    state.service_started(child_id)
    state.mark_running()
    control = resident.ControlServer(coordinator, state)
    control.start()
    def publish():
        coordinator.write_record({'protocol': 1, 'version': resident.APP_VERSION, 'root': str(install),
            'code_id': fingerprint, 'session': session, 'workspace': str(workspace.directory),
            'manager': manager_id, 'service': child_id, 'ui': None, 'phase': state.phase, 'mode': 'resident'})
    try:
        deadline = time.monotonic() + 30
        publish()
        while state.phase == 'running' and time.monotonic() < deadline:
            publish()
            time.sleep(.02)
        coordinator.write_json('fixture-release-' + session + '.json', {'release': True})
        child.wait(timeout=5)
        state.phase = 'completed'
        publish()
        return 0 if child.returncode == 0 else 1
    finally:
        coordinator.write_json('fixture-release-' + session + '.json', {'release': True})
        child.wait(timeout=5)
        control.close()
        resource.close()
        manager_lease.close()
        workspace.close()
        coordinator.close()


if __name__ == '__main__':
    if len(sys.argv) == 5 and sys.argv[1] == '--fixture-manager':
        raise SystemExit(fixture_manager(*sys.argv[2:]))
    if len(sys.argv) == 5 and sys.argv[1] == '--fixture-lease-holder':
        raise SystemExit(fixture_lease_holder(int(sys.argv[2]), sys.argv[3], sys.argv[4]))
    if '--linux-fixture' in sys.argv:
        if not sys.platform.startswith('linux'):
            raise SystemExit('Linux is required for this isolated kernel fixture')
        if '--fixture-dir' not in sys.argv:
            raise SystemExit('Supply --fixture-dir with an explicit isolated path; board paths belong below /userdata/helmet-validation')
        index = sys.argv.index('--fixture-dir')
        if index + 1 >= len(sys.argv):
            raise SystemExit('--fixture-dir requires a path')
        FIXTURE_PARENT = Path(sys.argv[index + 1])
        importlib.import_module(RESIDENT_MODULE).private_directory(FIXTURE_PARENT)
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(LinuxResidentFixtureTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 1)
    unittest.main()
