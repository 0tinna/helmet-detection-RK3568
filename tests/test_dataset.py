"""Dataset preparation must preserve geometry and block group/hash leakage."""
import importlib
import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'helmet' / 'pc'))


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('dataset_tools'), 'dataset preparation missing')
        self.ds = importlib.import_module('dataset_tools')

    def test_voc_uses_one_based_inclusive_to_zero_based_half_open(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Image decoder is intentionally not needed by the converter geometry API.
            (root / 'a.xml').write_text('<annotation><filename>a.jpg</filename><size><width>100</width><height>80</height></size><object><name>hat</name><bndbox><xmin>1</xmin><ymin>2</ymin><xmax>10</xmax><ymax>20</ymax></bndbox></object></annotation>')
            result = self.ds.voc_to_coco([root / 'a.xml'], class_map={'hat': 1}, coordinate_mode='voc-inclusive')
            self.assertEqual(result['images'][0]['file_name'], 'a.jpg')
            self.assertEqual(result['annotations'][0]['bbox'], [0., 1., 10., 19.])
            self.assertEqual(result['annotations'][0]['area'], 190.)

    def test_voc_rejects_bad_paths_unknown_labels_and_invalid_boxes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'a.xml')
            for name, filename, box in [('car', 'a.jpg', '1 1 10 10'),
                                        ('hat', '../a.jpg', '1 1 10 10'),
                                        ('hat', 'a.jpg', '10 1 2 10')]:
                xmin, ymin, xmax, ymax = box.split()
                path.write_text(f'<annotation><filename>{filename}</filename><size><width>100</width><height>80</height></size><object><name>{name}</name><bndbox><xmin>{xmin}</xmin><ymin>{ymin}</ymin><xmax>{xmax}</xmax><ymax>{ymax}</ymax></bndbox></object></annotation>')
                with self.subTest(name=name, filename=filename), self.assertRaises(ValueError):
                    self.ds.voc_to_coco([path], class_map={'hat': 1})

    def test_scene_split_is_deterministic_and_duplicate_hashes_share_partition(self):
        records = [{'file_name': 'scene1/a.jpg', 'group': 'scene1', 'image_sha256': 'same'},
                   {'file_name': 'scene1/b.jpg', 'group': 'scene1', 'image_sha256': 'b'},
                   {'file_name': 'scene2/a.jpg', 'group': 'scene2', 'image_sha256': 'same'},
                   {'file_name': 'scene3/a.jpg', 'group': 'scene3', 'image_sha256': 'c'}]
        first = self.ds.split_records(records, eval_fraction=.5, seed='fixed')
        second = self.ds.split_records(list(reversed(records)), eval_fraction=.5, seed='fixed')
        self.assertEqual(first, second)
        self.assertEqual(first['scene1/a.jpg'], first['scene1/b.jpg'])
        self.assertEqual(first['scene1/a.jpg'], first['scene2/a.jpg'])
        with self.assertRaises(ValueError):
            self.ds.split_records([{'file_name': 'a.jpg', 'image_sha256': 'a'}])

    def test_manifest_records_unknown_independence_hashes_and_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'a.jpg').write_bytes(b'image fixture')
            coco = {'images': [{'id': 1, 'file_name': 'a.jpg', 'width': 100, 'height': 80}],
                    'categories': [{'id': 1, 'name': 'hardhat'}],
                    'annotations': [{'id': 1, 'image_id': 1, 'category_id': 1, 'bbox': [0, 0, 10, 10]}]}
            result = self.ds.create_manifest(root, coco, {'source_url': 'https://example.org/raw',
                                                        'license': 'unknown', 'origin': 'external', 'independence': 'unknown'},
                                             {'a.jpg': 'scene-a'})
            self.assertEqual(result['provenance']['independence'], 'unknown')
            self.assertEqual(result['counts']['images'], 1)
            self.assertEqual(result['counts']['annotations'], 1)
            self.assertEqual(len(result['records'][0]['image_sha256']), 64)
            self.assertEqual(len(result['records'][0]['annotation_sha256']), 64)

    def test_full_body_labels_require_explicit_ignore_and_remain_in_raw_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'a.xml')
            path.write_text('<annotation><filename>a.jpg</filename><size><width>100</width><height>80</height></size><object><name>head</name><bndbox><xmin>1</xmin><ymin>1</ymin><xmax>10</xmax><ymax>10</ymax></bndbox></object><object><name>person</name><bndbox><xmin>1</xmin><ymin>1</ymin><xmax>50</xmax><ymax>70</ymax></bndbox></object></annotation>')
            with self.assertRaises(ValueError):
                self.ds.voc_to_coco([path], {'head': 0})
            import inspect
            self.assertIn('ignored_labels', inspect.signature(self.ds.voc_to_coco).parameters)
            result = self.ds.voc_to_coco([path], {'head': 0}, ignored_labels=['person'])
            self.assertEqual(len(result['annotations']), 1)
            self.assertEqual(result['info']['ignored_label_counts'], {'person': 1})
            self.assertEqual(result['info']['raw_object_count'], 2)

    def test_manifest_preserves_ignored_object_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'a.jpg').write_bytes(b'fixture')
            coco = {'images': [{'id': 1, 'file_name': 'a.jpg', 'width': 100, 'height': 80}],
                    'categories': [{'id': 1, 'name': 'hardhat'}],
                    'annotations': [{'id': 1, 'image_id': 1, 'category_id': 1, 'bbox': [0, 0, 10, 10]}],
                    'info': {'raw_object_count': 3, 'ignored_label_counts': {'person': 2}}}
            result = self.ds.create_manifest(root, coco, {'source_url': 'raw', 'license': 'unknown',
                                                         'origin': 'external', 'independence': 'unknown'},
                                             {'a.jpg': 'scene'})
            self.assertIn('raw_objects', result['counts'])
            self.assertEqual(result['counts']['raw_objects'], 3)
            self.assertEqual(result['counts']['ignored_labels'], {'person': 2})

    def test_real_shwd_difficult_object_can_be_included_only_by_explicit_policy(self):
        # Original selected SHWD 000022.xml: difficult hat, 690x518, source
        # extrema (640,105,660,128). This reproduces the real conversion block.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, '000022.xml')
            path.write_text('<annotation><filename>000022.jpg</filename><size><width>690</width><height>518</height></size><object><name>hat</name><difficult>1</difficult><bndbox><xmin>640</xmin><ymin>105</ymin><xmax>660</xmax><ymax>128</ymax></bndbox></object></annotation>')
            with self.assertRaises(ValueError):
                self.ds.voc_to_coco([path], {'hat': 1})
            import inspect
            self.assertIn('difficult_policy', inspect.signature(self.ds.voc_to_coco).parameters)
            result = self.ds.voc_to_coco([path], {'hat': 1}, difficult_policy='include')
            self.assertEqual(result['annotations'][0]['bbox'], [639., 104., 21., 24.])
            self.assertEqual(result['annotations'][0]['source_difficult'], 1)
            self.assertEqual(result['info']['difficult_label_counts'], {'hat': 1})
            self.assertEqual(result['info']['evaluation_rules']['difficult_policy'], 'include-as-normal')
            images, gt, metadata = self.ds.validate_coco(result)
            self.assertEqual(len(gt[1]), 1)
            self.assertEqual(metadata['evaluation_rules']['difficult_policy'], 'include-as-normal')

    def test_real_shwd_extension_mismatch_resolves_explicitly_without_losing_raw_names(self):
        import cv2
        import inspect
        import numpy as np
        # Exact source case: 000034.xml declares .jpeg; original ZIP image .jpg.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / '000034.xml'
            path.write_text('<annotation><filename>000034.jpeg</filename><size><width>750</width><height>711</height></size><object><name>hat</name><difficult>0</difficult><bndbox><xmin>139</xmin><ymin>373</ymin><xmax>197</xmax><ymax>435</ymax></bndbox></object><object><name>hat</name><difficult>1</difficult><bndbox><xmin>334</xmin><ymin>45</ymin><xmax>360</xmax><ymax>78</ymax></bndbox></object><object><name>hat</name><difficult>0</difficult><bndbox><xmin>653</xmin><ymin>369</ymin><xmax>722</xmax><ymax>440</ymax></bndbox></object></annotation>')
            cv2.imwrite(str(root / '000034.jpg'), np.zeros((711, 750, 3), np.uint8))
            self.assertIn('image_root', inspect.signature(self.ds.voc_to_coco).parameters)
            with self.assertRaises(ValueError):
                self.ds.voc_to_coco([path], {'hat': 1}, difficult_policy='include', image_root=root)
            result = self.ds.voc_to_coco([path], {'hat': 1}, difficult_policy='include',
                                         image_root=root, filename_policy='unique-stem')
            self.assertEqual(len(result['annotations']), 3)
            im = result['images'][0]
            self.assertEqual(im['file_name'], '000034.jpg')
            self.assertEqual(im['original_filename'], '000034.jpeg')
            self.assertEqual(im['source_filename'], '000034.jpg')
            self.assertEqual(im['source_image_sha256'], hashlib.sha256((root / '000034.jpg').read_bytes()).hexdigest())
            self.assertEqual(result['info']['raw_annotations'][0]['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(result['info']['raw_annotations'][0]['original_filename'], '000034.jpeg')
            self.ds.verify_images(root, result['images'])
            manifest = self.ds.create_manifest(root, result, {'source_url': 'original ZIP', 'license': 'unknown',
                                                             'origin': 'SHWD', 'independence': 'unknown'},
                                                 {'000034.jpg': 'source-scene-unknown'})
            self.assertEqual(manifest['records'][0]['source_filename'], '000034.jpg')
            self.assertEqual(manifest['records'][0]['original_filename'], '000034.jpeg')

    def test_source_filename_resolution_preserves_case_and_fails_ambiguous_stems(self):
        import inspect
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'frame.xml'
            path.write_text('<annotation><filename>frame.jpg</filename><size><width>100</width><height>80</height></size><object><name>hat</name><bndbox><xmin>1</xmin><ymin>1</ymin><xmax>10</xmax><ymax>10</ymax></bndbox></object></annotation>')
            (root / 'FRAME.JPG').write_bytes(b'original image')
            self.assertIn('filename_policy', inspect.signature(self.ds.voc_to_coco).parameters)
            result = self.ds.voc_to_coco([path], {'hat': 1}, image_root=root, filename_policy='unique-stem')
            self.assertEqual(result['images'][0]['file_name'], 'FRAME.JPG')
            self.assertEqual(result['images'][0]['original_filename'], 'frame.jpg')
            path.write_text(path.read_text().replace('frame.jpg', 'frame.jpeg'))
            (root / 'frame.png').write_bytes(b'other image')
            with self.assertRaises(ValueError):
                self.ds.voc_to_coco([path], {'hat': 1}, image_root=root, filename_policy='unique-stem')


if __name__ == '__main__':
    unittest.main()
