"""Verification-runner rules; no board/NPU/network execution."""
import importlib.util
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'docs/experiments/2026-11-14/verify_runtime.py'


class BoardValidationTests(unittest.TestCase):
    def module(self):
        self.assertTrue(SCRIPT.is_file(), 'board evidence verification rules are missing')
        spec = importlib.util.spec_from_file_location('verify_runtime', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def evidence(self):
        manifest = {'kind': 'longrun', 'requested_duration_seconds': 2,
                    'replay_fps': 10, 'result_ttl': 1, 'exit_code': 0,
                    'backend': 'rknnlite', 'physical_camera_tested': False,
                    'source_kind': 'video-replay', 'actual_engine_calls': 3,
                    'started_monotonic': 10, 'elapsed_seconds': 2.15,
                    'input_size': [32, 32], 'class_order': ['person', 'hat'],
                    'thresholds': {'conf': .35, 'nms': .45, 'skip': 0},
                    'model': {'path': 'helmet_32x32.rknn', 'sha256': 'a' * 64},
                    'input': {'path': 'replay.avi', 'sha256': 'b' * 64},
                    'cleanup': {'engine_initialized': True, 'source_initialized': True,
                                'engine_released': True, 'source_released': True,
                                'capture_worker_alive': False, 'detect_worker_alive': False,
                                'shutdown_timeout': False,
                                'sdk_resources': [{'created': True, 'released': True, 'release_error': None}]},
                    'sources': {name: {'sha256': 'c' * 64} for name in
                                ('pipeline_runtime.py', 'sources.py', 'telemetry.py', 'alarm.py',
                                 'yolo_post.py', 'verify_runtime.py')},
                    'governors': [{'before': 'ondemand', 'after': 'ondemand', 'restore_success': True}]}
        session = {'state': 'stop', 'elapsed_seconds': 2.1, 'model': 'helmet_32x32.rknn',
                   'model_sha256': 'a' * 64, 'source': 'replay.avi',
                   'input_size': [32, 32], 'class_order': ['person', 'hat'],
                   'thresholds': {'conf': .35, 'nms': .45}, 'replay_fps': 10, 'result_ttl': 1,
                   'counters': {'captured': 3, 'consumed': 3, 'detected': 3, 'output': 3,
                                'read_failure': 0, 'active_skip': 0, 'overwritten': 0,
                                'pending_capture': 0, 'pending_output': 0, 'output_overwritten': 0,
                                'displayed': 0, 'detection_failure': 0}}
        frames = []
        for frame_id, stamp, latency in ((1, 10.0, 20.0), (2, 10.95, 30.0), (3, 11.9, 40.0)):
            common = {'model': 'helmet_32x32.rknn', 'model_sha256': 'a' * 64, 'source': 'replay.avi',
                      'class_order': ['person', 'hat'], 'classes': [1], 'scores': [.8],
                      'boxes': [[1, 1, 2, 2]], 'source_epoch': 0}
            frames.append({'record_type': 'detection', 'frame_id': frame_id,
                           **common,
                           'captured_at': stamp, 'processed_at': stamp + 0.01,
                           'prep_ms': 1, 'infer_ms': 8, 'post_ms': 1})
            frames.append({'record_type': 'output', 'frame_id': frame_id, 'captured_at': stamp,
                           **common, 'processed_at': stamp + .01, 'input_size': [32, 32],
                           'image_size': [32, 24], 'conf': .35, 'nms': .45, 'skip': 0,
                           'class_names': ['hat'],
                           'output_at': stamp + latency / 1000, 'callback_finished_at': stamp + latency / 1000 + .003,
                           'capture_to_output_ms': latency, 'capture_to_sink_complete_ms': latency + 3,
                           'sink_ms': 3, 'valid': True, 'valid_at_dispatch': True,
                           'valid_at_sink_complete': True, 'state': 'healthy', 'alarm_state': 'clear'})
        resources = [{'monotonic': stamp, 'cpu_percent': 12, 'rss_bytes': 1000000,
                      'temperature_c': {'t': 50}, 'npu_frequency_hz': {'n': 400000000}}
                     for stamp in (10.0, 11.0, 12.0)]
        events = [{'type': 'state', 'state': 'startup', 'monotonic': 10, 'valid': False, 'alarm_state': 'unknown'},
                  {'type': 'state', 'state': 'healthy', 'monotonic': 10.02, 'valid': True, 'alarm_state': 'clear'},
                  {'type': 'state', 'state': 'stop', 'monotonic': 12.1, 'valid': False, 'alarm_state': 'unknown'}]
        return manifest, session, frames, resources, events

    def engine_fault(self):
        manifest, session, frames, resources, events = self.evidence()
        manifest.update(kind='fault-case', scenario='engine-exception', exit_code=1,
                        injected_exceptions=1, actual_engine_calls=1, elapsed_seconds=1.1, fault_at=1)
        session.update(state='fault', elapsed_seconds=1.01)
        session['counters'].update(captured=2, consumed=2, detected=1, output=1, detection_failure=1)
        events[-1].update(state='fault', monotonic=11.0001, reason='injected inference failure', alarm_state='unknown', valid=False)
        events.insert(-1, {'type': 'injection', 'scenario': 'engine-exception', 'phase': 'raise',
                          'monotonic': 11, 'actual_engine_calls': 1})
        return manifest, session, frames[:2], resources, events

    def source_fault(self):
        manifest, session, frames, resources, events = self.evidence()
        manifest.update(kind='fault-case', scenario='source-interruption', actual_source_reads=4,
                        injected_read_failures=2, recovered=True, fault_at=.5, recover_at=1.5)
        session['counters']['read_failure'] = 2
        for row in frames[2:4]:
            row['captured_at'] = 10.4
            row['processed_at'] = 10.41
        frames[3].update(output_at=10.43, callback_finished_at=10.433)
        for row in frames[4:]:
            row['source_epoch'] = 2
        events[2:2] = [
            {'type': 'injection', 'scenario': 'source-interruption', 'phase': 'begin', 'monotonic': 10.5},
            {'type': 'state', 'state': 'degraded', 'monotonic': 10.5001, 'valid': False, 'alarm_state': 'unknown',
             'reason': 'source read failure'},
            {'type': 'injection', 'scenario': 'source-interruption', 'phase': 'recovered', 'monotonic': 11.5},
            {'type': 'state', 'state': 'healthy', 'monotonic': 11.94, 'valid': True, 'alarm_state': 'clear'},
        ]
        return manifest, session, frames, resources, events

    def test_valid_replay_evidence_passes_without_claiming_camera_or_ssh(self):
        result = self.module().validate_longrun(*self.evidence())
        self.assertTrue(result['passed'], result)
        self.assertFalse(result['physical_camera_tested'])
        self.assertEqual(result['ssh_survival'], 'not_observed')
        self.assertAlmostEqual(result['metrics']['capture_to_output_ms']['p99'], 39.8)

    def test_short_duration_missing_hardware_samples_and_provenance_are_not_accepted(self):
        module = self.module()
        evidence = self.evidence()
        evidence[0]['requested_duration_seconds'] = 1800
        evidence[0]['model']['sha256'] = None
        evidence[3].clear()
        result = module.validate_longrun(*evidence)
        self.assertFalse(result['passed'])
        self.assertTrue(any('duration' in item for item in result['errors']))
        self.assertTrue(any('sha256' in item for item in result['errors']))
        self.assertTrue(any('resource' in item for item in result['errors']))

    def test_reused_frame_ids_and_stale_healthy_outputs_are_rejected(self):
        evidence = self.evidence()
        evidence[2][-1]['frame_id'] = 1
        evidence[2][-1]['valid_at_sink_complete'] = False
        result = self.module().validate_longrun(*evidence)
        self.assertFalse(result['passed'])
        self.assertTrue(any('duplicate' in item for item in result['errors']))
        self.assertTrue(any('stale' in item for item in result['errors']))

    def test_inference_fault_requires_real_delegation_nonzero_exit_and_unknown_alarm(self):
        module = self.module()
        manifest, session, frames, resources, events = self.engine_fault()
        result = module.validate_fault_case(manifest, session, frames, events)
        self.assertTrue(result['passed'], result)
        manifest['actual_engine_calls'] = 0
        self.assertFalse(module.validate_fault_case(manifest, session, frames, events)['passed'])
        manifest['actual_engine_calls'] = 1
        events[-1]['alarm_state'] = 'active'
        self.assertFalse(module.validate_fault_case(manifest, session, frames, events)['passed'])

    def test_source_fault_requires_degraded_gap_recovery_and_no_valid_or_alarm_output_in_gap(self):
        module = self.module()
        manifest, session, frames, resources, events = self.source_fault()
        frames[3].update(output_at=10.75, callback_finished_at=10.753,
                         capture_to_output_ms=350, capture_to_sink_complete_ms=353)
        self.assertFalse(module.validate_fault_case(manifest, session, frames, events)['passed'])
        frames[3].update(valid=False, valid_at_dispatch=False, valid_at_sink_complete=False,
                         state='degraded', alarm_state='unknown')
        self.assertTrue(module.validate_fault_case(manifest, session, frames, events)['passed'])
        events.insert(3, {'type': 'alarm', 'monotonic': 11.0, 'metadata': {'valid': True}})
        self.assertFalse(module.validate_fault_case(manifest, session, frames, events)['passed'])

    def test_governor_is_restored_when_run_body_fails(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'governor'
            path.write_text('ondemand')
            guard = module.GovernorGuard([path], requested='performance')
            with self.assertRaisesRegex(RuntimeError, 'run failure'):
                with guard:
                    self.assertEqual(path.read_text().strip(), 'performance')
                    raise RuntimeError('run failure')
            self.assertEqual(path.read_text().strip(), 'ondemand')
            self.assertTrue(guard.records[0]['restore_success'])

    def test_fault_wrappers_delegate_real_io_and_inject_only_declared_window(self):
        module = self.module()
        now = [0.0]
        class Engine:
            def infer(self, image):
                return ['real-result', image]
            def release(self):
                pass
        engine = module.FaultEngine(Engine(), fail_after=1, clock=lambda: now[0])
        self.assertEqual(engine.infer('rgb'), ['real-result', 'rgb'])
        now[0] = 1.1
        with self.assertRaisesRegex(RuntimeError, 'injected inference failure'):
            engine.infer('rgb')
        self.assertEqual(engine.actual_engine_calls, 1)
        now[0] = 0
        class Source:
            def read(self, stop_event=None):
                return True, 'real-frame'
            def release(self):
                pass
        source = module.InterruptSource(Source(), begin=1, end=2, clock=lambda: now[0])
        self.assertEqual(source.read(), (True, 'real-frame'))
        now[0] = 1.1
        self.assertEqual(source.read(), (False, None))
        now[0] = 2.1
        self.assertEqual(source.read(), (True, 'real-frame'))
        self.assertTrue(source.recovered)

    def test_help_runs_without_rknn_or_board_access(self):
        self.module()
        result = subprocess.run([sys.executable, str(SCRIPT), '--help'], capture_output=True,
                                text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('longrun', result.stdout)
        self.assertIn('faultsuite', result.stdout)

    def test_analysis_generates_resource_latency_figures_and_quantiles(self):
        module = self.module()
        manifest, session, frames, resources, events = self.evidence()
        with tempfile.TemporaryDirectory() as folder:
            result = module.plot_analysis(frames, resources, Path(folder))
            self.assertAlmostEqual(result['capture_to_output_ms']['p99'], 39.8)
            self.assertGreater(Path(folder, 'latency.png').stat().st_size, 1000)
            self.assertGreater(Path(folder, 'resources.png').stat().st_size, 1000)

    def test_invalid_resource_values_and_non_hex_source_hashes_are_rejected(self):
        evidence = self.evidence()
        evidence[0]['sources']['sources.py']['sha256'] = 'x' * 64
        for row in evidence[3]:
            row.update(cpu_percent=float('nan'), rss_bytes=-1,
                       temperature_c={'t': float('nan')}, npu_frequency_hz={'n': -1})
        result = self.module().validate_longrun(*evidence)
        self.assertFalse(result['passed'])
        self.assertTrue(any('resource' in item for item in result['errors']))
        self.assertTrue(any('sha256' in item for item in result['errors']))

    def test_invalid_resource_tail_cannot_fill_valid_continuous_coverage(self):
        module = self.module()
        invalid_values = {'rss_bytes': -1, 'cpu_percent': -1,
                          'temperature_c': {'t': float('nan')}, 'npu_frequency_hz': {'n': 0}}
        for field, invalid in invalid_values.items():
            with self.subTest(field=field):
                manifest, session, frames, resources, events = self.evidence()
                manifest.update(requested_duration_seconds=10, elapsed_seconds=10.15, actual_engine_calls=11)
                session['elapsed_seconds'] = 10.1
                session['counters'].update(captured=11, consumed=11, detected=11, output=11)
                template = copy.deepcopy(frames[:2])
                frames.clear()
                for index in range(11):
                    detection, output = copy.deepcopy(template)
                    stamp = 10 + index
                    for row in (detection, output):
                        row.update(frame_id=index + 1, captured_at=stamp, processed_at=stamp + .01)
                    output.update(output_at=stamp + .02, callback_finished_at=stamp + .023,
                                  capture_to_output_ms=20, capture_to_sink_complete_ms=23)
                    frames.extend((detection, output))
                resources[:] = [{'monotonic': 10 + index, 'cpu_percent': 12, 'rss_bytes': 1000000,
                                 'temperature_c': {'t': 50}, 'npu_frequency_hz': {'n': 400000000}}
                                for index in range(11)]
                for row in resources[2:]:
                    row[field] = invalid
                events[-1]['monotonic'] = 20.1
                result = module.validate_longrun(manifest, session, frames, resources, events)
                self.assertFalse(result['passed'], result)
                coverage = result['timeline']['streams']['resource ' + field]
                self.assertEqual(coverage['count'], 2)
                self.assertTrue(any('coverage' in error and field in error for error in result['errors']))

    def test_resource_field_shapes_and_boolean_measurements_are_not_numeric_evidence(self):
        module = self.module()
        for field, value in (('rss_bytes', True), ('rss_bytes', {'rss': 1}), ('cpu_percent', False),
                             ('cpu_percent', float('inf')), ('temperature_c', {'t': True}),
                             ('npu_frequency_hz', {'n': -1})):
            with self.subTest(field=field, value=value):
                self.assertFalse(module.resource_value_valid(value, field))

    def test_mismatched_detection_capture_and_degraded_longrun_are_rejected(self):
        evidence = self.evidence()
        evidence[2][-1]['captured_at'] = 10.5
        evidence[4].insert(2, {'type': 'state', 'state': 'degraded', 'monotonic': 11,
                              'valid': False, 'alarm_state': 'unknown'})
        result = self.module().validate_longrun(*evidence)
        self.assertFalse(result['passed'])
        self.assertTrue(any('capture' in item for item in result['errors']))
        self.assertTrue(any('degraded' in item for item in result['errors']))

    def test_unhashed_or_tampered_artifacts_are_not_accepted(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'manifest.json').write_text(json.dumps({'kind': 'longrun', 'artifacts': {}}))
            (root / 'session.json').write_text('{}')
            self.assertTrue(module.load_evidence(root)[-1], 'unhashed session evidence must be rejected')
            records = {}
            for name in ('session.json', 'frames.jsonl', 'events.jsonl', 'resources.jsonl'):
                if not (root / name).exists():
                    (root / name).write_text('')
                records[name] = module.file_record(root / name)
            (root / 'manifest.json').write_text(json.dumps({'kind': 'longrun', 'artifacts': records}))
            self.assertEqual(module.load_evidence(root)[-1], [])
            (root / 'session.json').write_text('{"changed":true}')
            self.assertTrue(any('sha256' in item for item in module.load_evidence(root)[-1]))

    def test_faultsuite_parent_failure_and_wrong_process_exit_cannot_be_hidden_by_case_passes(self):
        module = self.module()
        self.assertTrue(callable(getattr(module, 'validate_suite', None)), 'parent/process exit acceptance rules missing')
        names = ('engine-exception', 'source-interruption', 'bad-model', 'bad-source')
        children = [{'scenario': name, 'passed': True, 'errors': []} for name in names]
        manifest = {'exit_code': 0, 'governors': [{'before': 'ondemand', 'after': 'ondemand', 'restore_success': True}],
                    'cases': [{'scenario': name, 'child_exit_code': 0 if name == 'source-interruption' else 1,
                               'expected_exit_code': 0 if name == 'source-interruption' else 1} for name in names]}
        self.assertTrue(module.validate_suite(manifest, children)['passed'])
        manifest['exit_code'] = 1
        self.assertFalse(module.validate_suite(manifest, children)['passed'])
        manifest['exit_code'] = 0
        manifest['cases'][0]['child_exit_code'] = 124
        self.assertFalse(module.validate_suite(manifest, children)['passed'])

    def test_v1_ssh_observation_requires_verified_live_command_and_operator_disconnection_note(self):
        module = self.module()
        self.assertTrue(callable(getattr(module, 'validate_observation', None)), 'v1 observation verification missing')
        manifest = {'pid': 17, 'started_monotonic': 10}
        observation = {'pid': 17, 'alive': True, 'start_id': '100', 'monotonic': 20,
                       'identity_command_matches_manifest': True, 'operator_asserted_disconnection': True,
                       'note': 'Fresh SSH session after closing launch session'}
        self.assertTrue(module.validate_observation(manifest, observation))
        observation['identity_command_matches_manifest'] = False
        self.assertFalse(module.validate_observation(manifest, observation))
        observation['identity_command_matches_manifest'] = True
        observation['operator_asserted_disconnection'] = False
        self.assertFalse(module.validate_observation(manifest, observation))

    def test_declared_1800_seconds_cannot_replace_two_seconds_of_timeline(self):
        evidence = self.evidence()
        evidence[0]['requested_duration_seconds'] = 1800
        evidence[1]['elapsed_seconds'] = 1800.1
        result = self.module().validate_longrun(*evidence)
        self.assertFalse(result['passed'])
        self.assertTrue(any('duration' in error or 'coverage' in error for error in result['errors']))

    def test_long_sampling_gaps_and_resource_tail_missing_are_rejected(self):
        evidence = self.evidence()
        evidence[0].update(requested_duration_seconds=1800, elapsed_seconds=1800.2)
        evidence[1]['elapsed_seconds'] = 1800.1
        evidence[4][-1]['monotonic'] = 1810.1
        for row in evidence[2][-2:]:
            row.update(captured_at=1809.9, processed_at=1809.91)
        evidence[2][-1].update(output_at=1809.94, callback_finished_at=1809.943)
        result = self.module().validate_longrun(*evidence)
        self.assertFalse(result['passed'])
        self.assertTrue(any('gap' in error for error in result['errors']))
        self.assertTrue(any('resource' in error for error in result['errors']))

    def test_detection_and_valid_output_after_terminal_stop_are_rejected(self):
        evidence = self.evidence()
        evidence[0]['replay_fps'] = evidence[1]['replay_fps'] = 2
        for row in evidence[2][-2:]:
            row.update(captured_at=12.2, processed_at=12.21)
        evidence[2][-1].update(output_at=12.24, callback_finished_at=12.243)
        result = self.module().validate_longrun(*evidence)
        self.assertFalse(result['passed'], result)
        self.assertTrue(any('active interval' in error for error in result['errors']))

    def test_zero_capture_counts_cannot_accompany_completed_inferences(self):
        evidence = self.evidence()
        evidence[1]['counters'].update(captured=0, consumed=0)
        self.assertFalse(self.module().validate_longrun(*evidence)['passed'])

    def test_cross_record_model_hash_dimensions_classes_and_thresholds_must_agree(self):
        evidence = self.evidence()
        evidence[1]['model_sha256'] = 'd' * 64
        for row in evidence[2]:
            row.update(model='other.rknn', model_sha256='e' * 64)
        evidence[2][-1].update(input_size=[640, 384], class_order=['hat', 'person'], conf=.25)
        result = self.module().validate_longrun(*evidence)
        self.assertFalse(result['passed'])
        self.assertTrue(any('model' in error for error in result['errors']))
        self.assertTrue(any('threshold' in error for error in result['errors']))

    def test_extra_real_calls_and_detector_processed_before_capture_are_rejected(self):
        evidence = self.evidence()
        evidence[0]['actual_engine_calls'] = 4
        evidence[2][0]['processed_at'] = 9
        self.assertFalse(self.module().validate_longrun(*evidence)['passed'])

    def test_actual_source_recovery_marker_prevents_premature_healthy_acceptance(self):
        manifest, session, frames, resources, events = self.source_fault()
        for event in events:
            if event.get('phase') == 'recovered':
                event['monotonic'] = 12
        result = self.module().validate_fault_case(manifest, session, frames, events)
        self.assertFalse(result['passed'])

    def test_misleading_degraded_flags_and_absent_post_recovery_output_are_rejected(self):
        manifest, session, frames, resources, events = self.source_fault()
        degraded = next(event for event in events if event.get('state') == 'degraded')
        degraded.update(valid=True, alarm_state='active')
        self.assertFalse(self.module().validate_fault_case(manifest, session, frames, events)['passed'])
        degraded.update(valid=False, alarm_state='unknown')
        frames = frames[:4]
        manifest['actual_engine_calls'] = 2
        session['counters'].update(captured=2, consumed=2, detected=2, output=2)
        self.assertFalse(self.module().validate_fault_case(manifest, session, frames, events)['passed'])

    def test_source_recovery_cannot_reuse_old_frame_identity_or_epoch(self):
        manifest, session, frames, resources, events = self.source_fault()
        for row in frames[-2:]:
            row.update(frame_id=1, captured_at=10, source_epoch=0)
        self.assertFalse(self.module().validate_fault_case(manifest, session, frames, events)['passed'])

    def test_expected_exception_does_not_mask_cleanup_error_or_alive_worker(self):
        manifest, session, frames, resources, events = self.engine_fault()
        manifest['cleanup_errors'] = ['engine release failed']
        self.assertFalse(self.module().validate_fault_case(manifest, session, frames, events)['passed'])
        manifest['cleanup_errors'] = []
        manifest['cleanup']['engine_released'] = False
        manifest['cleanup']['detect_worker_alive'] = True
        self.assertFalse(self.module().validate_fault_case(manifest, session, frames, events)['passed'])

    def test_legacy_missing_direct_fault_cleanup_is_incomplete_not_invented(self):
        manifest, session, frames, resources, events = self.engine_fault()
        manifest.pop('cleanup')
        result = self.module().validate_fault_case(manifest, session, frames, events)
        self.assertFalse(result['passed'])
        self.assertTrue(result['primary_fault_verified'])
        self.assertFalse(result['cleanup_evidence']['direct_recorded'])

    def test_real_inference_requires_initialized_resources_and_sdk_release_receipts(self):
        module = self.module()
        manifest, session, frames, resources, events = self.engine_fault()
        manifest['cleanup'].pop('sdk_resources', None)
        self.assertFalse(module.validate_fault_case(manifest, session, frames, events)['passed'])
        manifest['cleanup'].update(engine_initialized=False, source_initialized=False,
                                   engine_released=None, source_released=None, sdk_resources=[])
        result = module.validate_fault_case(manifest, session, frames, events)
        self.assertFalse(result['passed'], result)
        self.assertTrue(any('engine' in error for error in result['cleanup_evidence']['errors']))
        self.assertTrue(any('source' in error for error in result['cleanup_evidence']['errors']))
        self.assertTrue(any('SDK' in error for error in result['cleanup_evidence']['errors']))

    def test_partial_bad_model_init_requires_sdk_receipt_even_without_complete_engine(self):
        module = self.module()
        manifest, session, frames, resources, events = self.engine_fault()
        manifest.update(scenario='bad-model', error='pipeline: init_runtime failed', actual_engine_calls=0,
                        injected_exceptions=0, baseline_model={'sha256': 'a' * 64},
                        model={'path': 'corrupt.rknn', 'sha256': 'e' * 64, 'bytes': 46},
                        malformed_model_file_verified=True)
        session.update(model='corrupt.rknn', model_sha256=None)
        for name in session['counters']:
            session['counters'][name] = 0
        manifest['cleanup'].update(engine_initialized=False, engine_released=None, sdk_resources=[])
        events = [events[0], dict(events[-1], reason='init_runtime failed')]
        result = module.validate_fault_case(manifest, session, [], events)
        self.assertTrue(result['primary_fault_verified'], result)
        self.assertFalse(result['passed'], result)
        self.assertTrue(any('SDK' in error for error in result['cleanup_evidence']['errors']))

    def test_sdk_receipt_type_and_complete_success_fields_are_required(self):
        module = self.module()
        for receipts in (None, {}, [], [None], [{'created': True, 'released': True}],
                         [{'created': False, 'released': True, 'release_error': None}],
                         [{'created': True, 'released': False, 'release_error': None}]):
            with self.subTest(receipts=receipts):
                manifest, session, frames, resources, events = self.engine_fault()
                manifest['cleanup']['sdk_resources'] = receipts
                self.assertFalse(module.validate_fault_case(manifest, session, frames, events)['passed'])

    def test_pending_final_output_conserves_counts_after_real_detection(self):
        manifest, session, frames, resources, events = self.source_fault()
        final = dict(frames[-2], frame_id=4, captured_at=12, processed_at=12.01)
        frames.append(final)
        manifest['actual_engine_calls'] = 4
        manifest['actual_source_reads'] = 5
        session['counters'].update(captured=4, consumed=4, detected=4, pending_output=1)
        result = self.module().validate_fault_case(manifest, session, frames, events)
        self.assertTrue(result['passed'], result)

    def test_malformed_model_init_failure_needs_verified_tiny_payload_evidence(self):
        manifest, session, frames, resources, events = self.engine_fault()
        manifest.update(scenario='bad-model', error='pipeline: init_runtime failed', actual_engine_calls=0,
                        injected_exceptions=0, baseline_model={'sha256': 'a' * 64},
                        model={'path': 'corrupt.rknn', 'sha256': 'e' * 64, 'bytes': 46})
        session.update(model='corrupt.rknn', model_sha256=None)
        for name in session['counters']:
            session['counters'][name] = 0
        manifest['cleanup'].update(engine_initialized=False, engine_released=None)
        events = [events[0], dict(events[-1], reason='init_runtime failed')]
        self.assertFalse(self.module().validate_fault_case(manifest, session, [], events)['passed'])
        manifest['malformed_model_file_verified'] = True
        result = self.module().validate_fault_case(manifest, session, [], events)
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['startup_failure_stage'], 'runtime-init')

    def test_missing_governor_readback_is_not_successful_restoration(self):
        evidence = self.evidence()
        evidence[0]['governors'] = []
        self.assertFalse(self.module().validate_longrun(*evidence)['passed'])

    def test_source_reaction_retains_announcement_violation_without_failing_observed_contract(self):
        manifest, session, frames, resources, events = self.source_fault()
        degraded = next(event for event in events if event.get('state') == 'degraded')
        degraded['monotonic'] = 10.51
        frames[3].update(output_at=10.505, callback_finished_at=10.508,
                         capture_to_output_ms=105, capture_to_sink_complete_ms=108)
        result = self.module().validate_fault_case(manifest, session, frames, events)
        self.assertTrue(result['passed'], result)
        reaction = result['source_reaction_evidence']
        self.assertFalse(reaction['announced_gap_zero_latency'])
        self.assertTrue(reaction['observed_failed_read_contract'])
        self.assertAlmostEqual(reaction['reaction_ms'], 10)
        self.assertEqual(reaction['announcement_violations']['output_frame_ids'], [2])
        frames[3].update(output_at=10.515, callback_finished_at=10.518,
                         capture_to_output_ms=115, capture_to_sink_complete_ms=118)
        result = self.module().validate_fault_case(manifest, session, frames, events)
        self.assertFalse(result['passed'])
        self.assertFalse(result['source_reaction_evidence']['observed_failed_read_contract'])

    def test_wrapper_shutdown_false_is_not_a_native_failed_read(self):
        module = self.module()
        stop = threading.Event()
        class Source:
            def read(self, stop_event=None):
                stop_event.set()
                return False, None
            def release(self):
                pass
        for wrapper in (module.RecordingSource, module.InterruptSource):
            stop.clear()
            source = wrapper(Source())
            self.assertEqual(source.read(stop), (False, None))
            self.assertEqual(source.actual_source_reads, 1)
            self.assertEqual(source.actual_source_read_failures, 0)

    def test_source_marker_equal_capture_timestamp_requires_causal_epoch_checks(self):
        manifest, session, frames, resources, events = self.source_fault()
        for row in frames[2:4]:
            row.update(captured_at=10.5, processed_at=10.50001)
        frames[2].update(prep_ms=0, infer_ms=0, post_ms=0)
        frames[3].update(output_at=10.50002, callback_finished_at=10.50003,
                         capture_to_output_ms=.02, capture_to_sink_complete_ms=.03, sink_ms=.01)
        result = self.module().validate_fault_case(manifest, session, frames, events)
        self.assertTrue(result['passed'], result)
        for row in frames[2:4]:
            row.update(captured_at=10.6, processed_at=10.60001)
        frames[3].update(output_at=10.60002, callback_finished_at=10.60003,
                         valid=False, valid_at_dispatch=False, valid_at_sink_complete=False,
                         state='degraded', alarm_state='unknown')
        self.assertFalse(self.module().validate_fault_case(manifest, session, frames, events)['passed'])

    def test_class_names_and_invalid_output_flags_cannot_disagree_with_packet(self):
        module = self.module()
        evidence = self.evidence()
        evidence[2][-1]['class_names'] = ['person']
        self.assertFalse(module.validate_longrun(*evidence)['passed'])
        evidence[2][-1]['class_names'] = ['hat']
        evidence[2][-1]['valid'] = False
        errors, _, _ = module.evidence_inventory(evidence[0], evidence[1], evidence[2], evidence[4])
        self.assertTrue(any('invalid output' in error for error in errors), errors)

    def test_cleanup_collector_retains_secondary_release_failure_and_live_workers(self):
        import queue
        module = self.module()
        class Resource:
            def release(self):
                raise RuntimeError('release failed after expected primary fault')
        engine = module.RecordingEngine(Resource())
        source = module.RecordingSource(Resource())
        for resource in (engine, source):
            with self.assertRaisesRegex(RuntimeError, 'release failed'):
                resource.release()
        failures = queue.Queue()
        failures.put(RuntimeError('secondary worker failure'))
        worker = SimpleNamespace(is_alive=lambda: True)
        pipeline = SimpleNamespace(errors=failures, capture_thread=worker, detect_thread=None)
        manifest = {'error': 'injected primary fault'}
        module.collect_cleanup(manifest, pipeline, engine, source)
        result = module.cleanup_evidence(manifest, require_direct=True)
        self.assertFalse(result['accepted'])
        self.assertTrue(manifest['cleanup']['capture_worker_alive'])
        self.assertTrue(any('secondary worker failure' in error for error in result['errors']))
        self.assertFalse(manifest['cleanup']['engine_released'])

    def test_sdk_release_failure_remains_a_cleanup_failure_on_expected_exit(self):
        import types
        module = self.module()
        class SDK:
            def release(self):
                raise RuntimeError('SDK release failed')
        api = types.ModuleType('rknnlite.api')
        api.RKNNLite = SDK
        manifest = {'exit_code': 1, 'error': 'injected primary fault'}
        with patch.dict(sys.modules, {'rknnlite': types.ModuleType('rknnlite'), 'rknnlite.api': api}):
            with module.SDKLifecycleMonitor() as monitor:
                resource = api.RKNNLite()
                with self.assertRaisesRegex(RuntimeError, 'SDK release failed'):
                    resource.release()
            module.collect_cleanup(manifest, None, None, None, monitor.records)
        self.assertIs(api.RKNNLite, SDK)
        self.assertFalse(module.cleanup_evidence(manifest, require_direct=True)['accepted'])
        self.assertTrue(any('SDK release failed' in error for error in manifest['cleanup_errors']))

    def test_real_pipeline_bad_model_records_partial_sdk_release_locally(self):
        import contextlib
        import io
        import types
        import cv2
        import numpy as np
        from helmet import pipeline_runtime as runtime
        module = self.module()
        class SDK:
            def __init__(self, **kwargs):
                pass
            def load_rknn(self, path):
                return 0
            def init_runtime(self):
                return -1
            def release(self):
                pass
        api = types.ModuleType('rknnlite.api')
        api.RKNNLite = SDK
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / 'helmet_32x32.rknn'
            model.write_bytes(b'local fake-SDK model fixture')
            replay = root / 'replay.avi'
            writer = cv2.VideoWriter(str(replay), cv2.VideoWriter_fourcc(*'MJPG'), 10, (32, 24))
            self.assertTrue(writer.isOpened())
            writer.write(np.zeros((24, 32, 3), np.uint8))
            writer.release()
            args = SimpleNamespace(command='_case', output_root=str(root), run_id='local-bad-model',
                                   model=str(model), source=str(replay), runtime_dir=str(SCRIPT.parents[3] / 'helmet'),
                                   scenario='bad-model', governor=None, duration=.1, replay_fps=10,
                                   result_ttl=1, fault_at=.02, recover_at=.07,
                                   input_description='local software test fixture; fake SDK')
            with patch.dict(sys.modules, {'rknnlite': types.ModuleType('rknnlite'), 'rknnlite.api': api}), \
                    patch.object(module, 'runtime_module', return_value=runtime), \
                    patch.object(module, 'GovernorGuard', return_value=module.GovernorGuard([])), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.run_fault_case(args), 1)
            manifest = json.loads((root / 'local-bad-model' / 'manifest.json').read_text())
            cleanup = manifest['cleanup']
            self.assertTrue(cleanup['source_released'])
            self.assertFalse(cleanup['engine_initialized'])
            self.assertEqual(cleanup['sdk_resources'], [{'created': True, 'released': True, 'release_error': None}])
            self.assertTrue(module.cleanup_evidence(manifest, require_direct=True)['accepted'])
            self.assertIs(api.RKNNLite, SDK, 'verification monitor must restore the SDK factory')

    @patch('time.monotonic', new=time.perf_counter)
    def test_real_pipeline_fault_wrappers_record_direct_cleanup_locally(self):
        # This lifecycle test needs ordered local timestamps, rather than the
        # Windows coarse tick clock assigning dispatch/failure one equal value.
        # It does not relax the real evidence checker's observed-failure gate.
        import contextlib
        import io
        import types
        import cv2
        import numpy as np
        from helmet import pipeline_runtime as runtime
        module = self.module()
        class SDK:
            def __init__(self, **kwargs):
                pass
            def load_rknn(self, path):
                return 0
            def init_runtime(self):
                return 0
            def inference(self, inputs, data_format):
                return [np.full((1, 21, size, size), -20, np.float32) for size in (4, 2, 1)]
            def release(self):
                pass
        api = types.ModuleType('rknnlite.api')
        api.RKNNLite = SDK
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / 'helmet_32x32.rknn'
            model.write_bytes(b'local fake-SDK model fixture')
            replay = root / 'replay.avi'
            writer = cv2.VideoWriter(str(replay), cv2.VideoWriter_fourcc(*'MJPG'), 100, (32, 24))
            self.assertTrue(writer.isOpened())
            for _ in range(5):
                writer.write(np.zeros((24, 32, 3), np.uint8))
            writer.release()
            governor = root / 'local-governor-fixture'
            governor.write_text('ondemand')
            for scenario in ('engine-exception', 'source-interruption', 'bad-source'):
                args = SimpleNamespace(command='_case', output_root=str(root), run_id=scenario,
                                       model=str(model), source=str(replay), runtime_dir=str(SCRIPT.parents[3] / 'helmet'),
                                       scenario=scenario, governor=None, duration=.3, replay_fps=100,
                                       result_ttl=1, fault_at=.04, recover_at=.16,
                                       input_description='local software test fixture; fake SDK')
                guard = module.GovernorGuard([governor])
                with self.subTest(scenario=scenario), \
                        patch.dict(sys.modules, {'rknnlite': types.ModuleType('rknnlite'), 'rknnlite.api': api}), \
                        patch.object(module, 'runtime_module', return_value=runtime), \
                        patch.object(module, 'GovernorGuard', return_value=guard), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.run_fault_case(args), 0 if scenario == 'source-interruption' else 1)
                    manifest, session, frames, resources, events, errors = module.load_evidence(root / scenario)
                    self.assertEqual(errors, [])
                    self.assertTrue(manifest['validation']['passed'], manifest['validation'])
                    self.assertFalse(manifest['cleanup']['capture_worker_alive'])
                    self.assertFalse(manifest['cleanup']['detect_worker_alive'])
                    if scenario == 'bad-source':
                        self.assertEqual(manifest['cleanup']['sdk_resources'], [])
                    else:
                        self.assertTrue(manifest['cleanup']['engine_released'])
                        self.assertTrue(manifest['cleanup']['source_released'])
                        self.assertTrue(manifest['cleanup']['sdk_resources'][0]['released'])
                    self.assertIs(api.RKNNLite, SDK)


if __name__ == '__main__':
    unittest.main()
