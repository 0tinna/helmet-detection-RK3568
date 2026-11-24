"""AlertHub composition, failure isolation and lifecycle contracts."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest


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


class FakeMailer:
    def __init__(self, fail=False, close_failures=0):
        self.fail = fail
        self.close_failures = close_failures
        self.start_calls = 0
        self.enqueue_calls = []
        self.close_calls = 0

    def start(self):
        self.start_calls += 1

    def enqueue(self, events):
        self.enqueue_calls.append(events)
        if self.fail:
            raise OSError('private SMTP response')
        return True

    def status(self):
        return {
            'configured': True,
            'state': 'failed' if self.fail else 'idle',
            'pending_batches': 0,
            'worker_alive': self.start_calls > self.close_calls,
        }

    def close(self, timeout=3.0):
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            return False
        return True


class FakeViewer:
    def __init__(self, fail=False):
        self.fail = fail
        self.start_calls = 0
        self.close_calls = 0
        self.thread = None
        self.port = 8092

    def start(self):
        self.start_calls += 1
        if self.fail:
            raise OSError('public port is occupied')
        self.thread = SimpleNamespace(is_alive=lambda: self.close_calls == 0)

    def close(self):
        self.close_calls += 1


class FakeAccessKey:
    def url_path(self, public_host, public_port=8092, scheme='http', now=None):
        return '%s://%s:%d/alerts?access=fixture-token' % (
            scheme, public_host, public_port)


class FakeResolver:
    def __init__(self):
        self.calls = 0

    def resolve(self):
        self.calls += 1
        return {
            'address': '203.0.113.10',
            'checked_at': '2026-11-24T04:00:00+00:00',
            'stale': False,
        }


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

    def hub(self, *, mailer=None, viewer=None, events=None, cleanup_interval=3600):
        module = self.module()
        self.store = FakeStore(events or [])
        self.tracker = FakeTracker(self.candidate)
        self.mailer = mailer
        self.viewer = viewer
        self.resolver = FakeResolver()
        return module.AlertHub(
            self.root,
            store=self.store,
            tracker=self.tracker,
            mailer=self.mailer,
            resolver=self.resolver,
            access_key=FakeAccessKey(),
            viewer=self.viewer,
            host='127.0.0.1',
            port=8092,
            cleanup_interval=cleanup_interval,
        )

    def test_confirmed_candidate_is_published_before_mail_enqueue(self):
        mailer = FakeMailer()
        hub = self.hub(mailer=mailer)
        hub.start()
        self.assertTrue(hub.observe(self.packet(), b'\xff\xd8event\xff\xd9', 'run-a'))

        self.assertEqual(self.store.cleanup_calls, 1)
        self.assertEqual(mailer.start_calls, 1)
        self.assertEqual(len(self.tracker.calls), 1)
        candidate, jpeg, metadata = self.store.publish_calls[0]
        self.assertIs(candidate, self.candidate)
        self.assertEqual(jpeg, b'\xff\xd8event\xff\xd9')
        self.assertEqual(metadata['model'], 'helmet_416x416.rknn')
        self.assertEqual(metadata['selection'],
                         {'source': '/dev/video10', 'profile': 'responsive'})
        self.assertEqual(len(mailer.enqueue_calls), 1)
        self.assertEqual(mailer.enqueue_calls[0][0]['event_id'], 'event-1')
        self.assertEqual(hub.status()['events_created'], 1)

    def test_store_failure_is_contained_and_mail_is_not_enqueued(self):
        mailer = FakeMailer()
        hub = self.hub(mailer=mailer, events=[OSError('disk fixture')])
        hub.start()
        self.assertFalse(hub.observe(self.packet(), b'jpeg', 'run-a'))
        self.assertEqual(mailer.enqueue_calls, [])
        status = hub.status()
        self.assertEqual(status['last_error'], 'event_store_failed')
        self.assertEqual(status['events_created'], 0)
        self.assertNotIn('disk fixture', str(status))

    def test_absent_mail_configuration_does_not_block_saved_evidence(self):
        hub = self.hub(mailer=None)
        hub.start()
        self.assertTrue(hub.observe(self.packet(), b'jpeg', 'run-a'))
        self.assertEqual(len(self.store.publish_calls), 1)
        self.assertFalse(hub.status()['mail']['configured'])

    def test_mail_failure_does_not_raise_or_remove_published_event(self):
        mailer = FakeMailer(fail=True)
        hub = self.hub(mailer=mailer)
        hub.start()
        self.assertTrue(hub.observe(self.packet(), b'jpeg', 'run-a'))
        self.assertEqual(len(self.store.publish_calls), 1)
        self.assertEqual(hub.status()['events_created'], 1)
        self.assertEqual(hub.status()['last_error'], 'mail_enqueue_failed')
        self.assertNotIn('private SMTP response', str(hub.status()))

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
        mailer = FakeMailer()
        viewer = FakeViewer()
        hub = self.hub(mailer=mailer, viewer=viewer)
        hub.start()
        self.assertEqual((self.store.cleanup_calls, mailer.start_calls,
                          viewer.start_calls), (1, 1, 1))
        self.assertTrue(hub.status()['viewer']['alive'])
        self.assertTrue(hub.close(timeout=1))
        self.assertEqual((viewer.close_calls, mailer.close_calls,
                          self.store.close_calls), (1, 1, 1))
        self.assertTrue(hub.close(timeout=1))
        self.assertEqual((viewer.close_calls, mailer.close_calls,
                          self.store.close_calls), (1, 1, 1))
        self.assertFalse(hub.status()['viewer']['alive'])

    def test_viewer_start_failure_rolls_back_already_started_owners(self):
        mailer = FakeMailer()
        viewer = FakeViewer(fail=True)
        hub = self.hub(mailer=mailer, viewer=viewer)
        with self.assertRaises(OSError):
            hub.start()
        self.assertEqual(mailer.close_calls, 1)
        self.assertEqual(viewer.close_calls, 1)
        self.assertEqual(self.store.close_calls, 1)
        self.assertFalse(hub.status()['started'])

    def test_failed_start_rollback_does_not_report_an_alive_mailer_as_closed(self):
        mailer = FakeMailer(close_failures=2)
        viewer = FakeViewer(fail=True)
        hub = self.hub(mailer=mailer, viewer=viewer)
        with self.assertRaises(OSError):
            hub.start()
        self.assertEqual(mailer.close_calls, 1)
        self.assertFalse(hub.close(timeout=1))
        self.assertEqual(mailer.close_calls, 2)
        self.assertTrue(hub.close(timeout=1))
        self.assertEqual(mailer.close_calls, 3)

    def test_close_can_retry_after_a_mail_worker_timeout(self):
        mailer = FakeMailer(close_failures=1)
        hub = self.hub(mailer=mailer)
        hub.start()
        self.assertFalse(hub.close(timeout=.01))
        self.assertTrue(hub.close(timeout=1))
        self.assertEqual(mailer.close_calls, 2)

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

    def test_from_root_port_zero_needs_no_public_key_or_mail_configuration(self):
        module = self.module()
        hub = module.AlertHub.from_root(self.root, port=0)
        self.addCleanup(hub.close, 1)
        hub.start()
        self.assertFalse(hub.status()['viewer']['alive'])
        self.assertEqual(hub.status()['viewer']['port'], None)
        self.assertFalse((self.root / 'app-state' / 'public-access.key').exists())

    def test_public_url_builder_uses_dynamic_address_and_configured_port(self):
        hub = self.hub()
        self.assertEqual(
            hub.access_url('203.0.113.10'),
            'http://203.0.113.10:8092/alerts?access=fixture-token',
        )

    def test_bound_detection_provider_is_exposed_with_the_public_address(self):
        hub = self.hub()
        hub.bind_status_provider(
            lambda: {'running': True, 'valid': True, 'state': 'running'})
        hub.start()
        status = hub.status()
        self.assertEqual(status['detection']['running'], True)
        self.assertEqual(status['public_ip']['address'], '203.0.113.10')
        self.assertFalse(status['public_ip']['stale'])

    def test_repeated_status_reads_reuse_the_public_address_query(self):
        hub = self.hub()
        first = hub.status()
        second = hub.status()
        self.assertEqual(first['public_ip'], second['public_ip'])
        self.assertEqual(self.resolver.calls, 1)

    def test_status_can_read_cached_public_address_without_network(self):
        hub = self.hub()
        status = hub.status(refresh_public_ip=False)
        self.assertIsNone(status['public_ip']['address'])
        self.assertEqual(self.resolver.calls, 0)


if __name__ == '__main__':
    unittest.main()
