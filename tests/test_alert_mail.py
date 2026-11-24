"""SMTP alert batching, retry and LAN-address behavior."""
from contextlib import redirect_stdout
import importlib
import io
import json
from pathlib import Path
import smtplib
import tempfile
import time
import unittest
from unittest.mock import patch


class MailSettingsTests(unittest.TestCase):
    def module(self):
        try:
            from helmet.app import alert_mail
        except ImportError as error:
            self.fail('SMTP alert module is not implemented: ' + str(error))
        return alert_mail

    def write(self, value):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'mail.json'
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(json.dumps(value), encoding='utf-8')
        path.chmod(0o600)
        return path

    def settings(self, **updates):
        value = {
            'enabled': True,
            'smtp_host': 'smtp.example.edu.cn',
            'smtp_port': 465,
            'security': 'ssl',
            'username': 'sender@example.edu.cn',
            'password': 'private-auth-code',
            'sender': 'sender@example.edu.cn',
            'recipient': '230010044@mail.szpu.edu.cn',
            'timeout_seconds': 8,
            'batch_seconds': 5,
        }
        value.update(updates)
        return value

    def test_loads_supported_security_modes(self):
        module = self.module()
        for security in ('ssl', 'starttls', 'none'):
            path = self.write(self.settings(security=security))
            loaded = module.MailSettings.load(path)
            self.assertEqual(loaded.security, security)
            self.assertEqual(loaded.recipient, '230010044@mail.szpu.edu.cn')
            self.assertNotIn('private-auth-code', repr(loaded))

    def test_rejects_invalid_or_unsafe_configuration(self):
        module = self.module()
        invalid = [
            self.settings(security='plain'),
            self.settings(recipient='not-an-address'),
            self.settings(smtp_host=''),
            self.settings(password=''),
            self.settings(smtp_port=0),
            self.settings(timeout_seconds=0),
            self.settings(batch_seconds=0),
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises((ValueError, OSError)):
                    module.MailSettings.load(self.write(value))
        with self.assertRaises((ValueError, OSError)):
            module.MailSettings.load(self.write(b'x' * (64 * 1024 + 1)))

    def test_private_mode_rejects_group_or_world_permissions(self):
        module = self.module()
        self.assertTrue(module._private_file_mode(0o100600))
        self.assertFalse(module._private_file_mode(0o100644))
        self.assertFalse(module._private_file_mode(0o100660))


class LanAddressResolverTests(unittest.TestCase):
    def module(self):
        try:
            from helmet.app import alert_mail
        except ImportError as error:
            self.fail('LAN address resolver is not implemented: ' + str(error))
        return alert_mail

    def test_accepts_one_private_ipv4_interface_address(self):
        module = self.module()
        resolver = module.LanAddressResolver(
            address_getter=lambda: '192.168.1.10')
        result = resolver.resolve()
        self.assertEqual(result['address'], '192.168.1.10')
        self.assertEqual(result['source'], 'local_interface')

    def test_rejects_loopback_public_ipv6_and_malformed_values(self):
        module = self.module()
        for value in ('127.0.0.1', '8.8.8.8', '2001:db8::1', 'not-an-ip'):
            with self.subTest(value=value):
                resolver = module.LanAddressResolver(
                    address_getter=lambda value=value: value)
                result = resolver.resolve()
                self.assertIsNone(result['address'])
                self.assertEqual(result['source'], 'unavailable')

    def test_interface_lookup_failure_is_truthfully_unavailable(self):
        module = self.module()
        resolver = module.LanAddressResolver(
            address_getter=lambda: (_ for _ in ()).throw(OSError('private')))
        result = resolver.resolve()
        self.assertIsNone(result['address'])
        self.assertEqual(result['source'], 'unavailable')
        self.assertNotIn('private', json.dumps(result))


class FakeSMTP:
    instances = []
    failures_remaining = 0
    failure_exception = None
    quit_exception = None

    def __init__(self, settings):
        self.settings = settings
        self.messages = []
        self.started_tls = False
        self.logged_in = None
        self.quit_called = False
        self.__class__.instances.append(self)

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, username, password):
        self.logged_in = (username, password)

    def send_message(self, message):
        if self.__class__.failure_exception is not None:
            raise self.__class__.failure_exception
        if self.__class__.failures_remaining:
            self.__class__.failures_remaining -= 1
            raise OSError('smtp response contains private details')
        self.messages.append(message)

    def quit(self):
        self.quit_called = True
        if self.__class__.quit_exception is not None:
            raise self.__class__.quit_exception


