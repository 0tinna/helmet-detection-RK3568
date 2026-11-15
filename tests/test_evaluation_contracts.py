"""Adversarial real-evaluation contracts from independent review R1-R3."""
import copy
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PC_DIR = Path(__file__).resolve().parents[1] / 'helmet' / 'pc'
sys.path.insert(0, str(PC_DIR))


def complete_dump():
    return {'schema_version': 1, 'status': 'complete', 'created_utc': '2026-11-14T00:00:00+00:00',
            'model': {'name': 'm.rknn', 'sha256': 'a' * 64},
            'config': {'class_order': ['person', 'hat'], 'size': [32, 32], 'conf_min': .001,
                       'op_conf': .25, 'nms_iou': .45, 'max_det': 300,
                       'input_layout': 'NHWC RGB uint8 batch=1'},
            'runtime': {'python': '3.10.22', 'numpy': '1.26.4', 'opencv': '4.10.0'},
            'inputs': [{'id': 1, 'file_name': 'a.png', 'dimensions': [100, 100], 'status': 'ok',
                        'sha256': 'b' * 64, 'inference_ms': 1., 'detections': 1}],
            'counts': {'requested': 1, 'completed': 1, 'errors': 0}, 'errors': [],
            'detections': {'a.png': [[1, .9, 0, 0, 10, 10]]}}


def source_metrics(source='reference'):
    sizes = [[416, 416], [512, 288], [512, 512], [640, 384], [640, 640], [640, 640]]
    metric = .9 if source == 'reference' else .8
    count = 10 if source == 'reference' else 12
    output = {}
    for i, size in enumerate(sizes):
        descriptor = complete_dump()['config']
        descriptor['size'] = size
        descriptor['clipped_box_policy'] = 'discard-finite-zero-area-after-shared-postprocess-with-trace'
        descriptor['postprocess_sha256'] = 'f' * 64
        output['m%d' % i] = {
            'images': count, 'mAP50': metric, 'classes_with_gt': ['person', 'hat'],
            'person': {'npos': count, 'ap50': metric, 'precision': metric, 'recall': metric, 'f1': metric},
            'hat': {'npos': count, 'ap50': metric, 'precision': metric, 'recall': metric, 'f1': metric},
            'model': {'name': 'm%d.rknn' % i, 'sha256': '%064x' % (i + 1)},
            'dataset': {'annotation_sha256': ('c' if source == 'reference' else 'd') * 64,
                        'manifest_sha256': ('7' if source == 'reference' else '8') * 64, 'total_images': count},
            'config': {'op_conf': .25, 'iou': .5, 'conf_min': .001, 'nms_iou': .45, 'limit': 0},
            'inference': {'status': 'complete', 'backend': 'dets', 'dump_config': descriptor,
                          'detections_sha256': '9' * 64, 'dataset_identity_verified': True,
                          'image_hashes_verified': True},
            'ap_coverage': {'status': 'project-floor', 'confidence_floor': .001,
                            'project_confidence_floor': .001, 'comparable_project_ap': True,
                            'full_score_range': False},
            'metric_definition': {'metric_version': 'helmet-all-point-ap50-v1',
                                  'ap50': 'Project all-point AP50 at retained-score floor .001',
                                  'iou': .5, 'interpolation': 'all-point', 'gt_matching': 'highest-IoU available same-class GT',
                                  'class_mean': 'mean over classes with GT; absent classes null',
                                  'coco_ap50_95': None, 'area_rules': 'small <1024; medium <9216; large >=9216'}}
    return output


class DumpContractTests(unittest.TestCase):
    def setUp(self):
        self.ev = importlib.import_module('eval_map')
        self.images = [{'id': 1, 'file_name': 'a.png', 'width': 100, 'height': 100}]

    def test_complete_dump_validates_per_image_identity_dimensions_counts_and_config(self):
        self.assertEqual(len(self.ev.load_detections(complete_dump(), self.images)[1]), 1)
        cases = [('inputs', 'id', 999), ('inputs', 'dimensions', [1, 1]),
                 ('inputs', 'detections', 999), ('inputs', 'inference_ms', float('nan')),
                 ('counts', 'requested', 999), ('counts', 'completed', 0), ('counts', 'errors', 50),
                 ('config', 'size', [31, 32]), ('config', 'op_conf', .0001),
                 ('config', 'max_det', 0), ('config', 'input_layout', 'NCHW float')]
        for section, field, value in cases:
            dump = complete_dump()
            target = dump[section][0] if section == 'inputs' else dump[section]
            target[field] = value
            with self.subTest(section=section, field=field), self.assertRaises(ValueError):
                self.ev.load_detections(dump, self.images)
        dump = complete_dump()
        dump['detections']['a.png'][0][1] = .0009
        with self.assertRaises(ValueError):
            self.ev.load_detections(dump, self.images)

    def test_dataset_identity_cannot_be_rebound_to_different_annotations(self):
        self.assertIn('dataset', inspect.signature(self.ev.load_detections).parameters)
        dump = complete_dump()
        dump['dataset'] = {'annotation_sha256': 'c' * 64, 'manifest_sha256': 'd' * 64}
        expected = {'annotation_sha256': 'c' * 64, 'manifest_sha256': 'd' * 64}
        self.assertEqual(len(self.ev.load_detections(dump, self.images, dataset=expected)[1]), 1)
        for field in ('annotation_sha256', 'manifest_sha256'):
            changed = dict(expected)
            changed[field] = 'e' * 64
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.ev.load_detections(dump, self.images, dataset=changed)
        dump['dataset'] = {}
        with self.assertRaises(ValueError):
            self.ev.load_detections(dump, self.images, dataset={})

    def test_discarded_trace_cannot_exceed_total_raw_max_det(self):
        dump = complete_dump()
        dump['config'].update(max_det=1, clipped_box_policy='discard-finite-zero-area-after-shared-postprocess-with-trace')
        dump['inputs'][0].update(discarded_clipped_count=1,
                                 discarded_clipped_boxes=[[0, .002, 20, 0, 30, 0]],
                                 discarded_clipped_reason='zero_area_after_shared_source_clipping_or_postprocess')
        dump['counts']['discarded_clipped_count'] = 1
        with self.assertRaises(ValueError):
            self.ev.load_detections(dump, self.images)


