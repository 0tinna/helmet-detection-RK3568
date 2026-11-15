"""Local application controls with actual HTTP and controlled device boundaries."""
import importlib
import base64
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import cv2
import numpy as np


class ControlledPipeline:
    """A blocked device boundary; lifecycle and callbacks mirror real Pipeline."""
    instances = []

    def __init__(self, args, output_callback=None, event_callback=None):
        self.args = args
        self.output_callback, self.event_callback = output_callback, event_callback
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.allow_stop = threading.Event()
        self.allow_stop.set()
        self.capture_thread = self.detect_thread = None
        self.source_epoch = 0
        self.state = 'startup'
        self.valid = False
        self.cleaned = False
        self.frame_id = None
        self.instances.append(self)

    def run(self):
        self.stop_event.wait()
        self.allow_stop.wait()
        self.cleaned = True
        self.state, self.valid = 'stop', False

    def status_snapshot(self):
        return {'state': self.state, 'reason': self.state, 'valid': self.valid,
                'frame_id': self.frame_id, 'fps': {'detected': 11.0},
                'alarm_state': 'unknown' if not self.valid else 'active'}

    def emit(self, age=0, epoch=0, frame_id=1):
        self.state, self.valid, self.frame_id = 'healthy', True, frame_id
        captured = time.monotonic() - age
        packet = SimpleNamespace(frame_id=frame_id, captured_at=captured,
                                 original_bgr=np.zeros((24, 32, 3), np.uint8),
                                 annotated_bgr=np.full((24, 32, 3), 70, np.uint8),
                                 metadata={'valid': True, 'source_epoch': epoch,
                                           'expires_at': captured + self.args.result_ttl})
        self.output_callback(packet)


class ControlledWifi:
    def __init__(self):
        self.allow_finish = threading.Event()
        self.allow_finish.set()
        self.entered = threading.Event()
        self.fail = False

    def status(self):
        return {'state': 'COMPLETED', 'ssid': 'fixture', 'ip': '192.0.2.4', 'error': None}

    def scan(self):
        self.entered.set()
        self.allow_finish.wait()
        return {'networks': [{'ssid': 'fixture', 'security': 'WPA2-PSK',
                              'signal': -50, 'connectable': True}], 'error': None}

    def connect(self, ssid, password, security, remember=False):
        self.entered.set()
        self.allow_finish.wait()
        if self.fail:
            raise RuntimeError('dependency echoed password=' + password)
        return {'connected': True, 'state': 'COMPLETED', 'ssid': ssid,
                'ip': '192.0.2.4', 'saved': remember, 'error': None,
                'password': password, 'nested': {'psk': password}}


