"""AlertHub composition, evidence publication and lifecycle contracts."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest import mock


class FakeTracker:
    def __init__(self, candidate=None):
        self.candidate = candidate
        self.calls = []
        self.resets = []

    def observe(self, frame_id, captured_at, source_epoch, generation, detections):
        self.calls.append((frame_id, captured_at, source_epoch, generation, detections))
        return self.candidate

    def reset(self):
        self.resets.append(True)


class FakeStore:
    def __init__(self, events):
        self.events = list(events)
        self.publish_calls = []
        self.cleanup_calls = 0
        self.close_calls = 0

    def cleanup(self):
        self.cleanup_calls += 1
        return {'removed': 0, 'remaining': len(self.events)}

    def publish(self, candidate, jpeg, metadata):
        self.publish_calls.append((candidate, jpeg, metadata))
        if self.events and isinstance(self.events[0], Exception):
            raise self.events.pop(0)
        if self.events:
            return self.events.pop(0)
        return {
            'event_id': 'event-1',
            'captured_wall_time': candidate.captured_wall_time,
            'media_path': '/fixture/event-1.jpg',
        }

    def status(self):
        return {'records': len(self.publish_calls), 'closed': self.close_calls > 0}

    def close(self):
        self.close_calls += 1


class FakeViewer:
    def __init__(self, fail=False, close_failures=0):
        self.fail = fail
        self.close_failures = close_failures
        self.start_calls = 0
        self.close_calls = 0
        self.thread = None
        self.port = 8092

    def start(self):
        self.start_calls += 1
        if self.fail:
            raise OSError('event port is occupied')
        self.thread = SimpleNamespace(is_alive=lambda: self.close_calls == 0)

    def close(self):
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            return False
        return True


class AlertHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from helmet.app.alert_events import EventCandidate
        self.candidate = EventCandidate(
            captured_at=100.0,
            captured_wall_time='2026-11-24T14:25:30.000+08:00',
            frame_id=42,
            source_epoch=3,
            generation='run-a',
            trigger_track_ids=[1],
            visible_no_hat_track_ids=[1, 2],
            detections=[
                {'track_id': 1, 'box': [10, 10, 50, 50],
                 'score': .9, 'class': 'person'},
            ],
        )

    def module(self):
        try:
            from helmet.app import alert_hub
        except ImportError as error:
            self.fail('alert hub is not implemented: ' + str(error))
        return alert_hub

    def packet(self):
        return SimpleNamespace(
            frame_id=42,
            captured_at=100.0,
            metadata={
                'valid': True,
                'source_epoch': 3,
                'model': 'helmet_416x416.rknn',
                'source': '/dev/video10',
                'profile': 'responsive',
            },
            detections=[([10, 10, 50, 50], False, .9)],
        )

    def hub(self, *, viewer=None, events=None, cleanup_interval=3600):
        module = self.module()
        self.store = FakeStore(events or [])
        self.tracker = FakeTracker(self.candidate)
        self.viewer = viewer
        return module.AlertHub(
            self.root,
            store=self.store,
            tracker=self.tracker,
            viewer=self.viewer,
            host='127.0.0.1',
            port=8092,
            cleanup_interval=cleanup_interval,
        )

    def test_confirmed_candidate_is_saved_once_without_notification_side_effects(self):
        hub = self.hub()
        hub.start()
        self.assertTrue(hub.observe(self.packet(), b'\xff\xd8event\xff\xd9', 'run-a'))

        self.assertEqual(self.store.cleanup_calls, 1)
        self.assertEqual(len(self.tracker.calls), 1)
        candidate, jpeg, metadata = self.store.publish_calls[0]
        self.assertIs(candidate, self.candidate)
        self.assertEqual(jpeg, b'\xff\xd8event\xff\xd9')
        self.assertEqual(metadata['model'], 'helmet_416x416.rknn')
        self.assertEqual(metadata['selection'],
                         {'source': '/dev/video10', 'profile': 'responsive'})
        status = hub.status()
        self.assertEqual(status['events_created'], 1)
        self.assertNotIn('mail', status)

    def test_store_failure_is_contained(self):
        hub = self.hub(events=[OSError('disk fixture')])
        hub.start()
        self.assertFalse(hub.observe(self.packet(), b'jpeg', 'run-a'))
        status = hub.status()
        self.assertEqual(status['last_error'], 'event_store_failed')
        self.assertEqual(status['events_created'], 0)
        self.assertNotIn('disk fixture', str(status))

    def test_reset_clears_tracker_and_records_generation(self):
        hub = self.hub()
        hub.start()
        hub.reset('run-b')
        self.assertEqual(self.tracker.resets, [True])
        self.assertEqual(hub.status()['generation'], 'run-b')

    def test_observation_from_an_old_generation_is_rejected_after_reset(self):
        hub = self.hub()
        hub.start()
        hub.reset('run-b')
        self.assertFalse(hub.observe(
            self.packet(), b'jpeg', 'run-a'))
        self.assertEqual(self.tracker.calls, [])
        self.assertEqual(self.store.publish_calls, [])

    def test_start_and_close_order_is_explicit_and_close_is_idempotent(self):
        viewer = FakeViewer()
        hub = self.hub(viewer=viewer)
        hub.start()
        self.assertEqual((self.store.cleanup_calls, viewer.start_calls), (1, 1))
        self.assertTrue(hub.status()['viewer']['alive'])
        self.assertTrue(hub.close(timeout=1))
        self.assertEqual((viewer.close_calls, self.store.close_calls), (1, 1))
        self.assertTrue(hub.close(timeout=1))
        self.assertEqual((viewer.close_calls, self.store.close_calls), (1, 1))
        self.assertFalse(hub.status()['viewer']['alive'])

    def test_viewer_start_failure_rolls_back_started_owners(self):
        viewer = FakeViewer(fail=True)
        hub = self.hub(viewer=viewer)
        with self.assertRaises(OSError):
            hub.start()
        self.assertEqual(viewer.close_calls, 1)
        self.assertEqual(self.store.close_calls, 1)
        self.assertFalse(hub.status()['started'])

    def test_close_can_retry_after_viewer_cleanup_is_not_confirmed(self):
        viewer = FakeViewer(close_failures=1)
        hub = self.hub(viewer=viewer)
        hub.start()
        self.assertFalse(hub.close(timeout=.01))
        self.assertTrue(hub.close(timeout=1))
        self.assertEqual(viewer.close_calls, 2)

    def test_cleanup_runs_periodically_while_the_hub_is_open(self):
        hub = self.hub(cleanup_interval=.02)
        hub.start()
        deadline = time.monotonic() + 1
        while self.store.cleanup_calls < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertGreaterEqual(
            self.store.cleanup_calls, 2,
            'the resident hub must apply retention beyond startup cleanup',
        )
        self.assertTrue(hub.close(timeout=1))

    def test_observe_after_close_is_a_noop(self):
        hub = self.hub()
        hub.start()
        hub.close(timeout=1)
        self.assertFalse(hub.observe(self.packet(), b'jpeg', 'run-a'))
        self.assertEqual(self.store.publish_calls, [])

    def test_from_root_port_zero_needs_no_event_key_or_mail_configuration(self):
        module = self.module()
        hub = module.AlertHub.from_root(self.root, port=0)
        self.addCleanup(hub.close, 1)
        hub.start()
        self.assertFalse(hub.status()['viewer']['alive'])
        self.assertEqual(hub.status()['viewer']['port'], None)
        self.assertFalse((self.root / 'app-state' / 'event-access.key').exists())
        self.assertFalse((self.root / 'app-state' / 'mail.json').exists())

    def test_from_root_builds_the_direct_viewer_without_credentials(self):
        module = self.module()
        viewer = FakeViewer()
        with mock.patch.object(module, 'LanEventServer', return_value=viewer) as server:
            hub = module.AlertHub.from_root(self.root, port=8092)
            self.addCleanup(hub.close, 1)
            hub.start()
        server.assert_called_once()
        args, kwargs = server.call_args
        self.assertIs(args[0], hub.store)
        self.assertEqual(args[1].__self__, hub)
        self.assertEqual(kwargs, {'host': '0.0.0.0', 'port': 8092})
        self.assertEqual(viewer.start_calls, 1)
        self.assertFalse((self.root / 'app-state').exists())

    def test_direct_lan_url_contains_no_invitation_or_secret(self):
        hub = self.hub()
        self.assertEqual(hub.lan_url('192.168.1.10'), 'http://192.168.1.10:8092/')
        with self.assertRaises(ValueError):
            hub.lan_url('8.8.8.8')

    def test_bound_detection_provider_exposes_current_alarm_state_only(self):
        hub = self.hub()
        hub.bind_status_provider(lambda: {
            'state': 'healthy',
            'running': True,
            'valid': True,
            'generation': 'run-a',
            'alarm_state': 'active',
            'frame_id': 42,
            'source_epoch': 3,
            'public_ip': '203.0.113.10',
        })
        hub.start()
        status = hub.status()
        self.assertEqual(status['detection']['alarm_state'], 'active')
        self.assertEqual(status['detection']['frame_id'], 42)
        self.assertNotIn('public_ip', status)
        self.assertNotIn('mail', status)
        self.assertEqual(status['viewer']['scope'], 'lan')


if __name__ == '__main__':
    unittest.main()