class CoverageContractTests(unittest.TestCase):
    def make_cli_fixture(self, root):
        import cv2
        import numpy as np
        cv2.imwrite(str(root / 'a.png'), np.zeros((100, 100, 3), np.uint8))
        coco = {'images': [{'id': 1, 'file_name': 'a.png', 'width': 100, 'height': 100}],
                'categories': [{'id': 1, 'name': 'hardhat'}],
                'annotations': [{'id': 1, 'image_id': 1, 'category_id': 1, 'bbox': [0, 0, 10, 10]}]}
        path = root / '_annotations.coco.json'
        path.write_text(json.dumps(coco))
        dump = complete_dump()
        dump['config']['conf_min'] = .2
        dump['detections']['a.png'] = []
        dump['inputs'][0]['detections'] = 0
        dump['inputs'][0]['sha256'] = hashlib.sha256((root / 'a.png').read_bytes()).hexdigest()
        dump['dataset'] = {'annotation_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        (root / 'dets.json').write_text(json.dumps(dump))

    def test_error_cli_rejects_confidence_below_dump_floor_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_cli_fixture(root)
            result = subprocess.run([sys.executable, str(PC_DIR / 'analyze_errors.py'), 'errors',
                                     '--data', str(root), '--dets', str(root / 'dets.json'),
                                     '--confidence', '.1', '--count', '0', '--out', str(root / 'out')],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertFalse((root / 'out').exists())

    def test_high_floor_ap_is_explicitly_truncated_and_cannot_be_project_comparable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_cli_fixture(root)
            result = subprocess.run([sys.executable, str(PC_DIR / 'eval_map.py'), '--data', str(root),
                                     '--dets', str(root / 'dets.json'), '--out', str(root / 'metrics.json')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            metrics = json.loads((root / 'metrics.json').read_text())
            self.assertIn('ap_coverage', metrics)
            self.assertEqual(metrics['ap_coverage']['status'], 'truncated')
            self.assertFalse(metrics['ap_coverage']['comparable_project_ap'])
            self.assertIn('truncated', metrics['metric_definition']['ap50'])
            self.assertIsNone(metrics['accuracy_target_pass'])


class ComparisonContractTests(unittest.TestCase):
    def setUp(self):
        self.an = importlib.import_module('analyze_errors')

    def test_paired_model_hashes_definitions_configs_coverage_and_distinct_artifacts_required(self):
        cases = ['paired-hash', 'duplicate-artifacts', 'cross-source-op', 'paired-size',
                 'definition', 'missing-coverage', 'decoder-hash', 'class-coverage']
        for case in cases:
            reference, external = source_metrics(), source_metrics('external')
            if case == 'paired-hash':
                external['m0']['model']['sha256'] = 'e' * 64
            elif case == 'duplicate-artifacts':
                for source in (reference, external):
                    for row in source.values(): row['model']['sha256'] = 'a' * 64
            elif case == 'cross-source-op':
                for row in external.values():
                    row['config']['op_conf'] = .75
                    row['inference']['dump_config']['op_conf'] = .75
            elif case == 'paired-size':
                external['m0']['inference']['dump_config']['size'] = [512, 512]
            elif case == 'definition':
                for row in external.values(): row['metric_definition']['gt_matching'] = 'different matcher'
            elif case == 'missing-coverage':
                for source in (reference, external):
                    for row in source.values(): del row['classes_with_gt']
            elif case == 'decoder-hash':
                for row in external.values(): row['inference']['dump_config']['postprocess_sha256'] = 'e' * 64
            elif case == 'class-coverage':
                for row in external.values(): row['classes_with_gt'] = ['hat']
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.an.compare_models(reference, external)

    def test_different_sizes_between_models_are_allowed_when_each_pair_matches(self):
        result = self.an.compare_models(source_metrics(), source_metrics('external'))
        self.assertEqual(result.get('verification_status'), 'verified-pairs')
        self.assertAlmostEqual(result['models']['m0']['mAP50_delta'], -.1)
        self.assertIsNone(result['accuracy_target_pass'])

    def test_minimal_legacy_metrics_never_pass_even_with_declared_independence(self):
        legacy = {'m%d' % i: {'mAP50': .9, 'images': 10} for i in range(6)}
        result = self.an.compare_models(legacy, legacy, independence='verified', independence_evidence='source audit')
        self.assertIsNone(result['accuracy_target_pass'])
        self.assertEqual(result.get('verification_status'), 'unverified-legacy')

    def test_matching_high_floor_runs_remain_truncated_and_do_not_pass_project_target(self):
        reference, external = source_metrics(), source_metrics('external')
        for source in (reference, external):
            for row in source.values():
                row['config']['conf_min'] = .2
                row['inference']['dump_config']['conf_min'] = .2
                row['ap_coverage'].update(status='truncated', confidence_floor=.2, comparable_project_ap=False)
                row['metric_definition']['ap50'] = 'Confidence-truncated all-point AP50 at score floor .2'
                row['mAP50'] = .9
                row['person']['ap50'] = row['hat']['ap50'] = .9
        result = self.an.compare_models(reference, external, independence='verified', independence_evidence='source audit')
        self.assertIsNone(result['accuracy_target_pass'])


if __name__ == '__main__':
    unittest.main()