class SmtpAlertMailerTests(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances.clear()
        FakeSMTP.failures_remaining = 0
        FakeSMTP.failure_exception = None
        FakeSMTP.quit_exception = None
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def module(self):
        try:
            from helmet.app import alert_mail
        except ImportError as error:
            self.fail('SMTP alert mailer is not implemented: ' + str(error))
        return alert_mail

    def settings(self, **updates):
        module = self.module()
        values = {
            'enabled': True,
            'smtp_host': 'smtp.example.edu.cn',
            'smtp_port': 587,
            'security': 'starttls',
            'username': 'sender@example.edu.cn',
            'password': 'private-auth-code',
            'sender': 'sender@example.edu.cn',
            'recipient': '230010044@mail.szpu.edu.cn',
            'timeout_seconds': 2,
            'batch_seconds': .02,
        }
        values.update(updates)
        return module.MailSettings(**values)

    def event(self, number=1, *, generation='run-a', visible_ids=None, count=1):
        visible_ids = list(visible_ids or [number])
        image = self.root / ('event-%d.jpg' % number)
        image.write_bytes(b'\xff\xd8event-%d\xff\xd9' % number)
        return {
            'event_id': '20261124T14253%d000000Z-ab12cd34' % number,
            'captured_wall_time': '2026-11-24T14:25:3%d.000+08:00' % number,
            'generation': generation,
            'source_epoch': 0,
            'trigger_track_ids': [visible_ids[-1]],
            'visible_no_hat_track_ids': visible_ids,
            'count': count,
            'media_path': str(image),
        }

    def resolver(self, address='192.168.1.10'):
        module = self.module()
        return module.LanAddressResolver(
            address_getter=lambda: address)

    def mailer(self, **updates):
        module = self.module()
        resolver = updates.pop('resolver', self.resolver())
        kwargs = {
            'batch_seconds': .02,
            'retry_delays': (0, 0, 0),
        }
        kwargs.update(updates)
        mailer = module.SmtpAlertMailer(
            self.settings(),
            lambda address: 'http://%s:8092/alerts?access=test-token' % address,
            resolver,
            smtp_factory=FakeSMTP,
            **kwargs,
        )
        self.addCleanup(mailer.close, 1)
        return mailer

    def wait_idle(self, mailer, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = mailer.status()
            if status['pending_batches'] == 0 and status['state'] in (
                    'idle', 'sent', 'failed', 'stopped'):
                return status
            time.sleep(.01)
        self.fail('mailer did not become idle: ' + repr(mailer.status()))

    def test_two_events_in_one_window_send_one_short_message_and_attachment(self):
        mailer = self.mailer()
        mailer.start()
        self.assertTrue(mailer.enqueue([self.event(1)]))
        self.assertTrue(mailer.enqueue([self.event(2)]))
        status = self.wait_idle(mailer)
        self.assertEqual(status['last_result'], 'sent')
        self.assertEqual(len(FakeSMTP.instances), 1)
        smtp = FakeSMTP.instances[0]
        self.assertTrue(smtp.started_tls)
        self.assertEqual(len(smtp.messages), 1)
        message = smtp.messages[0]
        self.assertEqual(message['To'], '230010044@mail.szpu.edu.cn')
        self.assertIn('[RK3568安全帽告警]', message['Subject'])
        body = message.get_body(preferencelist=('plain',)).get_content()
        self.assertIn('内网查看', body)
        self.assertIn('192.168.1.10', body)
        self.assertIn(
            'http://192.168.1.10:8092/alerts?access=test-token', body)
        self.assertNotIn('公网 IP', body)
        self.assertIn('2', message['Subject'])
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(),
                         self.event(1)['event_id'] + '.jpg')
        self.assertTrue(mailer.close())

    def test_missing_lan_address_is_reported_without_a_fake_link(self):
        module = self.module()
        resolver = module.LanAddressResolver(
            address_getter=lambda: (_ for _ in ()).throw(OSError('offline')))
        mailer = self.mailer(resolver=resolver)
        mailer.start()
        mailer.enqueue([self.event()])
        status = self.wait_idle(mailer)
        self.assertEqual(status['last_result'], 'sent')
        message = FakeSMTP.instances[0].messages[0]
        body = message.get_body(preferencelist=('plain',)).get_content()
        self.assertIn('内网地址暂不可用', body)
        self.assertIn('同一 Wi-Fi/局域网', body)
        self.assertNotIn('http://203.0.113.10', body)
        self.assertNotIn('公网 IP', body)
        self.assertTrue(mailer.close())

    def test_smtp_probe_sends_one_short_message_without_an_event(self):
        mailer = self.mailer()
        result = mailer.probe()
        self.assertTrue(result['sent'])
        self.assertTrue(result['lan_address_available'])
        self.assertEqual(len(FakeSMTP.instances), 1)
        smtp = FakeSMTP.instances[0]
        self.assertEqual(len(smtp.messages), 1)
        message = smtp.messages[0]
        self.assertEqual(message['To'], '230010044@mail.szpu.edu.cn')
        self.assertIn('SMTP自检', message['Subject'])
        self.assertEqual(list(message.iter_attachments()), [])
        body = message.get_body(preferencelist=('plain',)).get_content()
        self.assertIn('192.168.1.10', body)
        self.assertIn('同一 Wi-Fi/局域网', body)
        self.assertNotIn('private-auth-code', body)

    def test_failure_retries_three_times_then_reports_without_secret_text(self):
        FakeSMTP.failures_remaining = 10
        mailer = self.mailer()
        mailer.start()
        mailer.enqueue([self.event()])
        status = self.wait_idle(mailer)
        self.assertEqual(status['last_result'], 'failed')
        self.assertEqual(len(FakeSMTP.instances), 4)
        dumped = json.dumps(status)
        self.assertNotIn('private-auth-code', dumped)
        self.assertNotIn('smtp response contains private details', dumped)
        self.assertTrue(mailer.close())

    def test_smtp_authentication_exception_is_retried_and_reported(self):
        FakeSMTP.failure_exception = smtplib.SMTPAuthenticationError(
            535, b'private authentication response')
        mailer = self.mailer()
        mailer.start()
        mailer.enqueue([self.event()])
        status = self.wait_idle(mailer)
        self.assertEqual(status['last_result'], 'failed')
        self.assertEqual(len(FakeSMTP.instances), 4)
        self.assertTrue(status['worker_alive'])
        self.assertNotIn('private authentication response', json.dumps(status))
        FakeSMTP.failure_exception = None
        mailer.enqueue([self.event(2)])
        recovered = self.wait_idle(mailer)
        self.assertEqual(recovered['last_result'], 'sent')
        self.assertEqual(len(FakeSMTP.instances), 5)
        self.assertTrue(mailer.close())
        self.assertFalse(mailer.status()['worker_alive'])

    def test_post_send_quit_failure_does_not_duplicate_the_message(self):
        FakeSMTP.quit_exception = smtplib.SMTPException(
            'private quit response')
        mailer = self.mailer()
        mailer.start()
        mailer.enqueue([self.event()])
        status = self.wait_idle(mailer)
        self.assertEqual(status['last_result'], 'sent')
        self.assertEqual(len(FakeSMTP.instances), 1)
        self.assertEqual(len(FakeSMTP.instances[0].messages), 1)
        self.assertTrue(mailer.close())

    def test_aggregate_subject_counts_unique_tracks_not_repeated_visible_rows(self):
        mailer = self.mailer()
        mailer.start()
        mailer.enqueue([self.event(
            1, generation='run-a', visible_ids=[1], count=1)])
        mailer.enqueue([self.event(
            2, generation='run-a', visible_ids=[1, 2], count=2)])
        status = self.wait_idle(mailer)
        self.assertEqual(status['last_result'], 'sent')
        message = FakeSMTP.instances[0].messages[0]
        self.assertIn('2 名未戴安全帽人员', message['Subject'])
        body = message.get_body(preferencelist=('plain',)).get_content()
        self.assertIn('本批人数：2', body)
        self.assertTrue(mailer.close())

    def test_queue_is_bounded_before_worker_starts(self):
        mailer = self.mailer(batch_seconds=.0001)
        for number in range(10):
            mailer.enqueue([self.event(number)])
        status = mailer.status()
        self.assertEqual(status['pending_batches'], 8)
        self.assertEqual(status['dropped_batches'], 2)
        self.assertTrue(mailer.close())

    def test_close_flushes_an_open_batch_and_joins_worker(self):
        mailer = self.mailer(batch_seconds=60)
        mailer.start()
        mailer.enqueue([self.event()])
        self.assertTrue(mailer.close(timeout=2))
        self.assertEqual(len(FakeSMTP.instances), 1)
        self.assertEqual(len(FakeSMTP.instances[0].messages), 1)
        self.assertFalse(mailer.status()['worker_alive'])

    def test_disabled_mailer_never_opens_smtp(self):
        module = self.module()
        mailer = module.SmtpAlertMailer(
            self.settings(enabled=False),
            lambda address: None,
            self.resolver(),
            smtp_factory=FakeSMTP,
        )
        mailer.start()
        self.assertFalse(mailer.enqueue([self.event()]))
        self.assertEqual(FakeSMTP.instances, [])
        self.assertTrue(mailer.close())


class MailProbeCliTests(unittest.TestCase):
    class ProbeMailer:
        def __init__(self, error=None):
            self.error = error
            self.closed = False

        def probe(self):
            if self.error is not None:
                raise self.error
            return {
                'sent': True,
                'recipient': '230010044@mail.szpu.edu.cn',
                'lan_address_available': True,
            }

        def close(self, timeout=3):
            self.closed = True
            return True

    def module(self):
        return importlib.import_module('helmet.app.mail_probe')

    def run_main(self, mailer):
        module = self.module()
        output = io.StringIO()
        with patch.object(module, 'build_mailer', return_value=mailer):
            with redirect_stdout(output):
                code = module.main(['--root', 'unused'])
        return code, output.getvalue()

    def test_success_is_machine_readable_and_closes_the_mailer(self):
        mailer = self.ProbeMailer()
        code, output = self.run_main(mailer)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {
            'ok': True,
            'sent': True,
            'recipient': '230010044@mail.szpu.edu.cn',
            'lan_address_available': True,
        })
        self.assertTrue(mailer.closed)

    def test_failure_is_sanitized_and_still_closes_the_mailer(self):
        mailer = self.ProbeMailer(OSError('private SMTP response body'))
        code, output = self.run_main(mailer)
        self.assertEqual(code, 1)
        value = json.loads(output)
        self.assertFalse(value['ok'])
        self.assertEqual(value['error'], 'mail_probe_failed')
        self.assertNotIn('private SMTP response body', output)
        self.assertTrue(mailer.closed)


if __name__ == '__main__':
    unittest.main()
