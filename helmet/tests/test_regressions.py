"""Run on the RK3568 with Python unittest; no extra test framework required."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HELMET_DIR = Path(__file__).resolve().parents[1]


class CLIRegressions(unittest.TestCase):
    def test_realtime_help_starts(self):
        result = subprocess.run(
            [sys.executable, str(HELMET_DIR / 'helmet_realtime.py'), '--help'],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--source', result.stdout)

    @unittest.skipUnless(os.environ.get('RK3568_TEST_MODEL'),
                         'set RK3568_TEST_MODEL to run the NPU integration test')
    def test_corrupt_source_is_rejected_within_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'broken.jpg').write_bytes(b'not an image')
            try:
                result = subprocess.run(
                    [sys.executable, str(HELMET_DIR / 'helmet_realtime.py'),
                     '--model', os.environ['RK3568_TEST_MODEL'],
                     '--source', folder, '--no-display', '--duration', '0.2'],
                    capture_output=True, text=True, timeout=5,
                )
            except subprocess.TimeoutExpired:
                self.fail('corrupt input validation did not stop within deadline')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Pipeline error', result.stderr)

    def evaluate_fixture(self, detections):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            annotations = {
                'images': [{'id': 1, 'file_name': 'a.jpg'},
                           {'id': 2, 'file_name': 'b.jpg'}],
                'categories': [{'id': 1, 'name': 'hardhat'}],
                'annotations': [
                    {'image_id': 1, 'category_id': 1, 'bbox': [10, 10, 20, 20]},
                    {'image_id': 2, 'category_id': 1, 'bbox': [10, 10, 20, 20]},
                ],
            }
            (root / '_annotations.coco.json').write_text(json.dumps(annotations))
            dets_path = root / 'dets.json'
            dets_path.write_text(json.dumps(detections))
            metrics_path = root / 'metrics.json'
            saved_path = root / 'saved.json'
            result = subprocess.run(
                [sys.executable, str(HELMET_DIR / 'pc' / 'eval_map.py'),
                 '--data', str(root), '--dets', str(dets_path),
                 '--out', str(metrics_path), '--save-dets', str(saved_path)],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return (json.loads(metrics_path.read_text()),
                    json.loads(saved_path.read_text()))

    def test_missing_detection_counts_as_missed_object(self):
        metrics, saved = self.evaluate_fixture(
            {'a.jpg': [[1, 0.9, 10, 10, 30, 30]]},
        )
        self.assertEqual(metrics['images'], 2)
        self.assertEqual(metrics['hat']['npos'], 2)
        self.assertAlmostEqual(metrics['hat']['recall'], 0.5)
        self.assertAlmostEqual(metrics['hat']['ap50'], 0.5)
        self.assertEqual(saved['b.jpg'], [])

    def test_empty_detections_keep_all_test_images(self):
        metrics, saved = self.evaluate_fixture({})
        self.assertEqual(metrics['images'], 2)
        self.assertEqual(metrics['hat']['npos'], 2)
        self.assertEqual(metrics['hat']['recall'], 0.0)
        self.assertEqual(saved, {'a.jpg': [], 'b.jpg': []})


if __name__ == '__main__':
    unittest.main()
