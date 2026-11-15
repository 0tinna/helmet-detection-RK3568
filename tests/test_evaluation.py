"""Trustworthy metrics: independently hand-calculated matches and rejections."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'helmet' / 'pc'))


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.ev = importlib.import_module('eval_map')

    def test_unknown_only_dataset_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, '_annotations.coco.json').write_text(json.dumps({
                'images': [{'id': 1, 'file_name': 'a.jpg', 'width': 100, 'height': 100}],
                'categories': [{'id': 7, 'name': 'car'}],
                'annotations': [{'id': 1, 'image_id': 1, 'category_id': 7, 'bbox': [0, 0, 10, 10]}]}))
            with self.assertRaises(ValueError):
                self.ev.load_gt(folder)

    def test_missing_predictions_reduce_recall_and_absent_class_is_undefined(self):
        gt = {1: [(1, 0, 0, 10, 10)], 2: [(1, 0, 0, 10, 10)]}
        result = self.ev.evaluate({1: [(1, .9, 0, 0, 10, 10)]}, gt)
        self.assertEqual(result['hat']['recall'], .5)
        self.assertEqual(result['hat']['ap50'], .5)
        self.assertIsNone(result['person']['ap50'])
        self.assertEqual(result['mAP50'], .5)

    def test_invalid_detections_are_errors(self):
        gt = {1: [(1, 0, 0, 10, 10)]}
        for row in [(2, .9, 0, 0, 10, 10), (1, float('nan'), 0, 0, 10, 10),
                    (1, 1.2, 0, 0, 10, 10), (1, .9, 10, 0, 0, 10), (1, .9, 0, 0, 10),
                    (1, .9, 0, 0, 1e308, 1e308)]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.ev.evaluate({1: [row]}, gt)
        with self.assertRaises(ValueError):
            self.ev.evaluate({2: []}, gt)

    def test_area_metrics_ignore_matching_large_gt_and_unmatched_large_predictions(self):
        gt = {1: [(1, 0, 0, 10, 10), (1, 100, 100, 200, 200)]}
        dets = {1: [(1, .99, 100, 100, 200, 200), (1, .95, 300, 300, 400, 400),
                    (1, .9, 0, 0, 10, 10)]}
        self.assertTrue(callable(getattr(self.ev, 'evaluate_areas', None)), 'area evaluation missing')
        result = self.ev.evaluate_areas(dets, gt)
        self.assertEqual(result['small']['hat']['npos'], 1)
        self.assertEqual(result['small']['hat']['ap50'], 1)
        self.assertEqual(result['small']['hat']['false_positives'], 0)
        self.assertEqual(result['large']['hat']['false_positives'], 1)

    def test_matching_chooses_available_gt_instead_of_already_used_best(self):
        gt = {1: [(1, 0, 0, 10, 10), (1, 2, 0, 12, 10)]}
        result = self.ev.evaluate({1: [(1, .99, 0, 0, 10, 10), (1, .9, 1, 0, 11, 10)]}, gt)
        self.assertEqual(result['hat']['recall'], 1)

    def test_failed_or_incomplete_dump_and_unknown_image_rejected(self):
        self.assertTrue(callable(getattr(self.ev, 'load_detections', None)), 'dump validation missing')
        images = [{'id': 1, 'file_name': 'a.jpg', 'width': 100, 'height': 100}]
        with self.assertRaises(ValueError):
            self.ev.load_detections({'schema_version': 1, 'status': 'failed', 'detections': {}}, images)
        with self.assertRaises(ValueError):
            self.ev.load_detections({'schema_version': 1, 'status': 'complete', 'detections': {}}, images)
        with self.assertRaises(ValueError):
            self.ev.load_detections({'unknown.jpg': []}, images)
        self.assertEqual(self.ev.load_detections({}, images), {1: []})

    def test_dataset_rejects_duplicate_ids_crowd_and_bad_geometry(self):
        base = {'images': [{'id': 1, 'file_name': 'a.jpg', 'width': 100, 'height': 100}],
                'categories': [{'id': 1, 'name': 'hardhat'}],
                'annotations': [{'id': 1, 'image_id': 1, 'category_id': 1, 'bbox': [0, 0, 10, 10]}]}
        variants = []
        duplicate = json.loads(json.dumps(base))
        duplicate['images'].append(dict(duplicate['images'][0]))
        variants.append(duplicate)
        for field, value in [('iscrowd', 1), ('ignore', 1), ('bbox', [0, 0, -1, 10]),
                             ('bbox', [95, 0, 10, 10]), ('category_id', 2), ('image_id', 2)]:
            item = json.loads(json.dumps(base))
            item['annotations'][0][field] = value
            variants.append(item)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, '_annotations.coco.json')
            for item in variants:
                path.write_text(json.dumps(item))
                with self.subTest(item=item), self.assertRaises(ValueError):
                    self.ev.load_gt(folder)

    def test_backend_exclusivity_fails_before_optional_library_import(self):
        import subprocess
        result = subprocess.run([sys.executable, str(Path(self.ev.__file__)),
                                 '--data', 'missing', '--onnx', 'm.onnx', '--dets', 'd.json'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_complete_dump_without_hash_evidence_is_rejected(self):
        images = [{'id': 1, 'file_name': 'a.jpg', 'width': 100, 'height': 100}]
        dump = {'schema_version': 1, 'status': 'complete', 'errors': [],
                'inputs': [{'file_name': 'a.jpg', 'status': 'ok'}], 'detections': {'a.jpg': []}}
        with self.assertRaises(ValueError):
            self.ev.load_detections(dump, images)

    def test_duplicate_json_detection_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'dets.json')
            path.write_text('{"a.jpg": [], "a.jpg": []}')
            with self.assertRaises(ValueError):
                self.ev.load_detections(path, [{'id': 1, 'file_name': 'a.jpg'}])

    def test_malformed_category_is_validation_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, '_annotations.coco.json')
            path.write_text(json.dumps({'images': [{'id': 1, 'file_name': 'a.jpg'}],
                                        'categories': [[]], 'annotations': []}))
            with self.assertRaises(ValueError):
                self.ev.load_gt(folder)

    def test_annotation_filename_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            child = root / 'child'
            child.mkdir()
            path = root / 'valid.json'
            path.write_text(json.dumps({'images': [{'id': 1, 'file_name': 'a.jpg'}],
                                        'categories': [{'id': 1, 'name': 'hardhat'}],
                                        'annotations': [{'image_id': 1, 'category_id': 1, 'bbox': [0, 0, 10, 10]}]}))
            with self.assertRaises(ValueError):
                self.ev.load_dataset(child, annotation_file='../valid.json')

    def test_package_imports_need_no_caller_path_mutation(self):
        import subprocess
        result = subprocess.run([sys.executable, '-c',
                                 'import helmet.pc.eval_map; import helmet.pc.analyze_errors; import helmet.dump_dets'],
                                capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
