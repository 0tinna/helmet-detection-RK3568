"""Standalone benchmark policy/resource cleanup with fake external SDK only."""
import contextlib
import importlib
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
from types import ModuleType
import unittest
from unittest.mock import patch

import cv2
import numpy as np


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.governor = self.root / 'governor'
        self.governor.write_text('powersave\n')
        self.image = self.root / 'input.png'
        cv2.imwrite(str(self.image), np.zeros((32, 32, 3), np.uint8))
        self.model = self.root / 'helmet_32x32.rknn'
        self.model.write_bytes(b'fake existing model')
        self.engines = []
        self.failure = None
        self.signal_number = None
        self.signal_dispatch = signal.raise_signal
        owner = self

        class Engine:
            def __init__(self, verbose=False):
                self.released = False
                self.model = None
                owner.engines.append(self)

            def load_rknn(self, model):
                self.model = model
                return 1 if owner.failure == 'load' else 0

            def init_runtime(self):
                return 1 if owner.failure == 'init' else 0

            def inference(self, inputs, data_format):
                if owner.failure == 'infer':
                    raise RuntimeError('fake inference failure')
                if owner.failure == 'interrupt':
                    raise KeyboardInterrupt()
                if owner.signal_number is not None:
                    number, owner.signal_number = owner.signal_number, None
                    if owner.signal_dispatch is signal.raise_signal:
                        handler = signal.getsignal(number)
                        if not callable(handler) or handler is signal.default_int_handler:
                            raise RuntimeError('benchmark has not installed a catchable signal handler')
                    owner.signal_dispatch(number)
                time.sleep(.02)
                return [np.full((1, 21, n, n), -50., np.float32) for n in (4, 2, 1)]

            def release(self):
                self.released = True

        lite = ModuleType('rknnlite.api')
        lite.RKNNLite = Engine
        package = ModuleType('rknnlite')
        package.__path__ = []
        package.api = lite
        with patch.dict(sys.modules, {'rknnlite': package, 'rknnlite.api': lite}):
            sys.modules.pop('helmet.benchmark', None)
            self.bench = importlib.import_module('helmet.benchmark')
        self.sdk = patch.dict(sys.modules, {'rknnlite': package, 'rknnlite.api': lite})
        self.sdk.start()
        self.addCleanup(self.sdk.stop)
        self.gov_patch = patch.object(self.bench, 'GOVERNOR_PATH', self.governor, create=True)
        self.gov_patch.start()
        self.addCleanup(self.gov_patch.stop)

    def invoke(self, extra=()):
        args = ['benchmark.py', '--model', str(self.model), '--image', str(self.image),
                '--loops', '1', '--warmup', '0', *extra]
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', args), contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            return self.bench.main()

    def test_cli_performance_governor_is_active_then_restored_with_readback(self):
        original = self.bench.bench_one
        observed = []
        def measure(*args, **kwargs):
            observed.append(self.governor.read_text().strip())
            return original(*args, **kwargs)
        with patch.object(self.bench, 'bench_one', side_effect=measure):
            code = self.invoke()
        self.assertEqual(observed, ['performance'])
        self.assertEqual(self.governor.read_text().strip(), 'powersave')
        self.assertEqual(code, 0)
        self.assertIn('powersave', self.stdout.getvalue())
        self.assertTrue(all(engine.released for engine in self.engines))

    def test_engine_release_on_load_init_inference_and_postprocess_failure(self):
        for failure in ('load', 'init', 'infer', 'post'):
            with self.subTest(stage=failure):
                self.failure = failure
                self.engines.clear()
                post = patch.object(self.bench, 'postprocess', side_effect=RuntimeError('fake post failure')) if failure == 'post' else contextlib.nullcontext()
                with post:
                    try:
                        self.bench.bench_one(str(self.model), cv2.imread(str(self.image)), loops=1, warmup=0)
                    except RuntimeError:
                        pass
                self.assertEqual(len(self.engines), 1)
                self.assertTrue(self.engines[0].released)

    def test_failed_model_cli_is_nonzero_and_restores_governor(self):
        for failure in ('load', 'init', 'infer', 'post'):
            with self.subTest(stage=failure):
                self.failure = failure
                post = patch.object(self.bench, 'postprocess', side_effect=RuntimeError('fake post failure')) if failure == 'post' else contextlib.nullcontext()
                with post:
                    code = self.invoke()
                self.assertIsInstance(code, int)
                self.assertNotEqual(code, 0)
                self.assertEqual(self.governor.read_text().strip(), 'powersave')

    def test_mixed_models_are_failure_and_success_json_remains_a_list(self):
        second = self.root / 'second.rknn'
        second.write_bytes(b'second model')
        target = self.root / 'results.json'
        calls = []
        row = {'model': 'second.rknn', 'size': '640x640', 'infer_ms': 1., 'post_ms': 1.,
               'e2e_ms': 2., 'fps': 500., 'temp': 40., 'loops': 1}
        def measure(model, *args, **kwargs):
            calls.append(model)
            return None if len(calls) == 1 else dict(row)
        with patch.object(self.bench, 'bench_one', side_effect=measure), patch.object(self.bench.glob, 'glob', return_value=[str(self.model), str(second)]):
            argv = ['benchmark.py', '--model-dir', str(self.root), '--image', str(self.image),
                    '--loops', '1', '--warmup', '0', '--json', str(target)]
            with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = self.bench.main()
        self.assertIsInstance(code, int)
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(target.read_text()), [row])
        self.assertEqual(self.governor.read_text().strip(), 'powersave')

    def test_invalid_parameters_fail_before_governor_or_engine_use(self):
        cases = [('--loops', '0'), ('--loops', '-1'), ('--warmup', '-1'),
                 ('--conf', '0'), ('--conf', '-.1'), ('--conf', '1.1'),
                 ('--conf', 'nan'), ('--conf', 'inf')]
        for case in cases:
            with self.subTest(option=case):
                self.engines.clear()
                try:
                    code = self.invoke(case)
                except SystemExit as error:
                    code = error.code
                self.assertNotEqual(code, 0)
                self.assertEqual(self.engines, [])
                self.assertEqual(self.governor.read_text(), 'powersave\n')

    def test_result_path_cannot_overwrite_governor_after_restoration(self):
        try:
            code = self.invoke(['--json', str(self.governor)])
        except SystemExit as error:
            code = error.code
        self.assertNotEqual(code, 0)
        self.assertEqual(self.engines, [])
        self.assertEqual(self.governor.read_text(), 'powersave\n')

    def test_governor_set_readback_failure_is_visible_and_restores(self):
        actual_write = Path.write_text
        def ignored_set(path, text, *args, **kwargs):
            if path == self.governor and text.strip() == 'performance':
                return len(text)
            return actual_write(path, text, *args, **kwargs)
        with patch.object(Path, 'write_text', ignored_set):
            code = self.invoke()
        self.assertIsInstance(code, int)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.engines, [])
        self.assertEqual(self.governor.read_text().strip(), 'powersave')

    def test_governor_restore_readback_failure_is_nonzero(self):
        actual_write = Path.write_text
        def ignored_restore(path, text, *args, **kwargs):
            if path == self.governor and text.strip() == 'powersave':
                return len(text)
            return actual_write(path, text, *args, **kwargs)
        with patch.object(Path, 'write_text', ignored_restore):
            code = self.invoke()
        self.assertIsInstance(code, int)
        self.assertNotEqual(code, 0)
        self.assertIn('restor', self.stderr.getvalue().lower())
        self.assertTrue(all(engine.released for engine in self.engines))

    def test_governor_restore_write_error_still_records_actual_readback(self):
        target = self.root / 'failed-restore.json'
        actual_write = Path.write_text
        def denied_restore(path, text, *args, **kwargs):
            if path == self.governor and text.strip() == 'powersave':
                raise OSError('fake restore write denial')
            return actual_write(path, text, *args, **kwargs)
        with patch.object(Path, 'write_text', denied_restore):
            code = self.invoke(['--manifest', str(target)])
        self.assertNotEqual(code, 0)
        evidence = json.loads(target.read_text())
        self.assertEqual(evidence['governor']['restored_readback'], 'performance')
        self.assertEqual(evidence['status'], 'failed')
        self.assertTrue(self.engines[0].released)

    def test_governor_set_write_error_still_records_actual_readback_and_restores(self):
        target = self.root / 'failed-set.json'
        actual_write = Path.write_text
        def denied_set(path, text, *args, **kwargs):
            if path == self.governor and text.strip() == 'performance':
                raise OSError('fake set write denial')
            return actual_write(path, text, *args, **kwargs)
        with patch.object(Path, 'write_text', denied_set):
            code = self.invoke(['--manifest', str(target)])
        self.assertNotEqual(code, 0)
        evidence = json.loads(target.read_text())
        self.assertEqual(evidence['governor']['performance_readback'], 'powersave')
        self.assertEqual(evidence['governor']['restored_readback'], 'powersave')
        self.assertEqual(self.engines, [])

    def test_keyboard_interrupt_releases_and_restores(self):
        self.failure = 'interrupt'
        try:
            code = self.invoke()
        except KeyboardInterrupt:
            code = 130
        self.assertEqual(code, 130)
        self.assertTrue(self.engines[0].released)
        self.assertEqual(self.governor.read_text().strip(), 'powersave')

    def test_catchable_int_and_term_release_restore_and_restore_handlers(self):
        for number in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=number):
                previous = signal.getsignal(number)
                self.signal_number = number
                self.engines.clear()
                with patch.object(self.bench, 'bench_one', wraps=self.bench.bench_one):
                    code = self.invoke()
                self.assertEqual(code, 128 + number)
                self.assertTrue(self.engines[0].released)
                self.assertEqual(self.governor.read_text().strip(), 'powersave')
                self.assertIs(signal.getsignal(number), previous)

    def test_hup_unwinds_the_same_cleanup_with_portable_signal_boundary(self):
        handlers = {}
        def register(number, handler):
            previous = handlers.get(number, signal.SIG_DFL)
            handlers[number] = handler
            return previous
        self.signal_dispatch = lambda number: handlers[number](number, None)
        self.signal_number = 1
        with patch.object(signal, 'SIGHUP', 1, create=True), patch.object(signal, 'signal', side_effect=register):
            code = self.invoke()
        self.assertEqual(code, 129)
        self.assertTrue(self.engines[0].released)
        self.assertEqual(self.governor.read_text().strip(), 'powersave')

    def test_manifest_records_configuration_hashes_runtime_and_restored_policy(self):
        manifest = self.root / 'measurement.json'
        result = self.root / 'result.json'
        code = self.invoke(['--manifest', str(manifest), '--json', str(result)])
        self.assertEqual(code, 0)
        self.assertIsInstance(json.loads(result.read_text()), list)
        evidence = json.loads(manifest.read_text())
        self.assertEqual(evidence['status'], 'complete')
        self.assertEqual(evidence['governor']['original'], 'powersave')
        self.assertEqual(evidence['governor']['performance_readback'], 'performance')
        self.assertEqual(evidence['governor']['restored_readback'], 'powersave')
        self.assertEqual(evidence['config']['loops'], 1)
        self.assertEqual(evidence['config']['warmup'], 0)
        self.assertEqual(evidence['config']['conf'], .25)
        self.assertEqual(len(evidence['input']['sha256']), 64)
        self.assertTrue(evidence['runtime']['python'])
        self.assertEqual(len(evidence['measurements'][0]['infer_ms_samples']), 1)
        self.assertEqual(len(evidence['measurements'][0]['post_ms_samples']), 1)
        self.assertEqual(len(evidence['measurements'][0]['e2e_ms_samples']), 1)

    def test_help_does_not_require_an_installed_rknn_sdk(self):
        import subprocess
        result = subprocess.run([sys.executable, str(Path(self.bench.__file__)), '--help'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
