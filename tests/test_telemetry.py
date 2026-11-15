"""Bounded live statistics and exact explicitly logged experiment summaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from helmet.telemetry import Telemetry, TIMINGS


class TelemetryRetentionTests(unittest.TestCase):
    @staticmethod
    def record(telemetry, index, value):
        stamp = index * .1
        telemetry.increment('captured', stamp)
        telemetry.increment('detected', stamp + .01)
        telemetry.increment('output', stamp + .02)
        telemetry.record_detection({'processed_at': stamp + .01, 'prep_ms': value,
                                    'infer_ms': value, 'post_ms': value})
        telemetry.record({'output_at': stamp + .02, 'capture_to_output_ms': value,
                          'sink_ms': value, 'capture_to_sink_complete_ms': value})
        telemetry.add_sample({'monotonic': stamp, 'cpu_percent': 12, 'rss_bytes': 1000000})

    def test_default_100000_rows_keep_bounded_samples_and_exact_counts_mean(self):
        telemetry = Telemetry()
        telemetry.started = 0
        try:
            for index in range(100000):
                self.record(telemetry, index, float(index))
            summary = telemetry.summary(now=10000)
            self.assertLessEqual(sum(len(values) for values in telemetry.values.values()), 6 * 2048)
            self.assertLessEqual(len(telemetry.runtime_samples), 1024)
            self.assertEqual(summary['counters']['captured'], 100000)
            self.assertEqual(summary['counters']['detected'], 100000)
            self.assertEqual(summary['counters']['output'], 100000)
            for name in TIMINGS:
                result = summary['full_session'][name]
                self.assertEqual(result['count'], 100000)
                self.assertEqual(result['mean'], 49999.5)
                self.assertEqual(result['sample_count'], 2048)
                self.assertEqual(result['percentile_method'], 'uniform_reservoir_linear')
                self.assertEqual(result['mean_method'], 'all_samples')
            retention = summary['runtime_sample_retention']
            self.assertEqual(retention['total_count'], 100000)
            self.assertEqual(retention['retained_count'], 1024)
            self.assertEqual(retention['method'], 'latest_samples')
            self.assertIsNone(retention['raw_log'])
        finally:
            telemetry.close()

    def test_explicit_jsonl_or_session_spools_exact_quantiles_with_bounded_live_samples(self):
        values = [(index % 101) ** 3 for index in range(4099)]
        for kind in ('jsonl', 'session_json'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / ('frames.jsonl' if kind == 'jsonl' else 'session.json')
                telemetry = Telemetry(**{kind: str(target)})
                telemetry.started = 0
                try:
                    for index, value in enumerate(values):
                        self.record(telemetry, index, value)
                    self.assertLessEqual(sum(len(row) for row in telemetry.values.values()), 6 * 2048)
                    self.assertLessEqual(len(telemetry.runtime_samples), 1024)
                    summary = telemetry.summary(now=410)
                    expected = np.percentile(values, [50, 95, 99])
                    for name in TIMINGS:
                        result = summary['full_session'][name]
                        self.assertEqual(result['count'], len(values))
                        self.assertEqual(result['sample_count'], len(values))
                        self.assertEqual(result['percentile_method'], 'exact_linear_disk_spool')
                        np.testing.assert_allclose([result[key] for key in ('p50', 'p95', 'p99')], expected)
                    retention = summary['runtime_sample_retention']
                    self.assertEqual(retention['total_count'], len(values))
                    self.assertEqual(retention['retained_count'], 1024)
                    raw_resources = Path(retention['raw_log'])
                    resources = [json.loads(line) for line in raw_resources.read_text().splitlines()]
                    self.assertEqual(len(resources), len(values))
                    raw_timings = Path(summary['timing_retention']['raw_log'])
                    rows = [json.loads(line) for line in raw_timings.read_text().splitlines()]
                    self.assertEqual(len(rows), 2 * len(values))
                finally:
                    telemetry.close()

    def test_exact_summary_can_append_after_sort_and_survive_idempotent_close(self):
        with tempfile.TemporaryDirectory() as folder:
            telemetry = Telemetry(jsonl=str(Path(folder) / 'frames.jsonl'))
            for index, value in enumerate((3, 1, 4)):
                self.record(telemetry, index, value)
            self.assertEqual(telemetry.summary()['full_session']['infer_ms']['p50'], 3)
            self.record(telemetry, 3, 2)
            result = telemetry.summary()['full_session']['infer_ms']
            self.assertEqual(result['count'], 4)
            self.assertEqual(result['p50'], 2.5)
            telemetry.close()
            telemetry.close()
            self.assertEqual(telemetry.summary()['full_session']['infer_ms'], result)

    def test_spool_summary_failure_still_closes_all_owned_files(self):
        with tempfile.TemporaryDirectory() as folder:
            telemetry = Telemetry(jsonl=str(Path(folder) / 'frames.jsonl'))
            self.record(telemetry, 0, 1)
            owned = [telemetry.stream, telemetry.resource_stream, *telemetry.spools.values()]
            try:
                with patch.object(telemetry, '_timing_summary', side_effect=OSError('spool read failed')):
                    with self.assertRaisesRegex(OSError, 'spool read failed'):
                        telemetry.close()
                self.assertTrue(all(stream.closed for stream in owned))
            finally:
                for stream in owned:
                    if not stream.closed:
                        stream.close()


if __name__ == '__main__':
    unittest.main()
