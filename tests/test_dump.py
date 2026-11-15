"""Exercise board dump with real preprocessing/decoder and an injected engine."""
import importlib
import importlib.util
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'helmet'))


class DumpTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('dump_dets'), 'board dump missing')
        self.dump = importlib.import_module('dump_dets')

    def test_unknown_model_size_fails_without_explicit_size(self):
        with self.assertRaises(ValueError):
            self.dump.resolve_size('unknown.rknn')
        self.assertEqual(self.dump.resolve_size('helmet-640-640.rknn'), (640, 640))
        self.assertEqual(self.dump.resolve_size('helmet_512x288.rknn'), (512, 288))
        with self.assertRaises(ValueError):
            self.dump.resolve_size('helmet_512x288.rknn', [640, 640])

    def run_fixture(self, fail=False):
        import cv2
        import numpy as np

        class Engine:
            released = False

            def load_rknn(self, path):
                return 0

            def init_runtime(self):
                return 0

            def inference(self, inputs, data_format):
                if fail:
                    return None
                if inputs[0].shape != (1, 32, 32, 3) or data_format != 'nhwc':
                    raise ValueError('incorrect model input')
                outputs = [np.full((1, 21, s, s), -50., np.float32) for s in (4, 2, 1)]
                outputs[0][0, :7, 2, 2] = [0, 0, 0, 0, 50, -50, 50]
                return outputs

            def release(self):
                self.released = True

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cv2.imwrite(str(root / 'a.jpg'), np.zeros((32, 32, 3), np.uint8))
            model = root / 'helmet_32x32.rknn'
            model.write_bytes(b'existing model fixture')
            engine = Engine()
            result = self.dump.dump_dataset(model, root,
                                            [{'id': 1, 'file_name': 'a.jpg', 'width': 32, 'height': 32}],
                                            engine_factory=lambda: engine)
            self.assertTrue(engine.released)
            return result

    def test_shared_decoder_dump_has_hashes_and_complete_per_image_status(self):
        result = self.run_fixture()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['inputs'][0]['status'], 'ok')
        self.assertEqual(len(result['model']['sha256']), 64)
        row = result['detections']['a.jpg'][0]
        self.assertEqual(row[0], 1)
        self.assertAlmostEqual(row[1], 1.)
        self.assertEqual(result['config']['op_conf'], .25)
        self.assertEqual(result['config']['nms_iou'], .45)
        import eval_map
        self.assertEqual(len(eval_map.load_detections(result, [{'id': 1, 'file_name': 'a.jpg',
                                                               'width': 32, 'height': 32}])[1]), 1)

    def test_failed_inference_records_failure_and_releases_engine(self):
        result = self.run_fixture(fail=True)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['inputs'][0]['status'], 'error')
        self.assertTrue(result['errors'])
        self.assertNotIn('a.jpg', result['detections'])

    def test_package_api_executes_without_caller_path_changes(self):
        script = '''
import tempfile
from pathlib import Path
import cv2
import numpy as np
import helmet.dump_dets as dump
class Engine:
    def load_rknn(self, path): return 0
    def init_runtime(self): return 0
    def inference(self, inputs, data_format):
        return [np.full((1,21,s,s), -50., np.float32) for s in (4,2,1)]
    def release(self): pass
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    model = root / 'helmet_32x32.rknn'
    model.write_bytes(b'model')
    cv2.imwrite(str(root / 'a.png'), np.zeros((32,32,3), np.uint8))
    result = dump.dump_dataset(model, root, [{'id':1,'file_name':'a.png','width':32,'height':32}], engine_factory=Engine)
    assert result['status'] == 'complete', result
    assert result['detections'] == {'a.png': []}, result
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / 'docs/experiments/2026-11-14/clipped-candidate-repro.npz').is_file(),
                         'requires archived real board raw heads')
    def test_real_padding_candidates_are_discarded_with_trace_after_shared_decode(self):
        import cv2
        import numpy as np
        artifact = Path(__file__).resolve().parents[1] / 'docs/experiments/2026-11-14/clipped-candidate-repro.npz'
        with np.load(artifact) as data:
            heads = [data['arr_%d' % i].copy() for i in range(3)]

        class Engine:
            def load_rknn(self, path): return 0
            def init_runtime(self): return 0
            def inference(self, inputs, data_format): return heads
            def release(self): pass

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / 'helmet_416x416.rknn'
            model.write_bytes(b'model identity fixture')
            cv2.imwrite(str(root / 'a.png'), np.zeros((540, 720, 3), np.uint8))
            images = [{'id': 1, 'file_name': 'a.png', 'width': 720, 'height': 540}]
            result = self.dump.dump_dataset(model, root, images, engine_factory=Engine)
            self.assertEqual(result['status'], 'complete', result['errors'])
            self.assertEqual(len(result['detections']['a.png']), 143)
            item = result['inputs'][0]
            self.assertEqual(item['discarded_clipped_count'], 2)
            self.assertEqual(len(item['discarded_clipped_boxes']), 2)
            self.assertEqual(item['discarded_clipped_boxes'][0][0], 0)
            self.assertEqual(item['discarded_clipped_boxes'][0][3], 539.)
            self.assertEqual(item['discarded_clipped_boxes'][0][5], 539.)
            self.assertEqual(result['counts']['discarded_clipped_count'], 2)
            import eval_map
            self.assertEqual(len(eval_map.load_detections(result, images)[1]), 143)

    def test_clipped_partition_rejects_invalid_classes_scores_nonfinite_and_reversed_boxes(self):
        import eval_map
        self.assertTrue(callable(getattr(eval_map, 'partition_clipped_candidates', None)))
        for row in [(2, .1, 0, 0, 10, 0), (1, float('nan'), 0, 0, 10, 0),
                    (1, 1.1, 0, 0, 10, 0), (1, .1, float('nan'), 0, 10, 0),
                    (1, .1, 20, 0, 10, 0)]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                eval_map.partition_clipped_candidates([row], (100, 100))


if __name__ == '__main__':
    unittest.main()
