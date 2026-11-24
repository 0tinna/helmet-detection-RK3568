"""Authenticated LAN-only event archive HTTP delivery."""
from datetime import datetime, timedelta, timezone
import http.client
import importlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit


class AlertAccessKeyTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module('helmet.app.alert_viewer')
        except ImportError as error:
            self.fail('public event viewer is not implemented: ' + str(error))

    def test_private_key_mode_rejects_group_or_world_permissions(self):
        module = self.module()
        self.assertTrue(module._private_key_mode(0o100600))
        self.assertFalse(module._private_key_mode(0o100644))
        self.assertFalse(module._private_key_mode(0o100660))

    def key_path(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / 'private' / 'event-access.key'

    def test_key_file_is_private_and_never_overwritten(self):
        module = self.module()
        path = self.key_path()
        first = module.AlertAccessKey.load_or_create(path)
        first_bytes = path.read_bytes()
        second = module.AlertAccessKey.load_or_create(path)
        self.assertEqual(first_bytes, path.read_bytes())
        self.assertGreaterEqual(len(first_bytes), 32)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        token = first.make_invite()
        self.assertTrue(first.verify_invite(token))
        self.assertTrue(second.verify_invite(token))

    def test_invite_rejects_tampering_expiration_and_another_key(self):
        module = self.module()
        first = module.AlertAccessKey.load_or_create(self.key_path())
        second = module.AlertAccessKey.load_or_create(self.key_path())
        now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        token = first.make_invite(now=now, ttl=60)
        self.assertTrue(first.verify_invite(token, now=now + timedelta(seconds=59)))
        self.assertFalse(first.verify_invite(token + 'x', now=now))
        self.assertFalse(first.verify_invite(token, now=now + timedelta(seconds=61)))
        self.assertFalse(second.verify_invite(token, now=now))

    def test_cookie_value_is_bound_to_hmac_and_url_encodes_invite(self):
        module = self.module()
        key = module.AlertAccessKey.load_or_create(self.key_path())
        now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        cookie = key.cookie_value(now=now)
        self.assertTrue(key.verify_cookie(cookie, now=now))
        self.assertFalse(key.verify_cookie(cookie + 'x', now=now))
        original = key.make_invite
        key.make_invite = lambda now=None: 'invite&value'
        url = key.url_path('192.168.1.10', lan_port=18092,
                           scheme='https', now=now)
        key.make_invite = original
        self.assertEqual(url, 'https://192.168.1.10:18092/alerts?access=invite%26value')
        with self.assertRaises(ValueError):
            key.url_path('203.0.113.10', lan_port=8092, now=now)


class LanEventServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.module = self.viewer_module()
        self.events = importlib.import_module('helmet.app.alert_events')
        self.store = self.events.AlertEventStore(self.root)
        self.addCleanup(self.store.close)
        self.first = self.publish(
            '2026-11-23T10:00:00.000+08:00', b'first-jpeg')
        self.second = self.publish(
            '2026-11-24T10:00:00.000+08:00', b'second-jpeg')
        self.key = self.module.AlertAccessKey.load_or_create(
            self.root / 'app-state' / 'event-access.key')
        self.server = self.module.LanEventServer(
            self.store, self.key,
            lambda: {'detection': 'running', 'mail': {'state': 'idle'},
                     'viewer': {'alive': True, 'scope': 'lan'}},
            host='127.0.0.1', port=0, max_clients=1)
        self.addCleanup(self.server.close)
        self.server.start()

    @staticmethod
    def viewer_module():
        try:
            return importlib.import_module('helmet.app.alert_viewer')
        except ImportError as error:
            raise AssertionError(
                'public event viewer is not implemented: ' + str(error))

    def candidate(self, wall_time):
        return self.events.EventCandidate(
            captured_at=100.0,
            captured_wall_time=wall_time,
            frame_id=7,
            source_epoch=1,
            generation='generation',
            trigger_track_ids=[1],
            visible_no_hat_track_ids=[1],
            detections=[{'track_id': 1, 'box': [10, 10, 50, 50],
                         'score': .9, 'class': 'person'}],
        )

    def publish(self, wall_time, jpeg):
        return self.store.publish(
            self.candidate(wall_time), jpeg,
            {'model': 'helmet_416x416.rknn',
             'selection': {'source': '/dev/video10'}})

    def request(self, route, method='GET', headers=None):
        connection = http.client.HTTPConnection(
            '127.0.0.1', self.server.port, timeout=3)
        try:
            connection.request(method, route, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read(), dict(response.headers)
        finally:
            connection.close()

    def login(self):
        path = self.key.url_path('127.0.0.1', lan_port=self.server.port)
        parsed = urlsplit(path)
        token = parse_qs(parsed.query)['access'][0]
        code, _, headers = self.request('/alerts?access=' + token)
        self.assertEqual(code, 302)
        return headers['Set-Cookie'].split(';', 1)[0]

    def test_event_data_and_media_require_a_valid_access_cookie(self):
        for route in ('/events', '/api/events', '/api/status',
                      '/events/%s.jpg' % self.second['event_id']):
            with self.subTest(route=route):
                self.assertEqual(self.request(route)[0], 403)
        code, body, headers = self.request('/alerts?access=bad')
        self.assertEqual(code, 403)
        self.assertNotIn('Set-Cookie', headers)
        cookie = self.login()
        code, body, _ = self.request('/api/events?limit=1',
                                     headers={'Cookie': cookie})
        self.assertEqual(code, 200)
        value = json.loads(body)
        self.assertEqual(len(value['events']), 1)
        self.assertEqual(value['events'][0]['event_id'], self.second['event_id'])
        self.assertNotIn('generation', value['events'][0])
        code, body, _ = self.request(
            '/events/%s.jpg' % self.second['event_id'],
            headers={'Cookie': cookie})
        self.assertEqual(code, 200)
        self.assertEqual(body, b'second-jpeg')

    def test_access_redirect_sets_strict_httponly_cookie(self):
        path = self.key.url_path('127.0.0.1', lan_port=self.server.port)
        token = parse_qs(urlsplit(path).query)['access'][0]
        code, _, headers = self.request('/alerts?access=' + token)
        self.assertEqual(code, 302)
        self.assertEqual(headers['Location'], '/events')
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertIn('Path=/', cookie)

    def test_root_is_a_data_free_entry_page_without_refresh_loop(self):
        code, body, headers = self.request('/')
        self.assertEqual(code, 200)
        self.assertIn('同一 Wi-Fi/局域网'.encode('utf-8'), body)
        self.assertNotIn('公网 IP'.encode('utf-8'), body)
        self.assertNotIn(b'/api/status', body)
        self.assertNotIn(self.second['event_id'].encode('ascii'), body)
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_pagination_filters_and_public_metadata(self):
        cookie = self.login()
        code, body, _ = self.request(
            '/api/events?limit=1&before=' + self.second['event_id'],
            headers={'Cookie': cookie})
        self.assertEqual(code, 200)
        value = json.loads(body)
        self.assertEqual([row['event_id'] for row in value['events']],
                         [self.first['event_id']])
        code, body, _ = self.request(
            '/api/events?day=2026-11-24', headers={'Cookie': cookie})
        self.assertEqual(code, 200)
        self.assertEqual([row['event_id'] for row in json.loads(body)['events']],
                         [self.second['event_id']])

    def test_no_live_or_control_routes_are_exposed(self):
        cookie = self.login()
        headers = {'Cookie': cookie}
        for route in ('/frame.jpg', '/stream.mjpg', '/status.json',
                      '/api/start', '/api/stop'):
            with self.subTest(route=route):
                self.assertEqual(self.request(route, headers=headers)[0], 404)
        for method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            with self.subTest(method=method):
                self.assertEqual(self.request('/api/events', method, headers)[0], 405)

    def test_security_headers_and_cookie_surface(self):
        cookie = self.login()
        code, body, headers = self.request('/events', headers={'Cookie': cookie})
        self.assertEqual(code, 200)
        self.assertIn(b'<h1', body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-eval', headers['Content-Security-Policy'])

    def test_status_does_not_publish_public_ip_or_remote_address_metrics(self):
        cookie = self.login()
        code, body, _ = self.request('/api/status', headers={'Cookie': cookie})
        self.assertEqual(code, 200)
        value = json.loads(body)
        self.assertNotIn('public_ip', value)
        self.assertEqual(value['viewer']['scope'], 'lan')
        self.assertNotIn('192.168.1.10', body.decode('utf-8'))

    def test_lan_client_guard_rejects_public_or_cgnat_sources(self):
        module = self.module
        self.assertTrue(module._is_lan_client('127.0.0.1'))
        self.assertTrue(module._is_lan_client('192.168.1.10'))
        self.assertFalse(module._is_lan_client('8.8.8.8'))
        self.assertFalse(module._is_lan_client('100.64.0.1'))
        self.assertFalse(module._is_lan_client('not-an-address'))

    def test_health_is_minimal_and_media_capacity_is_bounded(self):
        code, body, _ = self.request('/healthz')
        self.assertEqual(code, 200)
        self.assertEqual(body, b'ok')
        cookie = self.login()
        self.server.media_slots.acquire()
        try:
            code, _, _ = self.request(
                '/events/%s.jpg' % self.second['event_id'],
                headers={'Cookie': cookie})
            self.assertEqual(code, 503)
        finally:
            self.server.media_slots.release()

    def test_close_releases_listener(self):
        port = self.server.port
        self.server.close()
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.5)
        try:
            with self.assertRaises(OSError):
                connection.request('GET', '/')
                connection.getresponse()
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