class AppServiceTests(unittest.TestCase):
    def module(self):
        try:
            return importlib.import_module('helmet.app.service')
        except ImportError:
            self.fail('local board service is not implemented')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'models').mkdir()
        (self.root / 'input').mkdir()
        (self.root / 'models' / 'helmet_416x416.rknn').write_bytes(b'fixture')
        cv2.imwrite(str(self.root / 'input' / 'demo.jpg'), np.zeros((24, 32, 3), np.uint8))
        self.service = None
        self.controllers = []
        self.wifi = ControlledWifi()

    def tearDown(self):
        self.wifi.allow_finish.set()
        for controller in self.controllers:
            if controller.pipeline and hasattr(controller.pipeline, 'allow_stop'):
                controller.pipeline.allow_stop.set()
        if self.service:
            self.service.close(timeout=1)
        for controller in self.controllers:
            controller.close(timeout=1)
        self.temp.cleanup()

    def controller(self):
        controller = self.module().DetectionController(self.root, pipeline_factory=ControlledPipeline)
        self.controllers.append(controller)
        return controller

    def server(self):
        controller = self.controller()
        self.service = self.module().LocalService(controller, self.wifi, token='test-secret', port=0)
        self.service.start()
        return controller

    def request(self, route, payload=None, headers=None, token=True):
        request_headers = {'X-Helmet-Token': 'test-secret'} if token else {}
        request_headers.update(headers or {})
        data = None if payload is None else json.dumps(payload).encode()
        if data is not None:
            request_headers['Content-Type'] = 'application/json'
        req = Request('http://127.0.0.1:%d%s' % (self.service.port, route),
                      data=data, headers=request_headers)
        try:
            response = urlopen(req, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            return response.status, body, dict(response.headers)

    def wait_action(self):
        for _ in range(100):
            code, body, _ = self.request('/api/action')
            result = json.loads(body)
            if not result['busy']:
                return result
            time.sleep(.01)
        self.fail('wifi action did not finish')

    def test_control_requires_token_and_rejects_cross_origin(self):
        self.server()
        for payload, headers, token in [({}, {}, False), ({}, {'X-Helmet-Token': 'wrong'}, True),
                                        ({}, {'Origin': 'https://outside.example'}, True)]:
            code, _, _ = self.request('/api/stop', payload, headers, token)
            self.assertEqual(code, 403)
        code, _, _ = self.request('/api/status', headers={'Origin': 'http://127.0.0.1:%d' % self.service.port})
        self.assertEqual(code, 200)
        code, _, _ = self.request('/api/status?token=test-secret', token=False)
        self.assertEqual(code, 403, 'query tokens apply only to frame images')

    def test_invalid_and_oversized_body_do_not_start_detection(self):
        controller = self.server()
        for payload in [{'model': '../outside.rknn', 'source': 'demo.jpg'},
                        {'model': 'helmet_416x416.rknn', 'source': '/etc/passwd'},
                        {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg', 'command': 'id'},
                        {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg', 'profile': 'unsafe'}]:
            code, _, _ = self.request('/api/start', payload)
            self.assertEqual(code, 400)
        code, _, _ = self.request('/api/start', {'padding': 'x' * 9000})
        self.assertEqual(code, 413)
        self.assertIsNone(controller.pipeline)

    def test_model_and_source_symlink_escape_is_rejected(self):
        controller = self.controller()
        try:
            (self.root / 'input' / 'escape.jpg').symlink_to(self.root.parent / 'escape.jpg')
        except OSError:
            self.skipTest('symlink privilege unavailable')
        with self.assertRaises(ValueError):
            controller.start('helmet_416x416.rknn', 'escape.jpg', 'replay')

    def test_camera_catalog_excludes_internal_and_metadata_nodes(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as dev_dir, tempfile.TemporaryDirectory() as sysfs_dir:
            dev_root, sysfs_root = Path(dev_dir), Path(sysfs_dir)
            fixtures = {
                'video0': ('rkisp_mainpath', '0'),
                'video10': ('USB Camera', '0'),
                'video11': ('USB Camera', '1'),
            }
            for name, (device_name, index) in fixtures.items():
                (dev_root / name).write_bytes(b'device')
                class_dir = sysfs_root / name
                class_dir.mkdir()
                (class_dir / 'name').write_text(device_name, encoding='utf-8')
                (class_dir / 'index').write_text(index, encoding='utf-8')

            sources = module._camera_sources(dev_root, sysfs_root, lambda path: True)

        self.assertEqual([Path(row['id']).name for row in sources], ['video10'])
        self.assertEqual(sources[0]['name'], '实时摄像头 USB Camera')
        self.assertEqual(sources[0]['kind'], 'camera')

    def test_camera_catalog_keeps_same_name_capture_devices_in_numeric_order(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as dev_dir, tempfile.TemporaryDirectory() as sysfs_dir:
            dev_root, sysfs_root = Path(dev_dir), Path(sysfs_dir)
            for name, index in {'video10': '0', 'video2': '0', 'video11': '1'}.items():
                (dev_root / name).write_bytes(b'device')
                class_dir = sysfs_root / name
                class_dir.mkdir()
                (class_dir / 'name').write_text('USB Camera', encoding='utf-8')
                (class_dir / 'index').write_text(index, encoding='utf-8')

            sources = module._camera_sources(dev_root, sysfs_root, lambda path: True)

        self.assertEqual([Path(row['id']).name for row in sources], ['video2', 'video10'])

    def test_camera_catalog_ignores_non_numeric_video_names(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as dev_dir, tempfile.TemporaryDirectory() as sysfs_dir:
            dev_root, sysfs_root = Path(dev_dir), Path(sysfs_dir)
            for name in ('video2-extra', 'video10'):
                (dev_root / name).write_bytes(b'device')
                class_dir = sysfs_root / name
                class_dir.mkdir()
                (class_dir / 'name').write_text('USB Camera', encoding='utf-8')
                (class_dir / 'index').write_text('0', encoding='utf-8')

            sources = module._camera_sources(dev_root, sysfs_root, lambda path: True)

        self.assertEqual([Path(row['id']).name for row in sources], ['video10'])

    def test_an_installed_directory_cannot_redirect_outside_app_root(self):
        controller = self.controller()
        with tempfile.TemporaryDirectory() as outside:
            model_path = self.root / 'models'
            alternate = Path(outside)
            (alternate / 'helmet_416x416.rknn').write_bytes(b'outside')
            original = Path.resolve
            # Resolve boundary controls this test even where Windows symlink
            # privilege is unavailable. Actual listing/file operations stay real.
            def redirect(p, *args, **kwargs):
                if p == model_path:
                    return alternate
                if p.parent == model_path:
                    return alternate / p.name
                return original(p, *args, **kwargs)
            with patch.object(Path, 'resolve', redirect):
                with self.assertRaises(ValueError):
                    controller.start('helmet_416x416.rknn', 'demo.jpg', 'replay')

    def test_snapshot_output_directory_cannot_redirect_outside_app_root(self):
        original = Path.resolve
        for escaped in (self.root / 'results', self.root / 'results' / 'app-snapshots'):
            with self.subTest(path=escaped), tempfile.TemporaryDirectory() as outside:
                controller = self.controller()
                alternate = Path(outside)
                def redirect(p, *args, **kwargs):
                    try:
                        relative = p.relative_to(escaped)
                    except ValueError:
                        return original(p, *args, **kwargs)
                    return alternate / relative
                with patch.object(Path, 'resolve', redirect):
                    with self.assertRaises(ValueError):
                        controller.start('helmet_416x416.rknn', 'demo.jpg', 'replay')
                self.assertIsNone(controller.pipeline)
                self.assertEqual(list(alternate.iterdir()), [], 'reject before writing outside application root')

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows junction behavior')
    def test_real_windows_junction_snapshot_escape_is_rejected_before_start(self):
        import subprocess
        results = self.root / 'results'
        with tempfile.TemporaryDirectory() as outside:
            made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(results), outside],
                                  capture_output=True, text=True)
            if made.returncode:
                self.skipTest('Windows junction creation unavailable')
            try:
                controller = self.controller()
                with self.assertRaises(ValueError):
                    controller.start('helmet_416x416.rknn', 'demo.jpg', 'replay')
                self.assertIsNone(controller.pipeline)
                self.assertEqual(list(Path(outside).iterdir()), [])
            finally:
                # Remove only the link using the same native filesystem API;
                # never recursively traverse or remove its external target.
                results.rmdir()

    def test_stop_timeout_keeps_exclusive_ownership_and_cleanup(self):
        controller = self.controller()
        controller.start('helmet_416x416.rknn', 'demo.jpg', 'replay')
        pipeline = controller.pipeline
        pipeline.allow_stop.clear()
        stopped = controller.stop(timeout=.03)
        self.assertFalse(stopped['stopped'])
        self.assertFalse(pipeline.cleaned, 'controller must not release worker resources')
        with self.assertRaises(RuntimeError):
            controller.start('helmet_416x416.rknn', 'demo.jpg', 'replay')
        pipeline.allow_stop.set()
        self.assertTrue(controller.stop(timeout=1)['stopped'])
        self.assertTrue(pipeline.cleaned)
        controller.start('helmet_416x416.rknn', 'demo.jpg', 'replay')
        self.assertIsNot(controller.pipeline, pipeline)

    def test_cached_jpeg_is_rejected_after_ttl_fault_or_epoch_change(self):
        controller = self.server()
        code, _, _ = self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        self.assertEqual(code, 200)
        controller.pipeline.emit()
        code, body, headers = self.request('/api/frame?token=test-secret&nonce=1', token=False)
        self.assertEqual(code, 200)
        self.assertEqual(headers['Content-Type'], 'image/jpeg')
        self.assertEqual(cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR).shape, (24, 32, 3))
        controller.pipeline.valid = False
        controller.pipeline.state = 'fault'
        self.assertEqual(self.request('/api/frame?token=test-secret', token=False)[0], 503)
        controller.pipeline.emit(age=1.5)
        self.assertEqual(self.request('/api/frame?token=test-secret', token=False)[0], 503)
        controller.pipeline.emit()
        controller.pipeline.source_epoch += 1
        self.assertEqual(self.request('/api/frame?token=test-secret', token=False)[0], 503)

    def test_preview_json_binds_encoded_image_to_frame_and_remaining_ttl(self):
        controller = self.server()
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        controller.pipeline.emit(age=.2)
        code, body, _ = self.request('/api/preview?nonce=1')
        self.assertEqual(code, 200)
        package = json.loads(body)
        self.assertEqual(package['frame_id'], 1)
        self.assertEqual(package['source_epoch'], 0)
        self.assertTrue(package['valid'])
        self.assertGreaterEqual(package['source_age_ms'], 200)
        self.assertLessEqual(package['remaining_ttl_ms'], 800)
        image = cv2.imdecode(np.frombuffer(base64.b64decode(package['jpeg_base64']), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image.shape, (24, 32, 3))
        self.assertEqual(self.request('/api/preview?token=test-secret', token=False)[0], 403)
        controller.pipeline.valid = False
        self.assertEqual(self.request('/api/preview')[0], 503)

    def test_status_reveals_missed_invalid_epoch_and_rejects_previous_frame(self):
        controller = self.server()
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        controller.pipeline.emit(epoch=0)
        previous = json.loads(self.request('/api/preview')[1])
        first_status = json.loads(self.request('/api/status')[1])['detection']
        self.assertIn('generation', previous, 'preview requires a public run identity')
        self.assertIn('source_epoch', first_status, 'status must reveal transitions missed between polls')
        self.assertEqual(previous['generation'], first_status['generation'])
        self.assertEqual(first_status['source_epoch'], 0)
        with controller.pipeline.lock:
            # Both source-invalid transitions happen between status polls.
            controller.pipeline.valid = False
            controller.pipeline.source_epoch = 2
            controller.pipeline.valid = True
        recovered = json.loads(self.request('/api/status')[1])['detection']
        self.assertTrue(recovered['valid'])
        self.assertEqual(recovered['source_epoch'], 2)
        self.assertNotEqual(previous['source_epoch'], recovered['source_epoch'])
        self.assertEqual(self.request('/api/preview')[0], 503)
        controller.pipeline.emit(epoch=2)
        fresh = json.loads(self.request('/api/preview')[1])
        self.assertEqual(fresh['generation'], recovered['generation'])
        self.assertEqual(fresh['source_epoch'], recovered['source_epoch'])

    def test_new_run_generation_distinguishes_same_epoch_and_frame_id(self):
        controller = self.server()
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        old_pipeline = controller.pipeline
        old_pipeline.emit()
        previous = json.loads(self.request('/api/preview')[1])
        self.request('/api/stop', {})
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        current_status = json.loads(self.request('/api/status')[1])['detection']
        self.assertIn('generation', previous, 'preview requires a public run identity')
        self.assertIn('generation', current_status, 'status requires the current run identity')
        self.assertNotEqual(previous['generation'], current_status['generation'])
        self.assertEqual(self.request('/api/preview')[0], 503)
        old_pipeline.emit()
        self.assertEqual(self.request('/api/preview')[0], 503, 'old run callback must remain rejected')
        controller.pipeline.emit()
        fresh = json.loads(self.request('/api/preview')[1])
        self.assertEqual(previous['frame_id'], fresh['frame_id'])
        self.assertEqual(previous['source_epoch'], fresh['source_epoch'])
        self.assertNotEqual(previous['generation'], fresh['generation'])
        self.assertEqual(fresh['generation'], current_status['generation'])

    def test_concurrent_reader_does_not_delete_new_same_epoch_frame(self):
        controller = self.server()
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        for expired_old, publications in ((False, 1), (True, 1), (False, 25)):
            with self.subTest(expired_old=expired_old, publications=publications):
                controller.pipeline.emit(frame_id=1)
                before = json.loads(self.request('/api/status')[1])['detection']
                original = controller._fresh
                entered, published = threading.Event(), threading.Event()
                blocked = []
                response = []
                def delayed(packet, pipeline):
                    if ('process_request_thread' in threading.current_thread().name
                            and not entered.is_set()):
                        blocked.append(packet)
                        entered.set()
                        if not published.wait(1):
                            raise RuntimeError('publisher barrier was not released')
                        if expired_old:
                            return False
                    return original(packet, pipeline)
                with patch.object(controller, '_fresh', side_effect=delayed):
                    reader = threading.Thread(target=lambda: response.append(self.request('/api/preview')))
                    reader.start()
                    try:
                        self.assertTrue(entered.wait(1))
                        for frame_id in range(2, publications + 2):
                            controller.pipeline.emit(frame_id=frame_id)
                        published.set()
                        reader.join(1)
                        self.assertFalse(reader.is_alive(), 'preview retry must remain bounded')
                    finally:
                        published.set()
                        reader.join(2)
                self.assertEqual(response[0][0], 200, 'same-epoch publisher must not create a false outage')
                package = json.loads(response[0][1])
                self.assertEqual(package['frame_id'], publications + 1)
                self.assertEqual(package['source_epoch'], 0)
                self.assertEqual(package['generation'], before['generation'])
                self.assertEqual(controller.frame.frame_id, publications + 1)
                followup = json.loads(self.request('/api/preview')[1])
                self.assertEqual(followup['frame_id'], publications + 1)

    def test_new_snapshot_remains_unavailable_when_its_source_epoch_is_invalid(self):
        controller = self.server()
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        controller.pipeline.emit(frame_id=1)
        original = controller._fresh
        entered, published = threading.Event(), threading.Event()
        response = []
        def delayed(packet, pipeline):
            if ('process_request_thread' in threading.current_thread().name
                    and not entered.is_set()):
                entered.set()
                if not published.wait(1):
                    raise RuntimeError('publisher barrier was not released')
            return original(packet, pipeline)
        with patch.object(controller, '_fresh', side_effect=delayed):
            reader = threading.Thread(target=lambda: response.append(self.request('/api/preview')))
            reader.start()
            try:
                self.assertTrue(entered.wait(1))
                controller.pipeline.emit(frame_id=2)
                with controller.pipeline.lock:
                    controller.pipeline.source_epoch = 2
                published.set()
                reader.join(1)
                self.assertFalse(reader.is_alive())
            finally:
                published.set()
                reader.join(2)
        self.assertEqual(response[0][0], 503)
        self.assertIsNone(controller.preview())

    def test_old_run_reader_does_not_delete_restarted_owner_cache(self):
        controller = self.server()
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        controller.pipeline.emit(frame_id=1)
        old_generation = controller.run_uuid
        original = controller._fresh
        entered, restarted = threading.Event(), threading.Event()
        response = []
        def delayed(packet, pipeline):
            if ('process_request_thread' in threading.current_thread().name
                    and not entered.is_set()):
                entered.set()
                if not restarted.wait(1):
                    raise RuntimeError('restart barrier was not released')
            return original(packet, pipeline)
        with patch.object(controller, '_fresh', side_effect=delayed):
            reader = threading.Thread(target=lambda: response.append(self.request('/api/preview')))
            reader.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(controller.stop(timeout=1)['stopped'])
                controller.start('helmet_416x416.rknn', 'demo.jpg', 'replay')
                controller.pipeline.emit(frame_id=1)
                restarted.set()
                reader.join(1)
                self.assertFalse(reader.is_alive())
            finally:
                restarted.set()
                reader.join(2)
        self.assertIsNotNone(controller.frame, 'old reader may not clear a new run owner cache')
        package = json.loads(self.request('/api/preview')[1])
        self.assertNotEqual(package['generation'], old_generation)
        self.assertEqual(package['generation'], controller.run_uuid)

    def test_wifi_operations_are_async_busy_and_results_hide_secrets(self):
        self.server()
        self.wifi.allow_finish.clear()
        code, body, _ = self.request('/api/wifi/connect', {'ssid': 'fixture', 'password': 'secret123',
                                                        'security': 'WPA2-PSK', 'remember': True})
        self.assertEqual(code, 202)
        self.assertNotIn('secret123', body.decode())
        self.assertTrue(self.wifi.entered.wait(1))
        self.assertEqual(self.request('/api/wifi/scan', {})[0], 409)
        self.assertEqual(self.request('/api/status')[0], 200, 'long wifi actions must not block status')
        self.wifi.allow_finish.set()
        action = self.wait_action()
        self.assertEqual(action['status'], 'completed')
        self.assertNotIn('secret123', json.dumps(action))
        self.assertTrue(action['result']['connected'])
        self.assertTrue(action['result']['saved'])

    def test_wifi_dependency_exception_never_echoes_password(self):
        self.server()
        self.wifi.fail = True
        self.request('/api/wifi/connect', {'ssid': 'fixture', 'password': 'secret123', 'security': 'WPA2-PSK'})
        action = self.wait_action()
        self.assertEqual(action['status'], 'failed')
        self.assertNotIn('secret123', json.dumps(action))

    def test_close_rejects_new_wifi_work_and_reports_busy_timeout(self):
        manager = self.module().WifiActions(self.wifi)
        self.wifi.allow_finish.clear()
        manager.start('scan', {})
        self.assertTrue(self.wifi.entered.wait(1))
        self.assertFalse(manager.close(timeout=.03))
        with self.assertRaises(RuntimeError):
            manager.start('scan', {})
        self.wifi.allow_finish.set()
        self.assertTrue(manager.close(timeout=1))

    def test_close_requests_backend_cancellation_before_waiting(self):
        class ClosingWifi(ControlledWifi):
            def __init__(self):
                super().__init__()
                self.closed = False
            def close(self):
                self.closed = True
                self.allow_finish.set()
        wifi = ClosingWifi()
        wifi.allow_finish.clear()
        manager = self.module().WifiActions(wifi)
        manager.start('scan', {})
        self.assertTrue(wifi.entered.wait(1))
        try:
            self.assertTrue(manager.close(timeout=.05))
            self.assertTrue(wifi.closed)
        finally:
            wifi.allow_finish.set()
            manager.close(timeout=1)

    def test_native_worker_still_alive_after_run_error_blocks_restart(self):
        controller = self.controller()
        controller.start('helmet_416x416.rknn', 'demo.jpg', 'replay')
        old = controller.pipeline
        gate = threading.Event()
        old.detect_thread = threading.Thread(target=gate.wait, daemon=True)
        old.detect_thread.start()
        try:
            self.assertFalse(controller.stop(timeout=1)['stopped'])
            with self.assertRaises(RuntimeError):
                controller.start('helmet_416x416.rknn', 'demo.jpg', 'replay')
            self.assertFalse(controller.status()['valid'])
        finally:
            gate.set()
            old.detect_thread.join(1)

    def test_result_expiring_during_encoding_is_not_published(self):
        controller = self.controller()
        controller.start('helmet_416x416.rknn', 'demo.jpg', 'replay')
        original = cv2.imencode
        def interrupted(*args, **kwargs):
            encoded = original(*args, **kwargs)
            controller.pipeline.source_epoch += 1
            return encoded
        with patch.object(self.module().cv2, 'imencode', side_effect=interrupted):
            controller.pipeline.emit()
        self.assertIsNone(controller.preview())

    def test_real_pipeline_outputs_and_stops_with_device_boundary_cleanup(self):
        from helmet.pipeline_runtime import Pipeline
        class Engine:
            def __init__(self):
                self.released = False
            def infer(self, rgb):
                self.assertion = rgb.shape == (416, 416, 3)
                return [np.full((1, 21, grid, grid), -20, np.float32) for grid in (52, 26, 13)]
            def release(self):
                self.released = True
        engine = Engine()
        def factory(args, **kwargs):
            return Pipeline(args, engine=engine, **kwargs)
        controller = self.module().DetectionController(self.root, pipeline_factory=factory)
        self.controllers.append(controller)
        controller.start('helmet_416x416.rknn', 'demo.jpg', 'replay')
        for _ in range(100):
            if controller.preview() is not None:
                break
            time.sleep(.01)
        self.assertIsNotNone(controller.preview())
        self.assertEqual(controller.status()['state'], 'no-detection')
        self.assertTrue(controller.stop(timeout=2)['stopped'])
        self.assertTrue(engine.released)
        self.assertTrue(engine.assertion)
        self.assertIsNone(controller.preview())

    def test_malformed_json_and_dns_rebinding_host_are_rejected(self):
        controller = self.server()
        code, _, _ = self.request('/api/status', headers={'Host': 'outside.example'})
        self.assertEqual(code, 403)
        code, _, _ = self.request('/api/start', [1, 2])
        self.assertEqual(code, 400)
        self.assertIsNone(controller.pipeline)

    def test_private_session_file_does_not_overwrite_previous_secret(self):
        module = self.module()
        token_file = self.root / 'session.token'
        module._private_file(token_file, 'first-secret')
        with self.assertRaises(FileExistsError):
            module._private_file(token_file, 'second-secret')
        self.assertEqual(token_file.read_text(), 'first-secret')

    def resident_server(self, verification=False):
        try:
            resident = importlib.import_module('helmet.app.resident')
        except ImportError:
            self.fail('resident lifecycle controls are not implemented')
        state = resident.ApplicationState(str(self.root), 'a' * 32, verification=verification)
        state.mark_running()
        state.ui_started('b' * 32)
        controller = self.controller()
        self.service = self.module().LocalService(controller, self.wifi, token='test-secret',
                                                  port=0, application=state)
        self.service.start()
        return controller, state

    def app_control(self, command, **fields):
        return self.request('/api/application/' + command,
                            {'session': 'a' * 32, 'request_id': 'test-' + command, **fields})

    def test_application_controls_keep_http_authorization_and_finite_schema(self):
        controller, _ = self.resident_server()
        for command in ('show', 'hide', 'quit', 'ack'):
            route = '/api/application/' + command
            payload = {'session': 'a' * 32, 'request_id': 'click', 'tab': 'wifi'}
            for headers, token in [({}, False), ({'X-Helmet-Token': 'wrong'}, True),
                                   ({'Host': 'outside.example'}, True),
                                   ({'Origin': 'https://outside.example'}, True)]:
                with self.subTest(command=command, headers=headers):
                    self.assertEqual(self.request(route, payload, headers, token)[0], 403)
        self.assertEqual(self.request('/api/application?token=test-secret', token=False)[0], 403)
        code, body, _ = self.app_control('show', tab='wifi', password='never-export-me')
        self.assertEqual(code, 400)
        self.assertNotIn(b'never-export-me', body)
        self.assertIsNone(controller.pipeline)
        self.assertEqual(self.request('/api/application')[0], 200)

    def test_hide_and_ui_crash_keep_same_detection_owner_and_selection(self):
        controller, state = self.resident_server()
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg',
                                    'profile': 'responsive'})
        controller.pipeline.emit(frame_id=7)
        original = controller.status()
        self.assertEqual(self.app_control('hide')[0], 202)
        self.assertFalse(controller.pipeline.stop_event.is_set())
        state.ui_exited(0)
        self.assertEqual(self.app_control('show', tab='wifi')[0], 202)
        current = json.loads(self.request('/api/config')[1])['detection']
        self.assertEqual(current['generation'], original['generation'])
        self.assertEqual(current['selection'], original['selection'])
        self.assertTrue(current['running'])

    def test_reshow_preview_requires_a_capture_after_the_new_visibility_intent(self):
        controller, state = self.resident_server()
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        # A coarse monotonic clock may give capture/show the same tick.
        controller.pipeline.emit(age=.2, frame_id=1)
        self.app_control('hide')
        self.app_control('show', tab='detection')
        boundary = state.snapshot()['intent']['preview_after']
        current = controller.preview_package()
        self.assertIsNotNone(current, 'the old packet must remain fresh before applying the show cutoff')
        self.assertLess(current['captured_at'], boundary)
        self.assertGreater(current['remaining_ttl_ms'], 0)
        self.assertEqual(current['source_epoch'], controller.pipeline.source_epoch)
        self.assertEqual(current['generation'], controller.run_uuid)
        self.assertEqual(self.request('/api/preview?after=' + str(boundary))[0], 503)
        controller.pipeline.emit(frame_id=2)
        code, body, _ = self.request('/api/preview?after=' + str(boundary))
        self.assertEqual(code, 200)
        package = json.loads(body)
        self.assertEqual(package['frame_id'], 2)
        self.assertGreaterEqual(package['captured_at'], boundary)
        self.assertEqual(package['generation'], controller.run_uuid)
        self.assertEqual(self.request('/api/preview?after=NaN')[0], 400)

    def test_removed_running_input_is_reported_without_replacing_the_selection(self):
        controller, _ = self.resident_server()
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        original_generation = controller.run_uuid
        (self.root / 'input/demo.jpg').unlink()
        cv2.imwrite(str(self.root / 'input/1.jpg'), np.zeros((24, 32, 3), np.uint8))
        # Keep this fixture independent from cameras attached to the test host.
        # Camera discovery itself is covered by dedicated sysfs fixtures.
        with patch.object(self.module(), '_camera_sources', return_value=[]):
            value = json.loads(self.request('/api/config')[1])
        self.assertEqual(value['detection']['selection']['source'], 'demo.jpg')
        self.assertFalse(value['selection_available']['source'])
        self.assertEqual(value['detection']['generation'], original_generation)
        self.assertEqual([row['id'] for row in value['sources']], ['1.jpg'])

    def test_quit_preserves_readable_stopping_and_refuses_all_new_work(self):
        controller, state = self.resident_server()
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        controller.pipeline.allow_stop.clear()
        self.wifi.allow_finish.clear()
        self.request('/api/wifi/scan', {})
        self.assertTrue(self.wifi.entered.wait(1))
        code, body, _ = self.app_control('quit')
        self.assertEqual(code, 202)
        self.assertEqual(json.loads(body)['application']['phase'], 'stopping')
        progress = self.service.close(timeout=.03)
        self.assertFalse(progress['detection_stopped'])
        self.assertFalse(progress['wifi_stopped'])
        self.assertTrue(self.service.thread.is_alive(), 'HTTP must survive live cleanup owners')
        code, body, _ = self.request('/api/status')
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)['detection']['running'])
        self.assertFalse(json.loads(body)['detection']['valid'])
        self.assertEqual(self.request('/api/application')[0], 200)
        self.assertEqual(self.request('/api/start', {})[0], 409)
        self.assertEqual(self.request('/api/wifi/connect', {'ssid': 'fixture', 'security': 'open'})[0], 409)
        self.assertEqual(self.app_control('show', tab='wifi')[0], 409)
        self.assertEqual(state.snapshot()['phase'], 'stopping')
        controller.pipeline.allow_stop.set()
        self.wifi.allow_finish.set()
        finished = self.service.close(timeout=1)
        self.assertTrue(all(finished.values()))

    def test_closed_http_is_not_evidence_that_native_detection_has_released(self):
        controller, _ = self.resident_server()
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        controller.pipeline.allow_stop.clear()
        self.service.server.shutdown()
        self.service.thread.join(1)
        self.service.begin_shutdown()
        self.assertTrue(controller.status()['running'])
        with self.assertRaises(RuntimeError):
            controller.start('helmet_416x416.rknn', 'demo.jpg')
        self.assertFalse(self.service.close(timeout=.02)['detection_stopped'])

    def test_native_render_receipt_uses_displayed_packet_and_backend_completion_ttl(self):
        controller, state = self.resident_server(verification=True)
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        self.app_control('show', tab='detection')
        controller.pipeline.emit(frame_id=7)
        preview = controller.preview_package()
        packet = {key: preview[key] for key in ('frame_id', 'captured_at', 'generation', 'source_epoch')}
        packet['selection'] = controller.status()['selection']
        controller.pipeline.emit(frame_id=99)  # The captured screenshot still depicts frame 7.
        fields = {'ui_instance': 'b' * 32, 'sequence': 1, 'visible': True, 'tab': 'detection',
                  'saved': True, 'capture_id': 'show-1-' + 'b' * 32, 'width': 720, 'height': 1120,
                  'packet': packet}
        before = time.monotonic()
        code, body, _ = self.app_control('render', **fields)
        self.assertEqual(code, 202)
        receipt = json.loads(body)['application']['last_render']
        self.assertEqual(receipt['packet']['frame_id'], 7)
        self.assertTrue(receipt['valid_at_receipt'])
        self.assertGreaterEqual(receipt['received_at'], before)
        self.assertGreater(receipt['remaining_ttl_ms'], 0)
        expired = {**packet, 'captured_at': time.monotonic() - 2}
        code, body, _ = self.request('/api/application/render',
                                    {'session': 'a' * 32, 'request_id': 'expired-render',
                                     **fields, 'packet': expired})
        self.assertEqual(code, 202)
        receipt = json.loads(body)['application']['last_render']
        self.assertFalse(receipt['valid_at_receipt'])
        self.assertEqual(receipt['remaining_ttl_ms'], 0)
        self.assertEqual(state.snapshot()['last_render']['packet']['frame_id'], 7)

    def test_render_completion_ttl_includes_a_slow_resident_lookup(self):
        controller, _ = self.resident_server(verification=True)
        self.request('/api/start', {'model': 'helmet_416x416.rknn', 'source': 'demo.jpg'})
        self.app_control('show', tab='detection')
        controller.pipeline.emit(frame_id=7)
        preview = controller.preview_package()
        packet = {key: preview[key] for key in ('frame_id', 'captured_at', 'generation', 'source_epoch')}
        packet['selection'] = controller.status()['selection']
        original = self.service.application_snapshot
        def delayed():
            value = original()
            time.sleep(1.05)
            return value
        with patch.object(self.service, 'application_snapshot', side_effect=delayed):
            code, body, _ = self.app_control('render', ui_instance='b' * 32, sequence=1,
                visible=True, tab='detection', saved=True, capture_id='show-1-' + 'b' * 32,
                width=720, height=1120, packet=packet)
        self.assertEqual(code, 202)
        receipt = json.loads(body)['application']['last_render']
        self.assertFalse(receipt['valid_at_receipt'])
        self.assertGreaterEqual(receipt['received_at'] - packet['captured_at'], 1.0)

    def test_invalid_or_reused_quit_request_does_not_close_a_running_service(self):
        controller, state = self.resident_server()
        self.app_control('show', tab='wifi')
        for request_id in (['nested'], 'test-show'):
            code, _, _ = self.request('/api/application/quit',
                                      {'session': 'a' * 32, 'request_id': request_id})
            self.assertEqual(code, 400)
            self.assertFalse(self.service.stopping)
            self.assertFalse(controller.closed)
            self.assertEqual(state.snapshot()['phase'], 'running')

    def test_resident_quit_intent_fences_new_http_work_before_cleanup_dispatch(self):
        controller, state = self.resident_server()
        state.request('quit', request_id='unix-quit')
        self.assertFalse(self.service.stopping, 'the resident intent precedes service cleanup dispatch')
        self.assertEqual(self.request('/api/start', {})[0], 409)
        self.assertEqual(self.request('/api/wifi/scan', {})[0], 409)
        self.assertIsNone(controller.pipeline)
        self.assertIsNone(self.service.actions.worker)


if __name__ == '__main__':
    unittest.main()
