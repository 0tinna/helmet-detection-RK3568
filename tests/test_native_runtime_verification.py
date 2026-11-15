"""Native evidence uses real Pipeline/HTTP; only the external SDK/OS is doubled.

These temporary runs are host software evidence, never real NPU/Qt acceptance.
"""
import importlib.util
import inspect
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'docs/experiments/2026-11-15/verify_native_runtime.py'


class SDKBoundary:
    fail_release = False

    def __init__(self, **kwargs):
        pass

    def load_rknn(self, path):
        return 0

    def init_runtime(self):
        return 0

    def inference(self, **kwargs):
        return [np.full((1, 21, size, size), -12, np.float32) for size in (52, 26, 13)]

    def release(self):
        if self.fail_release:
            raise RuntimeError('test SDK release failure')


class HostResourceBoundary:
    def sample(self):
        import time
        return {'monotonic': time.monotonic(), 'cpu_percent': 1.0, 'rss_bytes': 1000000,
                'temperature_c': {'test_only': 40.0}, 'npu_frequency_hz': {'test_only': 1}}


class NativeRuntimeTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'cached_temp'):
            cls.cached_temp.cleanup()

    def setUp(self):
        self.assertTrue(TOOL.is_file(), 'native runtime evidence runner is missing')
        spec = importlib.util.spec_from_file_location('native_evidence_test', TOOL)
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def prepare(self, parent):
        app_root = parent / 'app'
        (app_root / 'models').mkdir(parents=True)
        (app_root / 'input').mkdir()
        (app_root / 'models/helmet_416x416.rknn').write_bytes(b'host software SDK boundary fixture')
        writer = cv2.VideoWriter(str(app_root / 'input/construction-replay.avi'),
                                 cv2.VideoWriter_fourcc(*'MJPG'), 20, (64, 48))
        self.assertTrue(writer.isOpened())
        for number in range(8):
            writer.write(np.full((48, 64, 3), number * 20, np.uint8))
        writer.release()
        shutil.copytree(ROOT / 'helmet', app_root / 'src', ignore=shutil.ignore_patterns('__pycache__'))
        return app_root

    def execute(self, parent, release_failure=False, read_failure=False):
        app_root = self.prepare(parent)
        args = self.tool.build_parser().parse_args([
            'run', '--app-root', str(app_root), '--output-root', str(parent / 'runs'),
            '--run-id', 'short-host', '--duration', '6.2', '--model', 'helmet_416x416.rknn',
            '--source', 'construction-replay.avi', '--profile', 'responsive'])
        api = types.ModuleType('rknnlite.api')
        api.RKNNLite = SDKBoundary
        SDKBoundary.fail_release = release_failure
        self.addCleanup(setattr, SDKBoundary, 'fail_release', False)
        governor = parent / 'host-only-governor'
        governor.write_text('test', encoding='utf-8')
        # OS thermal/devfreq boundaries are unavailable on Windows. The real
        # Pipeline, source decoder, telemetry, controller and HTTP are retained.
        with patch.dict(sys.modules, {'rknnlite': types.ModuleType('rknnlite'), 'rknnlite.api': api}), \
                patch.object(self.tool, 'read_governors', return_value=[
                    {'path': str(governor), 'before': 'test', 'after': None, 'restore_success': False}]), \
                patch.object(self.tool, 'runtime_sampler', return_value=HostResourceBoundary()):
            if read_failure:
                runtime, _ = self.tool.load_app(app_root)
                from contextlib import ExitStack
                with ExitStack() as stack:
                    stack.enter_context(patch.object(runtime.VideoSource if hasattr(runtime, 'VideoSource') else
                        __import__('sources').VideoSource, 'read', side_effect=RuntimeError('test source read failure')))
                    code = self.tool.run_native(args)
            else:
                code = self.tool.run_native(args)
        return code, parent / 'runs/short-host'

    def base(self, suffix='evidence'):
        cls = type(self)
        if not hasattr(cls, 'cached_run'):
            cls.cached_temp = tempfile.TemporaryDirectory()
            code, candidate = self.execute(Path(cls.cached_temp.name))
            self.assertEqual(code, 0)
            cls.cached_run = candidate
        destination = Path(self.temp.name) / suffix
        shutil.copytree(cls.cached_run, destination)
        return destination

    def mutate(self, name, action):
        directory = self.base()
        path = directory / name
        data = json.loads(path.read_text(encoding='utf-8')) if path.suffix == '.json' else self.tool.old.read_jsonl(path)
        action(data)
        if path.suffix == '.json':
            self.tool.old.write_json(path, data)
        else:
            path.write_text(''.join(json.dumps(row) + '\n' for row in data), encoding='utf-8')
        # Rehashing adversarial input ensures checks are semantic, not just hash checks.
        if name != 'manifest.json':
            manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
            manifest['artifacts'][name] = self.tool.old.file_record(path)
            self.tool.old.write_json(directory / 'manifest.json', manifest)
        return self.tool.verify_evidence(directory)

    def test_real_pipeline_http_decoded_preview_and_cleanup(self):
        result = self.tool.verify_evidence(self.base())
        self.assertTrue(result['passed'], result['errors'])
        self.assertGreater(result['http']['decoded_previews'], 0)
        self.assertGreater(result['observed_rates']['detected_fps'], 0)
        self.assertEqual(result['public_target_fps'], 10)
        self.assertEqual(result['requested_replay_fps'], 20)
        self.assertFalse(result['native_gui_verified'])
        self.assertFalse(result['thirty_minute_app_passed'])

    def test_counter_corruption_is_rejected_even_after_rehash(self):
        result = self.mutate('session.json', lambda d: d['counters'].__setitem__('output', 100000))
        self.assertFalse(result['passed'])
        self.assertTrue(any('inventory' in e or 'counter' in e for e in result['errors']))

    def test_missing_sdk_receipt_is_rejected(self):
        result = self.mutate('manifest.json', lambda d: d['cleanup'].__setitem__('sdk_resources', []))
        self.assertFalse(result['passed'])
        self.assertTrue(any('SDK' in e for e in result['errors']))

    def test_claimed_gui_without_exit_is_rejected(self):
        result = self.mutate('manifest.json', lambda d: d['gui'].update(requested=True, started=True,
                            exited=False, pid=1234, returncode=None))
        self.assertFalse(result['passed'])
        self.assertTrue(any('GUI' in e for e in result['errors']))

    def test_expired_preview_is_rejected(self):
        def expired(rows):
            next(r for r in rows if r.get('route') == '/api/preview' and r.get('status') == 200)['body']['remaining_ttl_ms'] = -1
        result = self.mutate('http.jsonl', expired)
        self.assertFalse(result['passed'])
        self.assertTrue(any('TTL' in e or 'preview' in e for e in result['errors']))

    def test_previous_generation_preview_is_rejected(self):
        def changed(rows):
            next(r for r in rows if r.get('route') == '/api/preview' and r.get('status') == 200)['body']['generation'] = 'old-run'
        result = self.mutate('http.jsonl', changed)
        self.assertFalse(result['passed'])
        self.assertTrue(any('generation' in e for e in result['errors']))

    def test_missing_resource_artifact_is_rejected(self):
        directory = self.base()
        (directory / 'resources.jsonl').unlink()
        result = self.tool.verify_evidence(directory)
        self.assertFalse(result['passed'])
        self.assertTrue(any('resources' in e for e in result['errors']))

    def test_pipeline_resource_count_mismatch_is_rejected(self):
        result = self.mutate('session.json', lambda d: d['runtime_sample_retention'].__setitem__('total_count', 99999))
        self.assertFalse(result['passed'])
        self.assertTrue(any('sidecar' in e for e in result['errors']))

    def test_copied_model_provenance_is_recomputed(self):
        directory = self.base()
        model = directory / 'provenance/model/helmet_416x416.rknn'
        self.assertTrue(model.is_file(), 'offline evidence needs the actual input/model/source bytes')
        model.write_bytes(b'changed-model')
        result = self.tool.verify_evidence(directory)
        self.assertFalse(result['passed'])
        self.assertTrue(any('provenance' in e for e in result['errors']))

    def test_unhashed_raw_file_is_rejected(self):
        directory = self.base()
        (directory / 'unrecorded.jpg').write_bytes(b'untracked')
        result = self.tool.verify_evidence(directory)
        self.assertFalse(result['passed'])
        self.assertTrue(any('unhashed' in e for e in result['errors']))

    def test_removing_http_observations_cannot_claim_complete_run(self):
        result = self.mutate('http.jsonl', lambda rows: rows.__setitem__(slice(None),
                             [row for row in rows if row.get('route') != '/api/status']))
        self.assertFalse(result['passed'])
        self.assertTrue(any('HTTP' in e for e in result['errors']))

    def test_source_release_receipt_is_required(self):
        result = self.mutate('manifest.json', lambda d: d['cleanup'].__setitem__('source_released', False))
        self.assertFalse(result['passed'])
        self.assertTrue(any('source' in e for e in result['errors']))

    def test_real_source_symlink_escape_is_rejected_before_copy(self):
        parent = Path(self.temp.name)
        app_root = self.prepare(parent)
        source = app_root / 'input/construction-replay.avi'
        outside = parent / 'outside.avi'
        source.replace(outside)
        try:
            source.symlink_to(outside)
        except OSError:
            self.skipTest('Windows account cannot create symlinks')
        args = self.tool.build_parser().parse_args(['run', '--app-root', str(app_root),
            '--output-root', str(parent / 'runs'), '--run-id', 'escape', '--duration', '1'])
        code = self.tool.run_native(args)
        self.assertNotEqual(code, 0)
        self.assertFalse((parent / 'runs/escape/provenance/input/outside.avi').exists())

    def test_current_verifier_bytes_must_match_measured_verifier(self):
        result = self.mutate('manifest.json', lambda d: d['sources']['verify_native_runtime.py'].__setitem__('sha256', 'f' * 64))
        self.assertFalse(result['passed'])
        self.assertTrue(any('provenance' in e for e in result['errors']))

    def test_changed_source_epoch_preview_is_rejected(self):
        def changed(rows):
            next(r for r in rows if r.get('route') == '/api/preview' and r.get('status') == 200)['body']['source_epoch'] = 999
        result = self.mutate('http.jsonl', changed)
        self.assertFalse(result['passed'])
        self.assertTrue(any('epoch' in e for e in result['errors']))

    def test_jpeg_payload_corruption_is_rejected(self):
        def changed(rows):
            next(r for r in rows if r.get('route') == '/api/preview' and r.get('status') == 200)['body']['jpeg_base64'] = 'aW52YWxpZA=='
        result = self.mutate('http.jsonl', changed)
        self.assertFalse(result['passed'])
        self.assertTrue(any('JPEG' in e for e in result['errors']))

    def test_one_preview_observation_cannot_cover_an_extended_active_interval(self):
        self.assertIn('timeline', inspect.signature(self.tool.validate_http).parameters,
                      'HTTP verification lacks the actual active timeline')
        directory = self.base()
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        rows = self.tool.old.read_jsonl(directory / 'http.jsonl')
        outputs = [r for r in self.tool.old.read_jsonl(directory / 'frames.jsonl') if r['record_type'] == 'output']
        active = next(r for r in rows if r.get('phase') == 'active')
        result = self.tool.validate_http(manifest, rows, outputs, directory,
            {'active_start': active['started_monotonic'] - 20, 'active_stop': active['finished_monotonic'] + 20})
        self.assertTrue(any('coverage' in e or 'count' in e for e in result['errors']))

    def test_preview_must_have_positive_qml_conservative_ttl_budget(self):
        def delayed(rows):
            row = next(r for r in rows if r.get('route') == '/api/preview' and r.get('status') == 200)
            row['started_monotonic'] = row['finished_monotonic'] - 1.1
            row['latency_ms'] = 1100.0
        result = self.mutate('http.jsonl', delayed)
        self.assertFalse(result['passed'])
        self.assertTrue(any('TTL' in e for e in result['errors']))

    def gui_claim(self):
        """Adversarial GUI claim on real host records, only used to reject it."""
        directory = self.base()
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        events = self.tool.old.read_jsonl(directory / 'events.jsonl')
        start = next(e['monotonic'] for e in events if e.get('state') == 'startup')
        stop = next(e['monotonic'] for e in events if e.get('state') == 'stop')
        resources = self.tool.old.read_jsonl(directory / 'resources.jsonl')
        for row in resources:
            row['gui'] = {'pid': 1234, 'start_id': 'negative-test-only', 'monotonic': row['monotonic'],
                          'rss_bytes': 1000000, 'cpu_percent': 1.0}
        (directory / 'resources.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in resources), encoding='utf-8')
        output = next(r for r in self.tool.old.read_jsonl(directory / 'frames.jsonl') if r['record_type'] == 'output')
        manifest['active_worker_finished_monotonic'] = stop
        manifest['gui'] = {'requested': True, 'started': True, 'exited': True, 'returncode': 0,
            'pid': 1234, 'started_monotonic': start - .1, 'ready_monotonic': start - .01,
            'exited_monotonic': stop + .1, 'render_received_monotonic': stop - .1}
        rendered = {'saved': True, 'valid': True, 'generation': manifest['app_generation'],
                    'frameId': output['frame_id'], 'surfaceWidth': 64, 'surfaceHeight': 48}
        cv2.imwrite(str(directory / 'screen.png'), np.zeros((48, 64, 3), np.uint8))
        (directory / 'gui.log').write_text('qml: NATIVE_RENDER ' + json.dumps(rendered) +
                                       '\nqml: NATIVE_EXIT pipeline-stopped\n', encoding='utf-8')
        for name in ('resources.jsonl', 'screen.png', 'gui.log'):
            manifest['artifacts'][name] = self.tool.old.file_record(directory / name)
        self.tool.old.write_json(directory / 'cleanup.json', {'cleanup': manifest['cleanup'],
            'cleanup_errors': manifest['cleanup_errors'], 'service': manifest['service_cleanup'], 'gui': manifest['gui']})
        manifest['artifacts']['cleanup.json'] = self.tool.old.file_record(directory / 'cleanup.json')
        self.tool.old.write_json(directory / 'manifest.json', manifest)
        return directory

    def test_blank_gui_render_claim_without_ready_preview_is_rejected(self):
        result = self.tool.verify_evidence(self.gui_claim())
        self.assertFalse(result['native_gui_verified'])
        self.assertTrue(any('GUI' in e for e in result['errors']))

    def test_gui_child_clock_cannot_be_reordered_against_parent_samples(self):
        directory = self.gui_claim()
        resources = self.tool.old.read_jsonl(directory / 'resources.jsonl')
        resources[0]['gui']['monotonic'], resources[1]['gui']['monotonic'] = (
            resources[1]['monotonic'], resources[0]['monotonic'])
        (directory / 'resources.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in resources), encoding='utf-8')
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        manifest['artifacts']['resources.jsonl'] = self.tool.old.file_record(directory / 'resources.jsonl')
        self.tool.old.write_json(directory / 'manifest.json', manifest)
        result = self.tool.verify_evidence(directory)
        self.assertTrue(any('GUI resource' in e for e in result['errors']))

    def test_gui_render_wrong_dropdown_selection_is_rejected(self):
        directory = self.gui_claim()
        path = directory / 'gui.log'
        lines = path.read_text(encoding='utf-8').splitlines()
        rendered = json.loads(lines[0].split('NATIVE_RENDER ', 1)[1])
        rendered.update(previewVisible=True, previewReady=True, previewSourceKind='inline-jpeg',
            previewSourceSize=[64, 48], uiSelection={'model': 'helmet-640-640.rknn',
            'source': '1.jpg', 'profile': 'replay'})
        lines[0] = 'qml: NATIVE_RENDER ' + json.dumps(rendered)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        manifest['artifacts']['gui.log'] = self.tool.old.file_record(path)
        self.tool.old.write_json(directory / 'manifest.json', manifest)
        result = self.tool.verify_evidence(directory)
        self.assertFalse(result['native_gui_verified'])
        self.assertTrue(any('GUI' in e for e in result['errors']))

    def test_signal_after_journal_close_preserves_failed_manifest_and_restores_handlers(self):
        original_handlers = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}
        closed = {'events': False, 'signal_sent': False}
        real_close, real_record = self.tool.old.Journal.close, self.tool.old.file_record

        def close(journal):
            name = journal.stream.name
            real_close(journal)
            if Path(name).name == 'events.jsonl':
                closed['events'] = True

        def file_record(path):
            if closed['events'] and not closed['signal_sent']:
                closed['signal_sent'] = True
                signal.raise_signal(signal.SIGTERM)
            return real_record(path)

        try:
            with patch.object(self.tool.old.Journal, 'close', close), patch.object(self.tool.old, 'file_record', file_record):
                try:
                    code, directory = self.execute(Path(self.temp.name))
                except Exception as error:
                    self.fail('signal interrupted evidence finalization: ' + type(error).__name__)
            self.assertNotEqual(code, 0)
            manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['signal'], signal.SIGTERM)
            self.assertEqual(manifest['status'], 'failed')
            self.assertTrue((directory / 'cleanup.json').is_file())
            for number, handler in original_handlers.items():
                self.assertEqual(signal.getsignal(number), handler)
        finally:
            for number, handler in original_handlers.items():
                signal.signal(number, handler)

    def test_signal_during_final_derived_publication_is_persisted_and_nonzero(self):
        real_write = self.tool.old.write_json
        sent = {'done': False}

        def write_json(path, value):
            real_write(path, value)
            if Path(path).name == 'run-verification.json' and not sent['done']:
                sent['done'] = True
                signal.raise_signal(signal.SIGTERM)

        with patch.object(self.tool.old, 'write_json', write_json):
            code, directory = self.execute(Path(self.temp.name))
        self.assertNotEqual(code, 0)
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['signal'], signal.SIGTERM)
        self.assertEqual(manifest['status'], 'failed')
        self.assertNotEqual(manifest['exit_code'], 0)
        self.assertFalse(self.tool.verify_evidence(directory)['passed'])

    def test_fixed_deadline_observer_permits_polling_jitter_before_measurement(self):
        directory = self.base()
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        rows = self.tool.old.read_jsonl(directory / 'http.jsonl')
        active = [r for r in rows if r.get('phase') == 'active' and r['route'] == '/api/status']
        # A completed second observation can start .05s closer to its nominal
        # deadline after the first poll overshot; each raw timestamp stays real.
        second = active[2]['observation']
        first_start = active[0]['started_monotonic']
        delta = first_start + 4.95 - active[2]['started_monotonic']
        for row in rows:
            if row.get('phase') == 'active' and row.get('observation') == second:
                row['started_monotonic'] += delta
                row['finished_monotonic'] += delta
        # Cadence is asserted separately from preview capture binding, which
        # remains intentionally inconsistent in this adversarial timing input.
        outputs = [r for r in self.tool.old.read_jsonl(directory / 'frames.jsonl') if r['record_type'] == 'output']
        result = self.tool.validate_http(manifest, rows, outputs, directory)
        self.assertFalse(any('cadence' in e for e in result['errors']), result['errors'])

    def test_wrong_json_shapes_produce_failed_derived_file(self):
        for name, value in (('session.json', []), ('cleanup.json', []), ('manifest.json', {'artifacts': []})):
            with self.subTest(name=name):
                directory = self.base(name + '-evidence')
                self.tool.old.write_json(directory / name, value)
                if name != 'manifest.json':
                    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
                    manifest['artifacts'][name] = self.tool.old.file_record(directory / name)
                    self.tool.old.write_json(directory / 'manifest.json', manifest)
                target = Path(self.temp.name) / (name + '-derived.json')
                try:
                    code = self.tool.main(['verify', str(directory), '--out', str(target)])
                except Exception as error:
                    self.fail('offline malformed JSON escaped without failed output: ' + type(error).__name__)
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(target.read_text(encoding='utf-8'))['passed'])

    def test_short_run_cannot_be_promoted_to_1800_seconds(self):
        result = self.mutate('manifest.json', lambda d: d.__setitem__('requested_duration_seconds', 1800))
        self.assertFalse(result['passed'])
        self.assertFalse(result['thirty_minute_app_passed'])

    def test_sdk_release_failure_persists_failed_evidence_and_nonzero(self):
        code, directory = self.execute(Path(self.temp.name), release_failure=True)
        self.assertNotEqual(code, 0)
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['status'], 'failed')
        self.assertFalse(manifest['cleanup']['sdk_resources'][0]['released'])
        self.assertTrue(manifest['cleanup_errors'])

    def test_source_read_failure_persists_failed_evidence_and_nonzero(self):
        code, directory = self.execute(Path(self.temp.name), read_failure=True)
        self.assertNotEqual(code, 0)
        self.assertTrue((directory / 'manifest.json').is_file())
        self.assertFalse(self.tool.verify_evidence(directory)['passed'])

    def test_invalid_arguments_and_escape_create_no_run(self):
        root = Path(self.temp.name)
        for value in ('../escape', 'a/b', 'a\\b', '.', '..'):
            with self.subTest(run_id=value), self.assertRaises(ValueError):
                self.tool.main(['run', '--app-root', str(ROOT), '--output-root', str(root), '--run-id', value])
        for value in ('0', '-1', 'nan', 'inf'):
            with self.subTest(duration=value), self.assertRaises(ValueError):
                self.tool.main(['run', '--app-root', str(ROOT), '--output-root', str(root), '--run-id', 'invalid', '--duration', value])
        self.assertEqual(list(root.iterdir()), [])

    def test_run_and_derived_output_refuse_overwrite(self):
        directory = self.base()
        target = Path(self.temp.name) / 'derived.json'
        self.assertEqual(self.tool.main(['verify', str(directory), '--out', str(target)]), 0)
        with self.assertRaises(FileExistsError):
            self.tool.main(['verify', str(directory), '--out', str(target)])
        before = (directory / 'manifest.json').read_bytes()
        with self.assertRaises(FileExistsError):
            self.tool.main(['run', '--app-root', str(ROOT), '--output-root', str(directory.parent), '--run-id', directory.name])
        self.assertEqual((directory / 'manifest.json').read_bytes(), before)

    def test_token_never_enters_public_evidence(self):
        directory = self.base()
        self.assertFalse(list(directory.rglob('App.qml')))
        for path in directory.rglob('*'):
            if path.is_file() and path.suffix in ('.json', '.jsonl', '.log', '.txt'):
                text = path.read_text(encoding='utf-8')
                self.assertNotIn('X-Helmet-Token', text)
                self.assertNotIn('apiToken', text)

    def test_offline_verifier_imports_without_sdk_or_qt(self):
        directory = self.base()
        out = Path(self.temp.name) / 'subprocess-derived.json'
        command = [sys.executable, str(TOOL), 'verify', str(directory), '--out', str(out)]
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertTrue(json.loads(out.read_text(encoding='utf-8'))['passed'])
