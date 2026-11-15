"""Boot start/stop decisions with kernel ownership substituted at its boundary."""
from contextlib import nullcontext, redirect_stderr
import importlib
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from helmet.app import launch, resident


class BackgroundLaunchTests(unittest.TestCase):
    def test_background_entry_cannot_mix_with_show_hide_or_verification_actions(self):
        try:
            args = launch.parse_arguments(['--background'])
        except SystemExit:
            self.fail('normal launcher does not have a background bootstrap entry')
        self.assertTrue(args.background)
        self.assertEqual(args.mode, 'resident')
        self.assertEqual(args.control, 'show')
        for extra in (['--control', 'hide'], ['--smoke-seconds', '10'], ['--verify-resident-ui']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                launch.parse_arguments(['--background', *extra])

    def test_background_manager_waits_for_an_icon_before_creating_a_window(self):
        root = Path('/userdata/helmet-validation/boot-fixture')
        with patch.object(resident, 'validate_install', return_value=root), \
                patch.object(resident, 'process_identity', return_value={'pid': 123, 'starttime': 10, 'uid': 0}):
            try:
                manager = resident.ResidentManager(root, background=True)
            except TypeError:
                self.fail('resident cannot start without an initial show intent')
        self.assertFalse(manager.application.snapshot()['intent']['visible'])
        self.assertFalse(manager.application.needs_ui())
        manager.application.mark_running()
        manager.application.request('show', request_id='icon', tab='wifi')
        self.assertTrue(manager.application.needs_ui())
        self.assertEqual(manager.application.snapshot()['intent']['tab'], 'wifi')


class BootCleanupTests(unittest.TestCase):
    def setUp(self):
        try:
            self.boot = importlib.import_module('helmet.app.boot')
        except ImportError:
            self.fail('confirmed boot shutdown is not implemented')
        self.root = Path('/userdata/helmet-validation/boot-fixture')
        self.session = 'a' * 32
        fingerprint = resident.code_identity(Path(resident.__file__).parent)
        self.record = {'root': str(self.root), 'version': resident.APP_VERSION, 'code_id': fingerprint,
                       'session': self.session, 'phase': 'completed',
                       'workspace': str(self.root / 'app-state' / ('session-' + self.session))}
        self.receipt = {'root': str(self.root), 'version': resident.APP_VERSION, 'code_id': fingerprint,
                        'session': self.session, 'phase': 'completed', 'cleanup_completed': True,
                        'resource_lease_released': True, 'service_exit': 0}
        self.live = False
        owner = self

        class OwnershipBoundary:
            def __init__(self, directory=None):
                self.directory = directory

            def prepare(self):
                return self

            def acquire(self, *args):
                return nullcontext()

            def read_record(self):
                return owner.record

            def read_json(self, *args):
                return owner.receipt

            def assert_no_live_owners(self, record):
                if owner.live:
                    raise RuntimeError('live inherited resource owner')

            def close(self):
                pass

        self.patcher = patch.object(resident, 'Coordinator', OwnershipBoundary)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_accepted_quit_with_live_owner_cannot_report_success(self):
        self.live = True
        with self.assertRaises(TimeoutError):
            self.boot.wait_for_cleanup(self.root, self.session, timeout=.02)

    def test_absent_processes_without_actual_cleanup_receipt_are_not_success(self):
        self.receipt = None
        with self.assertRaises(TimeoutError):
            self.boot.wait_for_cleanup(self.root, self.session, timeout=.02)

    def test_failed_cleanup_or_nonzero_service_exit_cannot_report_completed(self):
        for field, value in (('cleanup_completed', False), ('resource_lease_released', False),
                             ('service_exit', -15), ('phase', 'failed')):
            with self.subTest(field=field):
                original = self.receipt[field]
                self.receipt[field] = value
                with self.assertRaises(RuntimeError):
                    self.boot.wait_for_cleanup(self.root, self.session, timeout=.02)
                self.receipt[field] = original

    def test_complete_same_session_receipt_and_released_owners_confirm_shutdown(self):
        result = self.boot.wait_for_cleanup(self.root, self.session, timeout=.02)
        self.assertTrue(result['stopped'])
        self.assertEqual(result['session'], self.session)
        self.assertEqual(result['phase'], 'completed')

    def test_different_session_cannot_be_substituted_for_the_requested_shutdown(self):
        self.record['session'] = 'b' * 32
        with self.assertRaises(RuntimeError):
            self.boot.wait_for_cleanup(self.root, self.session, timeout=.02)


if __name__ == '__main__':
    unittest.main()
