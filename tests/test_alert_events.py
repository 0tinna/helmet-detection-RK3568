"""Track-aware no-hat events and durable evidence storage."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

import numpy as np


class NoHatTrackTrackerTests(unittest.TestCase):
    def module(self):
        try:
            from helmet.app import alert_events
        except ImportError as error:
            self.fail('no-hat event tracker is not implemented: ' + str(error))
        return alert_events

    @staticmethod
    def detection(box, wearing=False, score=.9):
        return (box, wearing, score)

    def tracker(self, **kwargs):
        return self.module().NoHatTrackTracker(**kwargs)

    def test_one_continuous_no_hat_track_creates_one_event(self):
        tracker = self.tracker()
        self.assertIsNone(tracker.observe(
            1, 10.0, 0, 'run-a', [self.detection([10, 10, 50, 50])]))
        event = tracker.observe(
            2, 10.1, 0, 'run-a', [self.detection([11, 10, 51, 50])])
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger_track_ids, [1])
        self.assertEqual(event.visible_no_hat_track_ids, [1])
        for frame_id in range(3, 8):
            self.assertIsNone(tracker.observe(
                frame_id, 10.0 + frame_id / 10,
                0, 'run-a', [self.detection([12, 11, 52, 50])]))

    def test_pipeline_numpy_boxes_create_a_track_event(self):
        tracker = self.tracker()
        first = self.detection(np.array([10, 10, 50, 50], dtype=np.float32))
        second = self.detection(np.array([11, 10, 51, 50], dtype=np.float32))
        self.assertIsNone(tracker.observe(1, 10.0, 0, 'run-a', [first]))
        event = tracker.observe(2, 10.1, 0, 'run-a', [second])
        self.assertIsNotNone(event)
        self.assertEqual(event.trigger_track_ids, [1])
        self.assertEqual(event.detections[0]['box'], [11.0, 10.0, 51.0, 50.0])

    def test_two_simultaneous_heads_create_one_candidate_with_two_tracks(self):
        tracker = self.tracker()
        detections = [
            self.detection([10, 10, 50, 50]),
            self.detection([100, 10, 140, 50]),
        ]
        self.assertIsNone(tracker.observe(1, 10.0, 0, 'run-a', detections))
        event = tracker.observe(2, 10.1, 0, 'run-a', detections)
        self.assertEqual(event.trigger_track_ids, [1, 2])
        self.assertEqual(event.visible_no_hat_track_ids, [1, 2])
        self.assertEqual(event.detections[0]['class'], 'person')

    def test_later_entrant_creates_a_new_event_while_first_track_remains(self):
        tracker = self.tracker()
        first = self.detection([10, 10, 50, 50])
        second = self.detection([100, 10, 140, 50])
        self.assertIsNone(tracker.observe(1, 10.0, 0, 'run-a', [first]))
        first_event = tracker.observe(2, 10.1, 0, 'run-a', [first])
        self.assertEqual(first_event.trigger_track_ids, [1])
        self.assertIsNone(tracker.observe(3, 10.2, 0, 'run-a', [first, second]))
        second_event = tracker.observe(4, 10.3, 0, 'run-a', [first, second])
        self.assertEqual(second_event.trigger_track_ids, [2])
        self.assertEqual(second_event.visible_no_hat_track_ids, [1, 2])

    def test_short_miss_does_not_reset_an_existing_track(self):
        tracker = self.tracker()
        box = self.detection([10, 10, 50, 50])
        self.assertIsNone(tracker.observe(1, 10.0, 0, 'run-a', [box]))
        self.assertIsNotNone(tracker.observe(2, 10.1, 0, 'run-a', [box]))
        self.assertIsNone(tracker.observe(3, 11.5, 0, 'run-a', []))
        self.assertIsNone(tracker.observe(4, 11.6, 0, 'run-a', [box]))

    def test_reentry_after_max_gap_creates_a_new_track_and_event(self):
        tracker = self.tracker()
        box = self.detection([10, 10, 50, 50])
        self.assertIsNone(tracker.observe(1, 10.0, 0, 'run-a', [box]))
        self.assertIsNotNone(tracker.observe(2, 10.1, 0, 'run-a', [box]))
        self.assertIsNone(tracker.observe(3, 12.3, 0, 'run-a', [box]))
        event = tracker.observe(4, 12.4, 0, 'run-a', [box])
        self.assertEqual(event.trigger_track_ids, [2])

    def test_generation_or_epoch_change_resets_track_history(self):
        tracker = self.tracker()
        box = self.detection([10, 10, 50, 50])
        self.assertIsNone(tracker.observe(1, 10.0, 0, 'run-a', [box]))
        self.assertIsNotNone(tracker.observe(2, 10.1, 0, 'run-a', [box]))
        self.assertIsNone(tracker.observe(3, 10.2, 0, 'run-b', [box]))
        self.assertIsNotNone(tracker.observe(4, 10.3, 0, 'run-b', [box]))
        self.assertIsNone(tracker.observe(5, 10.4, 1, 'run-b', [box]))
        self.assertIsNotNone(tracker.observe(6, 10.5, 1, 'run-b', [box]))

    def test_overlapping_heads_are_not_collapsed_into_one_track(self):
        tracker = self.tracker()
        detections = [
            self.detection([10, 10, 50, 50]),
            self.detection([12, 11, 52, 51]),
        ]
        self.assertIsNone(tracker.observe(1, 10.0, 0, 'run-a', detections))
        event = tracker.observe(2, 10.1, 0, 'run-a', detections)
        self.assertEqual(event.trigger_track_ids, [1, 2])

    def test_wearing_hat_detections_never_create_an_event(self):
        tracker = self.tracker()
        detections = [self.detection([10, 10, 50, 50], wearing=True)]
        for frame_id in range(1, 6):
            self.assertIsNone(tracker.observe(
                frame_id, 10.0 + frame_id / 10, 0, 'run-a', detections))


class AlertEventStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def module(self):
        try:
            from helmet.app import alert_events
        except ImportError as error:
            self.fail('alert event store is not implemented: ' + str(error))
        return alert_events

    def candidate(self, captured_at=None, wall_time=None):
        module = self.module()
        if captured_at is None:
            captured_at = 100.0
        if wall_time is None:
            wall_time = datetime(2026, 11, 24, 14, 25, 30,
                                 tzinfo=timezone(timedelta(hours=8))).isoformat()
        return module.EventCandidate(
            captured_at=captured_at,
            captured_wall_time=wall_time,
            frame_id=42,
            source_epoch=3,
            generation='run-generation',
            trigger_track_ids=[1],
            visible_no_hat_track_ids=[1],
            detections=[{
                'track_id': 1,
                'box': [10, 10, 50, 50],
                'score': .9,
                'class': 'person',
            }],
        )

    def store(self, **kwargs):
        return self.module().AlertEventStore(self.root, **kwargs)

    def publish(self, store, candidate=None, jpeg=b'\xff\xd8fixture\xff\xd9'):
        return store.publish(
            candidate or self.candidate(),
            jpeg,
            {'model': 'helmet_416x416.rknn',
             'selection': {'source': '/dev/video10', 'profile': 'responsive'},
             'public_ip': '203.0.113.10'},
        )

    def test_publish_creates_a_dated_jpeg_and_metadata_pair(self):
        store = self.store()
        self.addCleanup(store.close)
        record = self.publish(store)
        event_root = self.root / 'results' / 'helmet-events'
        image = Path(record['media_path'])
        metadata = image.with_suffix('.json')
        self.assertTrue(image.is_file())
        self.assertTrue(metadata.is_file())
        self.assertEqual(image.parent.parent.parent.parent, event_root)
        self.assertRegex(image.parent.name, r'^\d{2}$')
        stored = json.loads(metadata.read_text(encoding='utf-8'))
        self.assertEqual(stored['event_id'], record['event_id'])
        self.assertEqual(stored['image'], record['image'])
        self.assertEqual(stored['count'], 1)
        self.assertNotIn('public_ip', stored)

    def test_public_list_is_newest_first_and_hides_private_paths(self):
        store = self.store()
        self.addCleanup(store.close)
        first_time = datetime(2026, 11, 23, 10, 0,
                              tzinfo=timezone(timedelta(hours=8))).isoformat()
        second_time = datetime(2026, 11, 24, 10, 0,
                               tzinfo=timezone(timedelta(hours=8))).isoformat()
        first = self.publish(store, self.candidate(100, first_time), b'old')
        second = self.publish(store, self.candidate(200, second_time), b'new')
        events = store.list_events()
        self.assertEqual([row['event_id'] for row in events],
                         [second['event_id'], first['event_id']])
        for row in events:
            self.assertNotIn('generation', row)
            self.assertNotIn('media_path', row)
            self.assertNotIn('public_ip', row)
            self.assertNotIn(str(self.root), json.dumps(row))

    def test_event_id_and_media_lookup_reject_invalid_or_outside_paths(self):
        store = self.store()
        self.addCleanup(store.close)
        record = self.publish(store)
        self.assertIsNone(store.media_path('../outside.jpg'))
        self.assertIsNone(store.media_path('not-an-event'))
        self.assertIsNone(store.media_path('/absolute/path.jpg'))
        self.assertEqual(store.media_path(record['event_id']),
                         Path(record['media_path']).resolve())

    def test_cleanup_removes_expired_events(self):
        store = self.store(retention_days=7)
        self.addCleanup(store.close)
        now = datetime(2026, 11, 24, 12, 0,
                       tzinfo=timezone(timedelta(hours=8)))
        old = self.candidate(100, (now - timedelta(days=8)).isoformat())
        current = self.candidate(200, (now - timedelta(days=2)).isoformat())
        old_record = self.publish(store, old)
        current_record = self.publish(store, current)
        result = store.cleanup(now=now)
        self.assertEqual(result['removed'], 1)
        self.assertIsNone(store.media_path(old_record['event_id']))
        self.assertIsNotNone(store.media_path(current_record['event_id']))

    def test_cleanup_enforces_event_count_limit_oldest_first(self):
        store = self.store(max_events=2)
        self.addCleanup(store.close)
        base = datetime(2026, 11, 24, 12, 0,
                        tzinfo=timezone(timedelta(hours=8)))
        records = [
            self.publish(store, self.candidate(
                index, (base + timedelta(minutes=index)).isoformat()), b'x' * 20)
            for index in range(3)
        ]
        result = store.cleanup(now=base + timedelta(minutes=10))
        self.assertEqual(result['removed'], 1)
        self.assertIsNone(store.media_path(records[0]['event_id']))
        self.assertEqual(len(store.list_events()), 2)

    def test_cleanup_keeps_an_event_indexed_when_its_files_cannot_be_deleted(self):
        store = self.store(max_events=1)
        self.addCleanup(store.close)
        base = datetime(2026, 11, 24, 12, 0,
                        tzinfo=timezone(timedelta(hours=8)))
        oldest = self.publish(
            store, self.candidate(100, base.isoformat()), b'old')
        self.publish(
            store, self.candidate(200, (base + timedelta(minutes=1)).isoformat()),
            b'new')
        with mock.patch.object(Path, 'unlink', side_effect=PermissionError('locked')):
            result = store.cleanup(now=base + timedelta(minutes=10))
        self.assertEqual(result['removed'], 0)
        self.assertEqual(result['remaining'], 2)
        self.assertIsNotNone(store.media_path(oldest['event_id']))

    def test_publish_refuses_when_free_space_is_below_the_floor(self):
        store = self.store(free_space_floor=1024)
        self.addCleanup(store.close)
        usage = type('Usage', (), {'total': 4096, 'used': 4000, 'free': 96})()
        with mock.patch('helmet.app.alert_events.shutil.disk_usage',
                        return_value=usage):
            with self.assertRaises(OSError):
                self.publish(store)
        self.assertEqual(store.list_events(), [])

    def test_index_skips_corrupt_metadata_and_reports_it(self):
        event_root = self.root / 'results' / 'helmet-events' / '2026' / '11' / '24'
        event_root.mkdir(parents=True)
        (event_root / 'broken.json').write_text('{', encoding='utf-8')
        store = self.store()
        self.addCleanup(store.close)
        status = store.status()
        self.assertEqual(status['corrupt_records'], 1)
        self.assertEqual(store.list_events(), [])

    def test_index_skips_valid_json_that_is_not_an_object(self):
        event_root = self.root / 'results' / 'helmet-events' / '2026' / '11' / '24'
        event_root.mkdir(parents=True)
        (event_root / 'array.json').write_text('[]', encoding='utf-8')
        store = self.store()
        self.addCleanup(store.close)
        status = store.status()
        self.assertEqual(status['corrupt_records'], 1)
        self.assertEqual(store.list_events(), [])

    def test_closed_store_rejects_new_publication(self):
        store = self.store()
        store.close()
        with self.assertRaises(RuntimeError):
            self.publish(store)


if __name__ == '__main__':
    unittest.main()
