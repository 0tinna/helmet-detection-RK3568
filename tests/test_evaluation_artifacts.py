"""Error selections and six-model comparison preserve evidence limitations."""
import importlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'helmet' / 'pc'))


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('analyze_errors'), 'error artifacts missing')
        self.an = importlib.import_module('analyze_errors')

    def test_matching_errors_exposes_tp_fp_fn_and_confidence(self):
        gt = {1: [(1, 0, 0, 10, 10), (0, 20, 20, 30, 30)]}
        dets = {1: [(1, .9, 0, 0, 10, 10), (1, .8, 40, 40, 50, 50),
                    (0, .1, 20, 20, 30, 30)]}
        result = self.an.match_errors(dets, gt, confidence=.25, iou=.5)
        self.assertEqual(len(result[1]['tp']), 1)
        self.assertEqual(len(result[1]['fp']), 1)
        self.assertEqual(len(result[1]['fn']), 1)
        self.assertEqual(result[1]['fn'][0][0], 0)

    def test_selection_is_reproducible_in_input_order(self):
        rows = [{'id': i, 'file_name': f'{i}.jpg'} for i in range(20)]
        first = self.an.select_images(rows, count=4, seed='fixed')
        self.assertEqual(first, self.an.select_images(list(reversed(rows)), count=4, seed='fixed'))

    def test_six_model_comparison_has_cross_source_deltas_and_no_uncertain_pass(self):
        reference = {f'm{i}': {'mAP50': .9, 'images': 10} for i in range(6)}
        external = {f'm{i}': {'mAP50': .8, 'images': 12} for i in range(6)}
        result = self.an.compare_models(reference, external, independence='unknown')
        self.assertIsNone(result['accuracy_target_pass'])
        self.assertAlmostEqual(result['models']['m0']['mAP50_delta'], -.1)
        with self.assertRaises(ValueError):
            self.an.compare_models(dict(list(reference.items())[:5]), external)

    def test_comparison_emits_class_deltas_without_filling_absent_classes(self):
        reference = {f'm{i}': {'mAP50': .9, 'images': 10, 'hat': {'ap50': .9, 'npos': 10},
                               'person': {'ap50': None, 'npos': 0}} for i in range(6)}
        external = {f'm{i}': {'mAP50': .8, 'images': 12, 'hat': {'ap50': .8, 'npos': 12},
                              'person': {'ap50': None, 'npos': 0}} for i in range(6)}
        result = self.an.compare_models(reference, external)
        self.assertIn('classes', result['models']['m0'])
        self.assertAlmostEqual(result['models']['m0']['classes']['hat']['ap50_delta'], -.1)
        self.assertIsNone(result['models']['m0']['classes']['person']['ap50_delta'])

    def test_verified_independence_needs_evidence(self):
        metrics = {f'm{i}': {'mAP50': .9, 'images': 10} for i in range(6)}
        with self.assertRaises(ValueError):
            self.an.compare_models(metrics, metrics, independence='verified')

    def test_actual_images_use_match_colors_and_summary_contains_plot_hashes(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cv2.imwrite(str(root / 'a.png'), np.full((240, 320, 3), 40, np.uint8))
            images = [{'id': 1, 'file_name': 'a.png', 'width': 320, 'height': 240}]
            gt = {1: [(1, 30, 60, 80, 110), (0, 30, 160, 80, 210)]}
            dets = {1: [(1, .9, 30, 60, 80, 110), (1, .8, 200, 60, 250, 110)]}
            result = self.an.create_error_artifacts(root, images, dets, gt, root / 'out', count=1)
            self.assertEqual(result['counts'], {'tp': 1, 'fp': 1, 'fn': 1})
            actual = cv2.imread(str(root / 'out' / result['selected'][0]['artifact']))
            self.assertEqual(actual[70, 30].tolist(), [0, 200, 0])
            self.assertEqual(actual[70, 200].tolist(), [0, 220, 255])
            self.assertEqual(actual[170, 30].tolist(), [0, 0, 255])
            self.assertIn('artifacts', result)
            self.assertEqual(len(result['artifacts']['area_metrics.png']), 64)


if __name__ == '__main__':
    unittest.main()
