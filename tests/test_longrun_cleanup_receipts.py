"""Longrun lifecycle evidence through a real local Pipeline and video source.

Only the external RKNNLite SDK and board-only resource readings are replaced.
These tests do not certify board inference, camera capture or 30-minute runs.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'docs/experiments/2026-11-14/verify_runtime.py'


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LongrunCleanupReceipts(unittest.TestCase):
    def evidence(self):
        # Hand-checked complete evidence fixture maintained by the verifier tests.
        fixture = load_module(ROOT / 'tests/test_board_validation.py', 'cleanup_fixture')
        return fixture.BoardValidationTests().evidence()

    def test_new_longrun_without_direct_receipts_is_not_accepted(self):
        module = load_module(SCRIPT, 'longrun_verifier')
        manifest, *rest = self.evidence()
        manifest.update(schema_version=2, cleanup_contract='direct-sdk-v1')
        manifest.pop('cleanup')
        result = module.validate_longrun(manifest, *rest)
        self.assertFalse(result['passed'], result)
        self.assertTrue(any('direct' in error for error in result['errors']), result)

    def test_legacy_longrun_remains_indirect_and_does_not_change_input(self):
        module = load_module(SCRIPT, 'longrun_verifier')
        manifest, *rest = self.evidence()
        manifest['schema_version'] = 1
        manifest.pop('cleanup')
        before = json.dumps(manifest, sort_keys=True)
        result = module.validate_longrun(manifest, *rest)
        self.assertTrue(result['passed'], result)
        self.assertFalse(result['cleanup_evidence']['direct_recorded'])
        self.assertFalse(result['cleanup_evidence']['accepted'])
        self.assertEqual(json.dumps(manifest, sort_keys=True), before)

    def test_new_longrun_contract_cannot_be_omitted_or_replaced_by_unknown_schema(self):
        module = load_module(SCRIPT, 'longrun_verifier')
        for schema, contract in ((2, None), (3, 'direct-sdk-v1'), (True, 'direct-sdk-v1')):
            with self.subTest(schema=schema, contract=contract):
                manifest, *rest = self.evidence()
                manifest['schema_version'] = schema
                if contract is not None:
                    manifest['cleanup_contract'] = contract
                result = module.validate_longrun(manifest, *rest)
                self.assertFalse(result['passed'], result)

    def run_local(self, folder, init_failure=False, release_failure=False):
        from helmet import pipeline_runtime as runtime
        module = load_module(SCRIPT, 'longrun_verifier')
        api = types.ModuleType('rknnlite.api')
        class SDK:
            def __init__(self, verbose=False):
                pass
            def load_rknn(self, path):
                return 0
            def init_runtime(self):
                return -1 if init_failure else 0
            def inference(self, inputs, data_format):
                if data_format != 'nhwc' or inputs[0].shape != (1, 32, 32, 3):
                    raise ValueError('unexpected real Pipeline SDK input')
                return [np.full((1, 21, grid, grid), -20, np.float32)
                        for grid in (4, 2, 1)]
            def release(self):
                if release_failure:
                    raise RuntimeError('external SDK release failed')
        api.RKNNLite = SDK
        root = Path(folder)
        model = root / 'helmet_32x32.rknn'
        model.write_bytes(b'local SDK boundary test model')
        video = root / 'replay.avi'
        writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), 20, (32, 24))
        self.assertTrue(writer.isOpened(), 'local replay test requires MJPG writer')
        writer.write(np.full((24, 32, 3), 90, np.uint8))
        writer.write(np.full((24, 32, 3), 30, np.uint8))
        writer.release()
        governor = root / 'governor'
        governor.write_text('ondemand')
        # Keep the actual monitor/journal and Pipeline sampling lifecycles.
        monitor_class = module.ResourceMonitor
        def fast_monitor(sampler, destination):
            return monitor_class(sampler, destination, interval=.025)
        def sample(sampler):
            return {'monotonic': time.monotonic(), 'cpu_percent': 10.0,
                    'rss_bytes': 1000000, 'temperature_c': {'fixture': 50.0},
                    'npu_frequency_hz': {'fixture': 400000000}}
        guard_class = module.GovernorGuard
        args = types.SimpleNamespace(command='longrun', output_root=str(root), run_id='run',
                                     runtime_dir=str(ROOT / 'helmet'), model=str(model),
                                     source=str(video), duration=.4, replay_fps=20,
                                     result_ttl=1, save_interval=0, governor=None,
                                     input_description='local external SDK boundary test')
        with patch.dict(sys.modules, {'rknnlite': types.ModuleType('rknnlite'), 'rknnlite.api': api}), \
                patch.object(module, 'runtime_module', return_value=runtime), \
                patch.object(module, 'ResourceMonitor', side_effect=fast_monitor), \
                patch.object(runtime.RuntimeSampler, 'sample', sample), \
                patch.object(module, 'GovernorGuard', side_effect=lambda **kwargs: guard_class(paths=[governor])):
            with contextlib.redirect_stdout(io.StringIO()):
                code = module.run_longrun(args)
        manifest = json.loads((root / 'run/manifest.json').read_text(encoding='utf-8'))
        self.assertIs(api.RKNNLite, SDK, 'SDK factory must be restored after the experiment')
        return module, code, manifest

    def test_completed_longrun_records_workers_source_and_delegated_sdk_release(self):
        with tempfile.TemporaryDirectory() as folder:
            module, code, manifest = self.run_local(folder)
        self.assertEqual(code, 0, manifest)
        self.assertGreater(manifest['actual_engine_calls'], 0)
        self.assertTrue(manifest.get('cleanup'), 'longrun omitted its resource receipts')
        record = manifest['cleanup']
        self.assertTrue(record['engine_initialized'])
        self.assertTrue(record['source_initialized'])
        self.assertTrue(record['engine_released'])
        self.assertTrue(record['source_released'])
        self.assertFalse(record['capture_worker_alive'])
        self.assertFalse(record['detect_worker_alive'])
        self.assertEqual(record['sdk_resources'], [{'created': True, 'released': True,
                                                   'release_error': None}])
        self.assertGreater(manifest['actual_source_reads'], 0)
        self.assertTrue(manifest['validation']['cleanup_evidence']['accepted'], manifest)

    def test_failed_model_init_records_partial_sdk_and_releases_open_source(self):
        with tempfile.TemporaryDirectory() as folder:
            module, code, manifest = self.run_local(folder, init_failure=True)
        self.assertEqual(code, 1, manifest)
        self.assertEqual(manifest['actual_engine_calls'], 0)
        self.assertTrue(manifest.get('cleanup'), 'partial SDK creation was not recorded')
        record = manifest['cleanup']
        self.assertFalse(record['engine_initialized'])
        self.assertIsNone(record['engine_released'])
        self.assertTrue(record['source_initialized'])
        self.assertTrue(record['source_released'])
        self.assertEqual(record['sdk_resources'], [{'created': True, 'released': True,
                                                   'release_error': None}])
        self.assertTrue(module.cleanup_evidence(manifest, require_direct=True)['accepted'], manifest)

    def test_sdk_release_failure_is_preserved_and_never_accepted_as_clean_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            module, code, manifest = self.run_local(folder, release_failure=True)
        self.assertEqual(code, 1, manifest)
        self.assertTrue(manifest.get('cleanup'), 'failed SDK release receipt was lost')
        record = manifest['cleanup']
        self.assertFalse(record['engine_released'])
        self.assertEqual(record['sdk_resources'][0]['release_error'], 'external SDK release failed')
        self.assertFalse(record['sdk_resources'][0]['released'])
        self.assertFalse(module.cleanup_evidence(manifest, require_direct=True)['accepted'])


if __name__ == '__main__':
    unittest.main()
