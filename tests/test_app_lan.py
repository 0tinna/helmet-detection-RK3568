"""Actual LAN HTTP against the App controller; only camera/NPU boundaries are fake."""
import base64
import http.client
import importlib
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest

import cv2
import numpy as np

try:
    from .test_app_service import ControlledPipeline, ControlledWifi
except ImportError:
    from test_app_service import ControlledPipeline, ControlledWifi


class AppLanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'models').mkdir()
        (root / 'input').mkdir()
        (root / 'models/helmet_416x416.rknn').write_bytes(b'fixture')
        cv2.imwrite(str(root / 'input/demo.jpg'), np.zeros((24, 32, 3), np.uint8))
        module = importlib.import_module('helmet.app.service')
        self.controller = module.DetectionController(root, pipeline_factory=ControlledPipeline)
        self.addCleanup(self.controller.close, 1)
        self.wifi = ControlledWifi()
        try:
            self.service = module.LocalService(self.controller, self.wifi, token='private-token',
                                              port=0, lan_port=0, lan_host='127.0.0.1')
        except TypeError:
            self.fail('App has no independently bound read-only LAN listener')
        self.addCleanup(self.service.close, 1)
        self.service.start()

    def request(self, route, body=None, private=False):
        port = self.service.port if private else self.service.lan.port
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
        headers = {'X-Helmet-Token': 'private-token'} if private else {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            connection.request('GET' if body is None else 'POST', route,
                               body=None if body is None else json.dumps(body), headers=headers)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.headers)
        finally:
            connection.close()

    def start_detection(self):
        code, _, _ = self.request('/api/start', {'model': 'helmet_416x416.rknn',
                                                'source': 'demo.jpg', 'profile': 'responsive'}, private=True)
        self.assertEqual(code, 200)
        self.controller.pipeline.emit(frame_id=7)

    def test_page_is_available_while_idle_without_exposing_private_controls(self):
        code, body, headers = self.request('/')
        self.assertEqual(code, 200)
        self.assertIn('安全帽检测'.encode('utf-8'), body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn(b'private-token', body)
        code, body, _ = self.request('/status.json')
        self.assertEqual(code, 200)
        status = json.loads(body)
        self.assertFalse(status['valid'])
        self.assertFalse(status['running'])
        self.assertNotIn('wifi_action', status)
        self.assertNotIn('token', status)
        for route in ('/api/start', '/api/stop', '/api/wifi/connect', '/api/application/quit'):
            self.assertEqual(self.request(route, {})[0], 405)
            self.assertEqual(self.request(route)[0], 404)
        self.assertIsNone(self.controller.pipeline)
        connection = http.client.HTTPConnection('127.0.0.1', self.service.port, timeout=2)
        try:
            connection.request('GET', '/api/status')
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            connection.close()

    def test_lan_and_native_preview_share_the_same_encoded_original_frame(self):
        self.start_detection()
        native = json.loads(self.request('/api/preview', private=True)[1])
        code, body, _ = self.request('/api/preview')
        self.assertEqual(code, 200)
        lan = json.loads(body)
        self.assertEqual(lan['frame_id'], 7)
        for field in ('generation', 'source_epoch', 'captured_at', 'jpeg_base64'):
            self.assertEqual(lan[field], native[field])
        jpeg = base64.b64decode(lan['jpeg_base64'], validate=True)
        decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(decoded.shape, (24, 32, 3))
        code, data, headers = self.request('/frame.jpg')
        self.assertEqual(code, 200)
        self.assertEqual(data, jpeg)
        self.assertEqual(headers['X-Helmet-Frame-Id'], '7')
        self.assertGreater(float(headers['X-Helmet-Remaining-Ttl-Ms']), 0)
        self.assertIs(self.controller.pipeline, self.service.controller.pipeline)

    def test_lan_status_exposes_an_exact_token_for_binary_frame_loading(self):
        self.start_detection()
        code, body, _ = self.request('/status.json')
        self.assertEqual(code, 200)
        status = json.loads(body)
        self.assertNotIn('jpeg_base64', status)
        token = status['frame_token']
        code, image, headers = self.request('/frame.jpg?frame_token=' + token)
        self.assertEqual(code, 200)
        self.assertEqual(headers['X-Helmet-Frame-Id'], '7')
        self.assertEqual(image, self.controller.preview_package(frame_token=token)['jpeg'])

    def test_lan_frame_token_never_falls_back_to_a_newer_frame(self):
        self.start_detection()
        old_token = json.loads(self.request('/status.json')[1])['frame_token']
        for frame_id in range(8, 12):
            self.controller.pipeline.emit(frame_id=frame_id)
        self.assertEqual(self.request('/status.json')[0], 200)
        self.assertEqual(self.request('/frame.jpg?frame_token=' + old_token)[0], 503)
        self.assertEqual(self.request('/frame.jpg?frame_token=invalid-token')[0], 400)

    def test_fault_and_missed_epoch_transition_immediately_suppress_cached_jpeg(self):
        self.start_detection()
        pipeline = self.controller.pipeline
        pipeline.valid = False
        self.assertEqual(self.request('/frame.jpg')[0], 503)
        pipeline.valid = True
        pipeline.source_epoch += 1
        self.assertEqual(self.request('/frame.jpg')[0], 503)
        self.assertFalse(json.loads(self.request('/status.json')[1])['valid'])
        pipeline.emit(epoch=1, frame_id=8)
        self.assertEqual(json.loads(self.request('/api/preview')[1])['frame_id'], 8)

    def test_frame_expiration_is_checked_even_when_pipeline_status_stays_healthy(self):
        self.start_detection()
        self.controller.pipeline.emit(age=.97, frame_id=8)
        time.sleep(.05)
        self.assertEqual(self.request('/frame.jpg')[0], 503)
        code, body, _ = self.request('/api/preview')
        self.assertEqual(code, 503)
        self.assertFalse(json.loads(body)['valid'])
        self.assertNotIn('jpeg_base64', json.loads(body))

    def test_stop_and_restart_with_same_frame_number_cannot_reuse_old_generation(self):
        self.start_detection()
        old = json.loads(self.request('/api/preview')[1])
        self.assertEqual(self.request('/api/stop', {}, private=True)[0], 200)
        self.assertEqual(self.request('/frame.jpg')[0], 503)
        self.start_detection()
        new = json.loads(self.request('/api/preview')[1])
        self.assertNotEqual(old['generation'], new['generation'])
        self.assertEqual(new['frame_id'], 7)

    def test_close_releases_both_listeners_and_does_not_claim_a_live_device_is_closed(self):
        self.start_detection()
        pipeline = self.controller.pipeline
        pipeline.allow_stop.clear()
        result = self.service.close(timeout=.03)
        self.assertFalse(result['detection_stopped'])
        self.assertFalse(self.service.closed)
        self.assertEqual(self.request('/frame.jpg')[0], 503)
        pipeline.allow_stop.set()
        self.assertTrue(self.service.close(timeout=1)['detection_stopped'])
        self.assertTrue(pipeline.cleaned)
        for port in (self.service.port, self.service.lan.port):
            with socket.socket() as connection:
                connection.settimeout(.2)
                self.assertNotEqual(connection.connect_ex(('127.0.0.1', port)), 0)

    def test_port_conflict_does_not_silently_choose_a_different_lan_port(self):
        module = importlib.import_module('helmet.app.service')
        occupied = socket.socket()
        self.addCleanup(occupied.close)
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        service = module.LocalService(self.controller, self.wifi, port=0,
                                      lan_host='127.0.0.1', lan_port=occupied.getsockname()[1])
        self.addCleanup(service.close, 1)
        with self.assertRaises(OSError):
            service.start()
        self.assertIsNone(self.controller.pipeline)

    def test_mjpeg_is_original_jpeg_with_frame_identity_and_releases_client_capacity(self):
        self.start_detection()
        connection = http.client.HTTPConnection('127.0.0.1', self.service.lan.port, timeout=2)
        try:
            connection.request('GET', '/stream.mjpg')
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.fp.readline(), b'--frame\r\n')
            headers = {}
            while True:
                line = response.fp.readline()
                if line == b'\r\n':
                    break
                key, value = line.decode('ascii').strip().split(': ', 1)
                headers[key] = value
            self.assertEqual(headers['X-Helmet-Frame-Id'], '7')
            jpeg = response.fp.read(int(headers['Content-Length']))
            self.assertEqual(jpeg, self.controller.preview())
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
