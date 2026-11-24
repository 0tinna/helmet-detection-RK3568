"""Direct LAN-only event archive HTTP delivery."""
import http.client
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit


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
        self.server = self.module.LanEventServer(
            self.store,
            lambda: {
                'events_created': 2,
                'last_event_at': self.second['captured_wall_time'],
                'detection': {
                    'state': 'healthy',
                    'running': True,
                    'valid': True,
                    'generation': 'run-a',
                    'alarm_state': 'active',
                    'frame_id': 42,
                    'source_epoch': 3,
                },
                'viewer': {'alive': True, 'scope': 'lan'},
            },
            host='127.0.0.1', port=0, max_clients=1)
        self.addCleanup(self.server.close)
        self.server.start()

    @staticmethod
    def viewer_module():
        try:
            return importlib.import_module('helmet.app.alert_viewer')
        except ImportError as error:
            raise AssertionError(
                'LAN event viewer is not implemented: ' + str(error))

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

    def test_dashboard_api_and_media_are_directly_available(self):
        for route in ('/', '/events', '/api/events',
                      '/api/status',
                      '/events/%s.jpg' % self.second['event_id']):
            with self.subTest(route=route):
                code, _, headers = self.request(route)
                self.assertEqual(code, 200)
                self.assertNotIn('Set-Cookie', headers)
        code, body, _ = self.request('/api/events?limit=1')
        value = json.loads(body)
        self.assertEqual(len(value['events']), 1)
        self.assertEqual(value['events'][0]['event_id'],
                         self.second['event_id'])
        self.assertNotIn('generation', value['events'][0])
        self.assertNotIn('email', value['events'][0])
        code, body, _ = self.request(
            '/events/%s.jpg' % self.second['event_id'])
        self.assertEqual((code, body), (200, b'second-jpeg'))

    def test_old_alerts_bookmark_redirects_without_authentication(self):
        code, _, headers = self.request('/alerts?access=old-token')
        self.assertEqual(code, 302)
        self.assertEqual(headers['Location'], '/')
        self.assertNotIn('Set-Cookie', headers)

    def test_root_is_the_read_only_dashboard_and_has_no_notification_ui(self):
        code, body, headers = self.request('/')
        self.assertEqual(code, 200)
        text = body.decode('utf-8')
        self.assertIn('HELMET SAFETY GRID', text)
        self.assertIn('当前告警', text)
        self.assertIn('可追溯报警记录', text)
        self.assertIn('href="#content"', text)
        self.assertNotIn('access=', text)
        self.assertNotIn('邀请', text)
        self.assertNotIn('邮件', text)
        self.assertNotIn('公网 IP', text)
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_pagination_filters_and_public_metadata(self):
        code, body, _ = self.request(
            '/api/events?limit=1&before=' + self.second['event_id'])
        self.assertEqual(code, 200)
        value = json.loads(body)
        self.assertEqual([row['event_id'] for row in value['events']],
                         [self.first['event_id']])
        code, body, _ = self.request('/api/events?day=2026-11-24')
        self.assertEqual(code, 200)
        self.assertEqual([row['event_id'] for row in json.loads(body)['events']],
                         [self.second['event_id']])
        for route in ('/api/events?limit=no', '/api/events?day=nope',
                      '/api/events?limit=1&limit=2'):
            with self.subTest(route=route):
                self.assertEqual(self.request(route)[0], 400)

    def test_no_live_or_control_routes_are_exposed(self):
        for route in ('/frame.jpg', '/stream.mjpg', '/status.json',
                      '/api/start', '/api/stop', '/api/wifi'):
            with self.subTest(route=route):
                self.assertEqual(self.request(route)[0], 404)
        for method in ('POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'):
            with self.subTest(method=method):
                self.assertEqual(self.request('/api/events', method)[0], 405)

    def test_security_headers_are_present_without_a_cookie_surface(self):
        code, body, headers = self.request('/')
        self.assertEqual(code, 200)
        self.assertIn(b'<main', body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-eval', headers['Content-Security-Policy'])
        self.assertNotIn('Set-Cookie', headers)

    def test_status_exposes_current_alarm_state_without_private_metrics(self):
        code, body, _ = self.request('/api/status')
        self.assertEqual(code, 200)
        value = json.loads(body)
        self.assertEqual(value['detection']['alarm_state'], 'active')
        self.assertEqual(value['detection']['frame_id'], 42)
        self.assertEqual(value['last_event_at'],
                         self.second['captured_wall_time'])
        self.assertEqual(value['viewer']['scope'], 'lan')
        self.assertNotIn('mail', value)
        self.assertNotIn('public_ip', value)
        self.assertNotIn('192.168.1.10', body.decode('utf-8'))

    def test_lan_client_guard_rejects_public_or_cgnat_sources(self):
        module = self.module
        self.assertTrue(module._is_lan_client('127.0.0.1'))
        self.assertTrue(module._is_lan_client('192.168.1.10'))
        self.assertTrue(module._is_lan_client('10.0.0.8'))
        self.assertFalse(module._is_lan_client('8.8.8.8'))
        self.assertFalse(module._is_lan_client('100.64.0.1'))
        self.assertFalse(module._is_lan_client('2001:db8::1'))
        self.assertFalse(module._is_lan_client('not-an-address'))

    def test_health_is_minimal_and_media_capacity_is_bounded(self):
        code, body, _ = self.request('/healthz')
        self.assertEqual((code, body), (200, b'ok'))
        self.server.media_slots.acquire()
        try:
            code, _, _ = self.request(
                '/events/%s.jpg' % self.second['event_id'])
            self.assertEqual(code, 503)
        finally:
            self.server.media_slots.release()

    def test_direct_lan_url_has_no_query_or_token(self):
        self.assertEqual(
            self.server.url_path('192.168.1.10'),
            'http://192.168.1.10:8092/')
        with self.assertRaises(ValueError):
            self.server.url_path('8.8.8.8')

    def test_direct_viewer_creates_no_access_key(self):
        self.assertFalse((self.root / 'app-state').exists())
        parsed = urlsplit(self.server.url_path('127.0.0.1'))
        self.assertEqual(parse_qs(parsed.query), {})
        self.assertEqual(parsed.path, '/')

    def test_close_releases_listener(self):
        port = self.server.port
        self.server.close()
        connection = http.client.HTTPConnection(
            '127.0.0.1', port, timeout=.5)
        try:
            with self.assertRaises(OSError):
                connection.request('GET', '/')
                connection.getresponse()
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
