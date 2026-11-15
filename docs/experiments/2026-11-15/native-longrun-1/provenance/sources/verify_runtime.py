#!/usr/bin/env python3
"""Real RKNN replay verification. Run on the board; inspect artifacts locally.

This runner measures file replay, not camera exposure or browser rendering.
Only the coordinating root starts board/NPU commands. See --help.
"""
import argparse
from datetime import datetime, timezone
import glob
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from types import SimpleNamespace


SOURCE_FILES = ('pipeline_runtime.py', 'sources.py', 'telemetry.py', 'alarm.py', 'yolo_post.py')
DEFAULT_ROOT = '/userdata/helmet-validation/20261114'

# Prespecified evidence policy, not fitted to any measured run. Capture/output
# gaps are bounded by one second or five configured replay periods. Independent
# 1-second resource sampling permits three missed sampling periods. Model/SDK
# startup and final bookkeeping have separate allowances and cannot substitute
# for the requested active timeline.
EVIDENCE_POLICY = {'startup_allowance_seconds': 10.0, 'finalization_allowance_seconds': 5.0,
                   'session_event_agreement_seconds': 1.0, 'duration_rounding_seconds': .1,
                   'resource_interval_seconds': 1.0, 'resource_gap_periods': 3.0,
                   'frame_gap_periods': 5.0, 'minimum_frame_gap_seconds': 1.0,
                   'timing_rounding_ms': .02}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
    os.replace(temporary, path)


def file_record(path):
    path = Path(path).resolve()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return {'path': str(path), 'sha256': digest.hexdigest(), 'bytes': path.stat().st_size}


def read_jsonl(path):
    with Path(path).open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def percentiles(values):
    values = sorted(float(value) for value in values)
    if not values:
        return {'count': 0, 'mean': None, 'p50': None, 'p95': None, 'p99': None}
    def percentile(fraction):
        index = (len(values) - 1) * fraction
        low = math.floor(index)
        high = math.ceil(index)
        return values[low] + (values[high] - values[low]) * (index - low)
    return {'count': len(values), 'mean': sum(values) / len(values),
            'p50': percentile(.5), 'p95': percentile(.95), 'p99': percentile(.99)}


def provenance_errors(manifest):
    errors = []
    for name in ('model', 'input'):
        digest = manifest.get(name, {}).get('sha256')
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
            errors.append(name + ' sha256 missing/invalid')
    for name in (*SOURCE_FILES, 'verify_runtime.py'):
        digest = manifest.get('sources', {}).get(name, {}).get('sha256')
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
            errors.append(name + ' source sha256 missing/invalid')
    if manifest.get('backend') != 'rknnlite':
        errors.append('real rknnlite backend evidence missing')
    if not manifest.get('governors'):
        errors.append('NPU governor read-back inventory missing')
    for governor in manifest.get('governors', []):
        if not governor.get('restore_success') or governor.get('before') != governor.get('after'):
            errors.append('NPU governor was not restored')
    return errors


def is_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def resource_value_valid(value, field):
    """One numerical policy for measurements and their coverage timestamps.

    Process CPU can exceed 100% with multiple cores; it must remain finite and
    nonnegative. RSS is a positive scalar. Thermal/frequency mappings must have
    finite values, with positive frequencies; booleans are never measurements.
    """
    if field not in ('cpu_percent', 'rss_bytes', 'temperature_c', 'npu_frequency_hz'):
        return False
    values = list(value.values()) if isinstance(value, dict) and field in ('temperature_c', 'npu_frequency_hz') else [value]
    if not values or not all(is_number(item) for item in values):
        return False
    if field in ('rss_bytes', 'npu_frequency_hz'):
        return all(item > 0 for item in values)
    if field == 'cpu_percent':
        return all(item >= 0 for item in values)
    return True


def evidence_inventory(manifest, session, frames, events):
    """Cross-record coherence; applicable to completed runs and fault prefixes."""
    errors = []
    counters = session.get('counters', {})
    count_names = ('captured', 'consumed', 'detected', 'overwritten', 'active_skip', 'read_failure',
                   'displayed', 'output', 'output_overwritten', 'detection_failure', 'pending_capture', 'pending_output')
    for name in count_names:
        if type(counters.get(name)) is not int or counters[name] < 0:
            errors.append('missing/negative/noninteger counter: ' + name)
    if not errors:
        if counters['captured'] != counters['consumed'] + counters['overwritten'] + counters['pending_capture']:
            errors.append('captured/consumed/overwritten/pending conservation failed')
        if counters['consumed'] != counters['detected'] + counters['active_skip'] + counters['detection_failure']:
            errors.append('consumed/detected/skipped/failure conservation failed')
        if counters['detected'] != counters['output'] + counters['output_overwritten'] + counters['pending_output']:
            errors.append('detected/output/overwritten/pending conservation failed')
        if counters['displayed'] > counters['output']:
            errors.append('display counter exceeds distinct output inventory')
    detections = [row for row in frames if row.get('record_type') == 'detection']
    outputs = [row for row in frames if row.get('record_type') == 'output']
    if any(row.get('record_type') not in ('detection', 'output') for row in frames):
        errors.append('unknown frame record boundary')
    if len(detections) != counters.get('detected') or len(outputs) != counters.get('output'):
        errors.append('per-frame inventory disagrees with completed counters')
    injected = manifest.get('injected_exceptions', 0)
    expected_calls = counters.get('detected', 0) + counters.get('detection_failure', 0) - injected
    if type(manifest.get('actual_engine_calls')) is not int or manifest['actual_engine_calls'] != expected_calls or expected_calls < 0:
        errors.append('actual real engine call inventory disagrees with completed/failure/injected counters')
    model = manifest.get('model', {})
    model_hash = model.get('sha256')
    session_model = session.get('model')
    session_source = session.get('source')
    if not session_model or Path(str(session_model)).name != Path(str(model.get('path', ''))).name:
        errors.append('session model path disagrees with manifest')
    if not session_source or Path(str(session_source)).name != Path(str(manifest.get('input', {}).get('path', ''))).name:
        errors.append('session source path disagrees with manifest input')
    if detections and session.get('model_sha256') != model_hash:
        errors.append('session model sha256 disagrees with manifest')
    elif session.get('model_sha256') not in (None, model_hash):
        errors.append('startup session model sha256 disagrees with manifest')
    size = manifest.get('input_size')
    if not isinstance(size, list) or len(size) != 2 or any(type(value) is not int or value <= 0 for value in size):
        errors.append('manifest input dimensions missing/invalid')
    if session.get('input_size') != size:
        errors.append('session input dimensions disagree with manifest')
    classes = ['person', 'hat']
    if manifest.get('class_order') != classes or session.get('class_order') != classes:
        errors.append('manifest/session class order disagrees with person/hat contract')
    thresholds = manifest.get('thresholds', {})
    for name in ('conf', 'nms'):
        value = thresholds.get(name)
        if not is_number(value) or not 0 < value < 1 or session.get('thresholds', {}).get(name) != value:
            errors.append('manifest/session threshold mismatch: ' + name)
    if type(thresholds.get('skip')) is not int or thresholds['skip'] < 0:
        errors.append('skip threshold missing/invalid')
    for name in ('replay_fps', 'result_ttl'):
        if not is_number(manifest.get(name)) or manifest[name] <= 0 or session.get(name) != manifest[name]:
            errors.append('session/runtime configuration mismatch: ' + name)
    detection_by_id = {}
    for kind, rows in [('detection', detections), ('output', outputs)]:
        ids = [row.get('frame_id') for row in rows]
        if any(type(value) is not int or value < 1 or value > counters.get('captured', -1) for value in ids):
            errors.append('frame IDs outside positive capture inventory: ' + kind)
        if len(ids) != len(set(ids)) or any(a >= b for a, b in zip(ids, ids[1:]) if type(a) is int and type(b) is int):
            errors.append('duplicate/nonmonotonic frame IDs: ' + kind)
        for row in rows:
            if row.get('model') != session_model or row.get('model_sha256') != model_hash:
                errors.append('per-frame model path/hash mismatch')
            if row.get('source') != session_source or row.get('class_order') != classes:
                errors.append('per-frame source/class order mismatch')
            arrays = [row.get(name) for name in ('classes', 'scores', 'boxes')]
            if not all(isinstance(value, list) for value in arrays) or len({len(value) for value in arrays if isinstance(value, list)}) != 1:
                errors.append('detection arrays have inconsistent lengths/types')
            else:
                if any(type(value) is not int or value not in (0, 1) for value in arrays[0]):
                    errors.append('unknown per-frame class IDs')
                if any(not is_number(value) or not 0 <= value <= 1 for value in arrays[1]):
                    errors.append('invalid per-frame confidence')
                if any(not isinstance(box, list) or len(box) != 4 or any(not is_number(value) for value in box) for box in arrays[2]):
                    errors.append('invalid box layout/nonfinite coordinates')
            captured, processed = row.get('captured_at'), row.get('processed_at')
            if not is_number(captured) or not is_number(processed) or processed < captured:
                errors.append('invalid capture/processed timestamp ordering')
            if kind == 'detection':
                timing = [row.get(name) for name in ('prep_ms', 'infer_ms', 'post_ms')]
                if any(not is_number(value) or value < 0 for value in timing):
                    errors.append('invalid detector timing')
                elif is_number(captured) and is_number(processed) and sum(timing) > (processed - captured) * 1000 + EVIDENCE_POLICY['timing_rounding_ms']:
                    errors.append('detector timings exceed capture-to-processed interval')
                if 'input_size' in row and row['input_size'] != size:
                    errors.append('detector input dimensions mismatch')
                detection_by_id[row.get('frame_id')] = row
            else:
                if row.get('input_size') != size:
                    errors.append('output input dimensions mismatch')
                row_classes = row.get('classes')
                if isinstance(row_classes, list) and all(type(value) is int and value in (0, 1) for value in row_classes) and row.get('class_names') != [classes[value] for value in row_classes]:
                    errors.append('output class names disagree with class IDs')
                if any(type(row.get(name)) is not bool for name in ('valid', 'valid_at_dispatch', 'valid_at_sink_complete')):
                    errors.append('output validity flags missing/nonboolean')
                if row.get('valid') is False and (row.get('state') not in ('degraded', 'fault', 'stop') or row.get('alarm_state') != 'unknown'):
                    errors.append('misleading invalid output state/alarm flags')
                for name in ('conf', 'nms', 'skip'):
                    if row.get(name) != thresholds.get(name):
                        errors.append('output threshold mismatch: ' + name)
                if type(row.get('source_epoch')) is not int or row['source_epoch'] < 0 or row['source_epoch'] > counters.get('read_failure', 0):
                    errors.append('output source epoch inconsistent with read-failure inventory')
                dispatched, finished = row.get('output_at'), row.get('callback_finished_at')
                if not all(is_number(value) for value in (captured, processed, dispatched, finished)) or not captured <= processed <= dispatched <= finished:
                    errors.append('invalid output timestamp ordering')
                else:
                    for name, actual in [('capture_to_output_ms', (dispatched - captured) * 1000),
                                         ('capture_to_sink_complete_ms', (finished - captured) * 1000)]:
                        if not is_number(row.get(name)) or abs(row[name] - actual) > EVIDENCE_POLICY['timing_rounding_ms']:
                            errors.append('output latency/capture timestamp mismatch: ' + name)
                    if not is_number(row.get('sink_ms')) or row['sink_ms'] < 0 or row['sink_ms'] > (finished - dispatched) * 1000 + EVIDENCE_POLICY['timing_rounding_ms']:
                        errors.append('sink timing exceeds handoff/completion interval')
                    if row.get('valid_at_dispatch') and dispatched - captured > manifest.get('result_ttl', 1):
                        errors.append('stale dispatch marked valid')
                    if row.get('valid_at_sink_complete') and finished - captured > manifest.get('result_ttl', 1):
                        errors.append('stale completion marked valid')
        for key in ('captured_at', 'processed_at') if kind == 'detection' else ('captured_at', 'output_at', 'callback_finished_at'):
            stamps = [row.get(key) for row in rows]
            if all(is_number(value) for value in stamps) and any(a > b for a, b in zip(stamps, stamps[1:])):
                errors.append('nonmonotonic frame timestamp stream: ' + key)
    for row in outputs:
        detection = detection_by_id.get(row.get('frame_id'))
        if detection is None:
            errors.append('output has no completed detector packet')
        elif any(row.get(key) != detection.get(key) for key in ('captured_at', 'processed_at', 'model_sha256', 'classes', 'scores', 'boxes')):
            errors.append('output capture/model/detection binding mismatch')
        elif 'source_epoch' in detection and row.get('source_epoch') != detection['source_epoch']:
            errors.append('output/detector epoch mismatch')
    epochs = [row.get('source_epoch') for row in outputs]
    if all(type(value) is int for value in epochs) and any(a > b for a, b in zip(epochs, epochs[1:])):
        errors.append('source epochs moved backwards')
    for event in events:
        if not is_number(event.get('monotonic')):
            errors.append('nonfinite/missing event timestamp')
        if event.get('type') == 'state':
            state = event.get('state')
            if state in ('startup', 'degraded', 'fault', 'stop') and (event.get('valid') is not False or event.get('alarm_state') != 'unknown'):
                errors.append('misleading invalid state flags: ' + str(state))
            if state in ('healthy', 'no-detection') and event.get('valid') is not True:
                errors.append('fresh state lacks valid evidence')
    stamps = [event.get('monotonic') for event in events]
    if all(is_number(value) for value in stamps) and any(a > b for a, b in zip(stamps, stamps[1:])):
        errors.append('event timestamps out of journal order')
    full = session.get('full_session', {})
    for name in ('prep_ms', 'infer_ms', 'post_ms', 'capture_to_output_ms', 'sink_ms', 'capture_to_sink_complete_ms'):
        if name in full and full[name].get('count') != (len(detections) if name in ('prep_ms', 'infer_ms', 'post_ms') else len(outputs)):
            errors.append('session timing sample count differs from raw inventory: ' + name)
    return list(dict.fromkeys(errors)), detections, outputs


def cleanup_evidence(manifest, require_direct, session=None, startup_stage=None):
    errors = ['secondary cleanup failure: ' + str(error) for error in manifest.get('cleanup_errors', [])]
    record = manifest.get('cleanup')
    if not isinstance(record, dict):
        if require_direct:
            errors.append('direct worker/release cleanup evidence not recorded by legacy runner')
        return {'direct_recorded': False, 'accepted': False,
                'basis': 'legacy normal stop is indirect evidence only', 'errors': errors}
    for name in ('engine', 'source'):
        initialized = record.get(name + '_initialized')
        if type(initialized) is not bool or (initialized and record.get(name + '_released') is not True):
            errors.append('initialized resource was not verifiably released: ' + name)
        if initialized is False and record.get(name + '_released') is not None:
            errors.append('uninitialized resource has contradictory release receipt: ' + name)
    actual_calls = manifest.get('actual_engine_calls', 0)
    detected = (session or {}).get('counters', {}).get('detected', 0)
    real_engine_used = ((is_number(actual_calls) and actual_calls > 0)
                        or (is_number(detected) and detected > 0))
    scenario = manifest.get('scenario')
    complete_engine_required = real_engine_used or scenario in ('engine-exception', 'source-interruption')
    source_required = complete_engine_required or scenario == 'bad-model' or (
        is_number(manifest.get('actual_source_reads')) and manifest['actual_source_reads'] > 0)
    if complete_engine_required and (record.get('engine_initialized') is not True or record.get('engine_released') is not True):
        errors.append('real inference/scenario requires initialized and released engine receipt')
    if source_required and (record.get('source_initialized') is not True or record.get('source_released') is not True):
        errors.append('real reads/inference/scenario requires initialized and released source receipt')
    for name in ('capture_worker_alive', 'detect_worker_alive', 'shutdown_timeout'):
        if record.get(name) is not False:
            errors.append('worker shutdown failed/unrecorded: ' + name)
    receipts = record.get('sdk_resources')
    sdk_required = complete_engine_required or scenario == 'bad-model' or record.get('engine_initialized') is True
    if not isinstance(receipts, list):
        errors.append('direct SDK resource receipt inventory missing/not a list')
    else:
        if sdk_required and not receipts:
            errors.append('real inference/partial model initialization requires at least one SDK creation/release receipt')
        if not receipts and not sdk_required:
            # Pipeline creates the source before constructing RKNNEngine. Only
            # an actual recognized source-open/decode failure proves that no
            # SDK object was reached; False flags alone are insufficient.
            pre_sdk_source_failure = (scenario == 'bad-source' and startup_stage == 'source-open/decode'
                                      and actual_calls == 0 and detected == 0
                                      and manifest.get('actual_source_reads', 0) == 0
                                      and record.get('engine_initialized') is False
                                      and record.get('source_initialized') is False)
            if not pre_sdk_source_failure:
                errors.append('empty SDK inventory lacks verified pre-SDK source failure stage')
        if scenario == 'bad-source' and startup_stage == 'source-open/decode' and (
                receipts or record.get('engine_initialized') is not False or record.get('source_initialized') is not False):
            errors.append('pre-SDK source failure contradicts SDK/initialized resource inventory')
        for resource in receipts:
            if (not isinstance(resource, dict) or not {'created', 'released', 'release_error'} <= resource.keys()
                    or resource.get('created') is not True or resource.get('released') is not True
                    or resource.get('release_error') is not None):
                errors.append('SDK resource creation/release receipt failed/incomplete')
    return {'direct_recorded': True, 'accepted': not errors, 'errors': errors, 'record': record}


def timeline_evidence(manifest, session, detections, outputs, resources, events):
    errors = []
    starts = [event['monotonic'] for event in events if event.get('state') == 'startup' and is_number(event.get('monotonic'))]
    stops = [event['monotonic'] for event in events if event.get('state') == 'stop' and is_number(event.get('monotonic'))]
    details = {'policy': dict(EVIDENCE_POLICY), 'streams': {}}
    if len(starts) != 1 or len(stops) != 1 or stops[0] <= starts[0]:
        return ['one ordered startup/stop active interval required'], details
    start, stop = starts[0], stops[0]
    details.update(active_start=start, active_stop=stop, observed_active_seconds=stop - start)
    target = manifest.get('requested_duration_seconds')
    elapsed = session.get('elapsed_seconds')
    if not is_number(target) or stop - start < target - EVIDENCE_POLICY['duration_rounding_seconds']:
        errors.append('observed state timeline does not cover requested duration')
    if not is_number(elapsed) or abs(elapsed - (stop - start)) > EVIDENCE_POLICY['session_event_agreement_seconds']:
        errors.append('session duration disagrees with observed startup/stop timeline')
    origin, runner_elapsed = manifest.get('started_monotonic'), manifest.get('elapsed_seconds')
    if (not is_number(origin) or not is_number(runner_elapsed) or not 0 <= start - origin <= EVIDENCE_POLICY['startup_allowance_seconds']
            or runner_elapsed < stop - origin - EVIDENCE_POLICY['duration_rounding_seconds']
            or runner_elapsed > stop - origin + EVIDENCE_POLICY['finalization_allowance_seconds']):
        errors.append('runner elapsed/startup/finalization disagrees with observed duration')
    fps = manifest.get('replay_fps', 0)
    frame_gap = max(EVIDENCE_POLICY['minimum_frame_gap_seconds'], EVIDENCE_POLICY['frame_gap_periods'] / fps) if is_number(fps) and fps > 0 else 1
    resource_gap = EVIDENCE_POLICY['resource_interval_seconds'] * EVIDENCE_POLICY['resource_gap_periods']
    def stream(name, stamps, gap):
        if len(stamps) < 2 or any(not is_number(value) for value in stamps):
            errors.append('insufficient finite timeline samples: ' + name)
            return
        intervals = [b - a for a, b in zip(stamps, stamps[1:])]
        details['streams'][name] = {'count': len(stamps), 'first': stamps[0], 'last': stamps[-1],
                                    'start_offset_seconds': stamps[0] - start, 'end_offset_seconds': stop - stamps[-1],
                                    'max_gap_seconds': max(intervals, default=0), 'allowed_gap_seconds': gap}
        if any(value < 0 or value > gap + 1e-6 for value in intervals):
            errors.append('nonmonotonic/excessive cadence gap: ' + name)
        if stamps[0] > start + gap or stamps[-1] < stop - gap:
            errors.append('start/end coverage missing: ' + name)
        if not name.startswith('resource') and any(value < start or value > stop for value in stamps):
            errors.append('frame timestamps outside observed active interval: ' + name)
        if stamps[-1] > stop + EVIDENCE_POLICY['finalization_allowance_seconds']:
            errors.append('sample lies outside stop/finalization: ' + name)
    stream('detector capture', [row.get('captured_at') for row in detections], frame_gap)
    stream('detector processed', [row.get('processed_at') for row in detections], frame_gap)
    stream('output dispatch', [row.get('output_at') for row in outputs], frame_gap)
    stream('sink completion', [row.get('callback_finished_at') for row in outputs], frame_gap)
    stream('resources', [row.get('monotonic') for row in resources], resource_gap)
    for field in ('cpu_percent', 'rss_bytes', 'temperature_c', 'npu_frequency_hz'):
        stamps = [row.get('monotonic') for row in resources if resource_value_valid(row.get(field), field)]
        stream('resource ' + field, stamps, resource_gap)
    return errors, details


def validate_longrun(manifest, session, frames, resources, events):
    errors = provenance_errors(manifest)
    target = manifest.get('requested_duration_seconds', 1800)
    if not isinstance(target, (int, float)) or target <= 0 or session.get('elapsed_seconds', 0) < target - .05:
        errors.append('requested duration not completed')
    if manifest.get('exit_code') != 0 or session.get('state') != 'stop':
        errors.append('nonzero exit or fault session')
    if manifest.get('source_kind') != 'video-replay' or manifest.get('physical_camera_tested') is not False:
        errors.append('replay provenance must explicitly exclude physical camera validation')
    counters = session.get('counters', {})
    detections = [row for row in frames if row.get('record_type') == 'detection']
    outputs = [row for row in frames if row.get('record_type') == 'output']
    if not detections or len(detections) != counters.get('detected') or manifest.get('actual_engine_calls', 0) < len(detections):
        errors.append('incomplete real inference/per-detection evidence')
    if not outputs or len(outputs) != counters.get('output'):
        errors.append('incomplete per-output evidence')
    for name, rows in [('detection', detections), ('output', outputs)]:
        ids = [row.get('frame_id') for row in rows]
        if len(ids) != len(set(ids)):
            errors.append('duplicate ' + name + ' frame IDs')
        if any(not isinstance(frame_id, int) or frame_id < 1 for frame_id in ids) or any(a >= b for a, b in zip(ids, ids[1:])):
            errors.append('nonmonotonic ' + name + ' frame IDs')
    detection_by_id = {row.get('frame_id'): row for row in detections}
    for row in outputs:
        if not row.get('valid') or not row.get('valid_at_dispatch') or not row.get('valid_at_sink_complete'):
            errors.append('stale output in uninterrupted replay')
            break
        detection = detection_by_id.get(row.get('frame_id'))
        if detection is None or abs(detection.get('captured_at', -1) - row.get('captured_at', -2)) > 1e-6:
            errors.append('output capture does not match its detection packet')
        captured, dispatched, finished = row.get('captured_at', -1), row.get('output_at', -2), row.get('callback_finished_at', -3)
        if not captured <= dispatched <= finished:
            errors.append('invalid capture/output/completion timestamp ordering')
        elif (abs(row.get('capture_to_output_ms', -1) - (dispatched - captured) * 1000) > .01
              or abs(row.get('capture_to_sink_complete_ms', -1) - (finished - captured) * 1000) > .01):
            errors.append('latency does not match recorded capture timestamps')
        if finished - captured > manifest.get('result_ttl', 1):
            errors.append('stale output despite valid flags')
    if counters.get('read_failure', 0) or counters.get('detection_failure', 0):
        errors.append('unexpected source/inference failure during longrun')
    for row in frames:
        names = ('prep_ms', 'infer_ms', 'post_ms') if row.get('record_type') == 'detection' else (
            'capture_to_output_ms', 'sink_ms', 'capture_to_sink_complete_ms')
        if any(not isinstance(row.get(name), (int, float)) or not math.isfinite(row[name]) or row[name] < 0 for name in names):
            errors.append('invalid timing samples')
            break
    for field in ('cpu_percent', 'rss_bytes', 'temperature_c', 'npu_frequency_hz'):
        valid = [row for row in resources if resource_value_valid(row.get(field), field)]
        if len(valid) < 2:
            errors.append('resource evidence missing: ' + field)
    if any(event.get('state') in ('fault', 'degraded') for event in events):
        errors.append('fault/degraded state occurred during uninterrupted longrun')
    if not any(event.get('state') == 'stop' for event in events):
        errors.append('final stop event missing')
    inventory_errors, detections, outputs = evidence_inventory(manifest, session, frames, events)
    errors.extend(inventory_errors)
    timing_errors, coverage = timeline_evidence(manifest, session, detections, outputs, resources, events)
    errors.extend(timing_errors)
    # Historical schema-one runs did not record SDK ownership. Keep their
    # indirect evidence intact; newly recorded runs must prove direct cleanup.
    schema = manifest.get('schema_version', 1)
    if type(schema) is not int or schema not in (1, 2):
        errors.append('unsupported longrun evidence schema')
    if schema == 2 and manifest.get('cleanup_contract') != 'direct-sdk-v1':
        errors.append('schema-two longrun direct cleanup contract missing')
    cleanup = cleanup_evidence(manifest, require_direct=schema != 1, session=session)
    errors.extend(cleanup['errors'])
    metrics = {name: percentiles([row[name] for row in outputs if isinstance(row.get(name), (int, float))])
               for name in ('capture_to_output_ms', 'sink_ms', 'capture_to_sink_complete_ms')}
    for name in ('prep_ms', 'infer_ms', 'post_ms'):
        metrics[name] = percentiles([row[name] for row in detections if isinstance(row.get(name), (int, float))])
    elapsed = session.get('elapsed_seconds', 0)
    rates = {name + '_fps': counters.get(name, 0) / elapsed if is_number(elapsed) and elapsed > 0 else None
             for name in ('captured', 'detected', 'output', 'displayed')}
    return {'passed': not errors, 'errors': list(dict.fromkeys(errors)), 'metrics': metrics,
            'timeline': coverage, 'cleanup_evidence': cleanup, 'observed_rates': rates,
            'throughput_target_met': all(is_number(rates[name]) and rates[name] >= manifest.get('replay_fps', 10)
                                         for name in ('detected_fps', 'output_fps')),
            'actual_thresholds': manifest.get('thresholds'),
            'scope': 'continuity/inventory/freshness verification; throughput and physical-camera gates remain separate',
            'snapshot_integrity': 'not in original hashed-core-log inventory; not asserted by this validator',
            'physical_camera_tested': False, 'ssh_survival': 'not_observed'}


class GovernorGuard:
    def __init__(self, paths=None, requested=None):
        self.paths = [Path(path) for path in (paths if paths is not None else glob.glob('/sys/class/devfreq/*npu*/governor'))]
        self.requested = requested
        self.records = []

    def __enter__(self):
        try:
            for path in self.paths:
                before = path.read_text().strip()
                record = {'path': str(path), 'before': before, 'requested': self.requested,
                          'after': None, 'restore_success': False}
                self.records.append(record)
                if self.requested is not None:
                    path.write_text(self.requested + '\n')
                    if path.read_text().strip() != self.requested:
                        raise RuntimeError('governor write did not take effect: ' + str(path))
            return self
        except BaseException:
            self.restore()
            raise

    def restore(self):
        errors = []
        for record in self.records:
            try:
                path = Path(record['path'])
                if path.read_text().strip() != record['before']:
                    path.write_text(record['before'] + '\n')
                record['after'] = path.read_text().strip()
                record['restore_success'] = record['after'] == record['before']
                if not record['restore_success']:
                    raise RuntimeError('governor restore mismatch')
            except BaseException as error:
                record['error'] = str(error)
                errors.append(str(error))
        if errors:
            raise RuntimeError('NPU governor restoration failed: ' + '; '.join(errors))

    def __exit__(self, kind, value, tb):
        self.restore()


class Journal:
    def __init__(self, path):
        self.stream = Path(path).open('x', encoding='utf-8')
        self.lock = threading.Lock()

    def write(self, row):
        with self.lock:
            self.stream.write(json.dumps(row, allow_nan=False) + '\n')
            self.stream.flush()

    def close(self):
        with self.lock:
            self.stream.close()


class ResourceMonitor:
    def __init__(self, sampler, path, interval=1):
        self.sampler, self.interval = sampler, interval
        self.journal = Journal(path)
        self.stop = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, name='verification-resources', daemon=True)

    def _run(self):
        try:
            while True:
                self.journal.write({**self.sampler.sample(), 'pid': os.getpid(), 'wall_time': utc_now()})
                if self.stop.wait(self.interval):
                    break
        except BaseException as error:
            self.error = error

    def start(self):
        self.thread.start()

    def check(self):
        if self.error is not None:
            raise RuntimeError('resource logging failed: ' + str(self.error))

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError('resource monitor did not stop')
        self.journal.close()
        self.check()


class RecordingEngine:
    def __init__(self, engine):
        self.engine = engine
        self.actual_engine_calls = 0
        self.released = False
        self.release_error = None

    def infer(self, image):
        self.actual_engine_calls += 1
        return self.engine.infer(image)

    def release(self):
        try:
            self.engine.release()
        except BaseException as error:
            self.release_error = str(error)
            raise
        self.released = True


class RecordingSource:
    def __init__(self, source):
        self.source = source
        self.actual_source_reads = 0
        self.actual_source_read_failures = 0
        self.released = False
        self.release_error = None

    def read(self, stop_event=None):
        self.actual_source_reads += 1
        ok, frame = self.source.read(stop_event)
        if not ok and (stop_event is None or not stop_event.is_set()):
            self.actual_source_read_failures += 1
        return ok, frame

    def release(self):
        try:
            self.source.release()
        except BaseException as error:
            self.release_error = str(error)
            raise
        self.released = True


class FaultEngine(RecordingEngine):
    """Verification-only fault after real calls; never pretends a thread was killed."""
    def __init__(self, engine, fail_after=1, clock=time.monotonic, event_callback=None):
        super().__init__(engine)
        self.fail_after, self.clock = fail_after, clock
        self.event_callback = event_callback or (lambda event: None)
        self.started = None
        self.injected_exceptions = 0

    def infer(self, image):
        now = self.clock()
        if self.started is None:
            self.started = now
        if now - self.started >= self.fail_after:
            self.injected_exceptions += 1
            self.event_callback({'type': 'injection', 'scenario': 'engine-exception',
                                 'phase': 'raise', 'monotonic': now, 'actual_engine_calls': self.actual_engine_calls})
            raise RuntimeError('injected inference failure after real RKNN calls')
        return super().infer(image)


class InterruptSource:
    """Verification-only timed read interruption around a real replay source."""
    def __init__(self, source, begin=1, end=3, clock=time.monotonic, event_callback=None):
        if not 0 < begin < end:
            raise ValueError('source interruption requires 0 < begin < end')
        self.source, self.begin, self.end, self.clock = source, begin, end, clock
        self.event_callback = event_callback or (lambda event: None)
        self.started = None
        self.actual_source_reads = self.injected_read_failures = 0
        self.interruption_started_at = self.recovered_at = None
        self.recovered = False
        self.released = False
        self.release_error = None
        self.actual_source_read_failures = 0

    def read(self, stop_event=None):
        now = self.clock()
        if self.started is None:
            self.started = now
        elapsed = now - self.started
        if self.begin <= elapsed < self.end:
            if self.interruption_started_at is None:
                self.interruption_started_at = now
                self.event_callback({'type': 'injection', 'scenario': 'source-interruption',
                                     'phase': 'begin', 'monotonic': now})
            if stop_event is not None:
                stop_event.wait(.005)
            if stop_event is None or not stop_event.is_set():
                self.injected_read_failures += 1
            return False, None
        self.actual_source_reads += 1
        ok, frame = self.source.read(stop_event)
        if not ok and (stop_event is None or not stop_event.is_set()):
            self.actual_source_read_failures += 1
        if ok and self.interruption_started_at is not None and elapsed >= self.end and not self.recovered:
            self.recovered, self.recovered_at = True, self.clock()
            self.event_callback({'type': 'injection', 'scenario': 'source-interruption',
                                 'phase': 'recovered', 'monotonic': self.recovered_at})
        return ok, frame

    def release(self):
        try:
            self.source.release()
        except BaseException as error:
            self.release_error = str(error)
            raise
        self.released = True


class SDKLifecycleMonitor:
    """Verification-only SDK ownership recording, including failed engine init.

    Calls still delegate to the installed RKNNLite. The factory is restored on
    exit; this runner owns one serial experiment in its child process.
    """
    def __init__(self):
        self.records = []

    def __enter__(self):
        self.api = importlib.import_module('rknnlite.api')
        self.original = self.api.RKNNLite
        monitor = self
        class RecordingSDK:
            def __init__(self, *args, **kwargs):
                self.delegate = monitor.original(*args, **kwargs)
                self.record = {'created': True, 'released': False, 'release_error': None}
                monitor.records.append(self.record)

            def __getattr__(self, name):
                return getattr(self.delegate, name)

            def release(self):
                try:
                    self.delegate.release()
                except BaseException as error:
                    self.record['release_error'] = str(error)
                    raise
                self.record['released'] = True
        self.api.RKNNLite = RecordingSDK
        return self

    def __exit__(self, error_type, error, tb):
        self.api.RKNNLite = self.original


def collect_cleanup(manifest, pipeline, engine, source, sdk_records=None):
    """Record live post-run ownership, including secondary worker failures."""
    actual_engine = engine if engine is not None else getattr(pipeline, 'engine', None)
    actual_source = source if source is not None else getattr(pipeline, 'source', None)
    errors = manifest.setdefault('cleanup_errors', [])
    for name, resource in [('engine', actual_engine), ('source', actual_source)]:
        if getattr(resource, 'release_error', None):
            errors.append(name + ' release: ' + resource.release_error)
    if pipeline is not None:
        while not pipeline.errors.empty():
            errors.append('secondary pipeline failure: ' + str(pipeline.errors.get_nowait()))
    workers = {name: getattr(pipeline, name + '_thread', None) for name in ('capture', 'detect')}
    manifest['cleanup'] = {
        'engine_initialized': actual_engine is not None,
        'source_initialized': actual_source is not None,
        'engine_released': getattr(actual_engine, 'released', None) if actual_engine is not None else None,
        'source_released': getattr(actual_source, 'released', None) if actual_source is not None else None,
        'capture_worker_alive': bool(workers['capture'] is not None and workers['capture'].is_alive()),
        'detect_worker_alive': bool(workers['detect'] is not None and workers['detect'].is_alive()),
        'shutdown_timeout': any('shutdown' in error.lower() for error in errors)
                            or 'shutdown' in manifest.get('error', '').lower(),
    }
    manifest['cleanup_errors'] = list(dict.fromkeys(errors))
    if sdk_records is not None:
        manifest['cleanup']['sdk_resources'] = sdk_records
        for record in sdk_records:
            if record.get('release_error'):
                manifest['cleanup_errors'].append('SDK release: ' + record['release_error'])


def validate_fault_case(manifest, session, frames, events):
    errors = provenance_errors(manifest)
    scenario = manifest.get('scenario')
    states = [event for event in events if event.get('type') == 'state']
    outputs = [row for row in frames if row.get('record_type') == 'output']
    counters = session.get('counters', {})
    startup_stage = None
    reaction_evidence = None
    if scenario in ('engine-exception', 'bad-model', 'bad-source'):
        faults = [event for event in states if event.get('state') == 'fault']
        if manifest.get('exit_code') != 1 or session.get('state') != 'fault' or not faults:
            errors.append('fault scenario did not exit nonzero with a genuine fault event')
        for event in faults:
            if event.get('valid') or event.get('alarm_state') != 'unknown':
                errors.append('misleading valid/alarm fault state')
        if faults:
            fault_at = faults[0]['monotonic']
            if any(event.get('type') == 'alarm' and event.get('monotonic', 0) >= fault_at for event in events):
                errors.append('alarm emitted after fault')
            if any(row.get('valid') and row.get('output_at', 0) >= fault_at for row in outputs):
                errors.append('valid output emitted after fault')
        if scenario == 'engine-exception':
            if manifest.get('actual_engine_calls', 0) < 1 or manifest.get('injected_exceptions') != 1:
                errors.append('engine exception lacked real RKNN delegation/injection evidence')
            if counters.get('detected', 0) < 1:
                errors.append('no completed real inference before exception')
            raises = [event for event in events if event.get('type') == 'injection'
                      and event.get('scenario') == scenario and event.get('phase') == 'raise']
            if len(raises) != 1 or not is_number(raises[0].get('monotonic')):
                errors.append('actual inference injection raise record missing/ambiguous')
            else:
                raised_at = raises[0]['monotonic']
                if raises[0].get('actual_engine_calls') != manifest.get('actual_engine_calls'):
                    errors.append('raise marker/manifest real call counts disagree')
                if faults and faults[0]['monotonic'] < raised_at:
                    errors.append('fault state predates declared inference injection')
                if any(row.get('processed_at', math.inf) > raised_at for row in frames if row.get('record_type') == 'detection'):
                    errors.append('completed detector packet after serial inference injection')
        else:
            if counters.get('detected', 0) or outputs:
                errors.append('startup failure unexpectedly produced detections')
            error = manifest.get('error', '')
            expected = ('load_rknn failed',) if scenario == 'bad-model' else ('cannot open video source', 'empty or undecodable video', 'missing video source')
            matched = any(text in error for text in expected)
            if scenario == 'bad-model' and 'init_runtime failed' in error:
                # Some Lite versions accept the file at load_rknn and reject its
                # contents at init_runtime. This only counts for a byte/hash-
                # verified tiny corrupt fixture, never a valid model/init error.
                matched = (manifest.get('malformed_model_file_verified') is True
                           and 0 < manifest.get('model', {}).get('bytes', 0) <= 128
                           and manifest.get('model', {}).get('sha256') != manifest.get('baseline_model', {}).get('sha256'))
                startup_stage = 'runtime-init' if matched else None
            elif scenario == 'bad-model' and matched:
                startup_stage = 'model-load'
            elif scenario == 'bad-source' and matched:
                startup_stage = 'source-open/decode'
            if not matched:
                errors.append('startup failed for an unrelated reason')
    elif scenario == 'source-interruption':
        degraded = [event for event in states if event.get('state') == 'degraded']
        if manifest.get('exit_code') != 0 or session.get('state') != 'stop':
            errors.append('recoverable source scenario did not stop normally')
        if (manifest.get('actual_engine_calls', 0) < 1 or manifest.get('actual_source_reads', 0) < 2
                or manifest.get('injected_read_failures', 0) < 1 or not manifest.get('recovered')
                or counters.get('read_failure', 0) < 1 or not degraded):
            errors.append('real source interruption/recovery evidence missing')
        beginnings = [event for event in events if event.get('type') == 'injection' and event.get('scenario') == scenario and event.get('phase') == 'begin']
        endings = [event for event in events if event.get('type') == 'injection' and event.get('scenario') == scenario and event.get('phase') == 'recovered']
        if len(beginnings) != 1 or len(endings) != 1 or not is_number(beginnings[0].get('monotonic')) or not is_number(endings[0].get('monotonic')):
            errors.append('actual source injection begin/recovered records missing/ambiguous')
        elif degraded:
            begin, actual_end = beginnings[0]['monotonic'], endings[0]['monotonic']
            observed = next((event for event in degraded if begin <= event.get('monotonic', -1) < actual_end
                             and event.get('reason') == 'source read failure'), None)
            if not begin < actual_end or observed is None:
                errors.append('degraded state does not lie in actual source injection interval')
            observed_at = observed['monotonic'] if observed is not None else None
            announcement_outputs = [row.get('frame_id') for row in outputs if begin <= row.get('output_at', -1) < actual_end
                                    and (row.get('valid') or row.get('valid_at_dispatch') or row.get('alarm_state') != 'unknown')]
            announcement_alarms = [event.get('monotonic') for event in events if event.get('type') == 'alarm'
                                   and begin <= event.get('monotonic', -1) < actual_end]
            announcement_states = [event.get('monotonic') for event in states if event.get('state') in ('healthy', 'no-detection')
                                   and begin <= event.get('monotonic', -1) < actual_end]
            reaction_evidence = {'contract': 'producer-observed failed read / source epoch invalidation',
                                 'injection_announced_at': begin, 'producer_observed_failure_at': observed_at,
                                 'actual_recovered_read_at': actual_end,
                                 'reaction_ms': (observed_at - begin) * 1000 if observed_at is not None else None,
                                 'announced_gap_zero_latency': not (announcement_outputs or announcement_alarms or announcement_states),
                                 'announcement_violations': {'output_frame_ids': announcement_outputs,
                                                             'alarm_timestamps': announcement_alarms,
                                                             'healthy_state_timestamps': announcement_states},
                                 'zero_latency_guaranteed': False, 'observed_failed_read_contract': False}
            source_origin = manifest.get('source_started_monotonic')
            if source_origin is not None:
                fault_at, recover_at = manifest.get('fault_at'), manifest.get('recover_at')
                if (not all(is_number(value) for value in (source_origin, fault_at, recover_at))
                        or not 0 < fault_at < recover_at or not source_origin + fault_at <= begin < source_origin + recover_at
                        or actual_end < source_origin + recover_at):
                    errors.append('actual source injection markers disagree with recorded injection schedule')
            recovery = next((event for event in states if event.get('state') in ('healthy', 'no-detection')
                             and event.get('monotonic', 0) >= actual_end), None)
            if recovery is None:
                errors.append('fresh recovery state missing')
            else:
                end = recovery['monotonic']
                if observed_at is not None and any(event.get('state') in ('healthy', 'no-detection') and observed_at <= event.get('monotonic', -1) < actual_end for event in states):
                    errors.append('premature healthy state inside actual source interruption')
                for row in outputs:
                    if observed_at is not None and observed_at <= row.get('output_at', 0) < actual_end and (
                            row.get('valid') or row.get('valid_at_dispatch') or row.get('alarm_state') != 'unknown'):
                        errors.append('misleading output after producer observed source outage')
                    if observed_at is not None and row.get('output_at', 0) >= observed_at and row.get('captured_at', math.inf) < begin and (row.get('valid') or row.get('valid_at_dispatch')):
                        errors.append('pre-outage capture reused as valid after observed interruption')
                if observed_at is not None and any(event.get('type') == 'alarm' and observed_at <= event.get('monotonic', 0) < actual_end for event in events):
                    errors.append('alarm emitted during source outage')
                pre_epochs = [row.get('source_epoch', -1) for row in outputs
                              if observed_at is not None and row.get('output_at', math.inf) < observed_at]
                old_epoch = max(pre_epochs, default=0)
                if any(row.get('output_at', -1) >= actual_end and (row.get('valid') or row.get('valid_at_dispatch'))
                       and (row.get('captured_at', -1) < actual_end or row.get('source_epoch', -1) <= old_epoch) for row in outputs):
                    errors.append('recovery presented pre-recovery capture or old source epoch as valid')
                fresh = [row for row in outputs if row.get('captured_at', -1) >= actual_end
                         and row.get('output_at', -1) >= end and row.get('valid') and row.get('valid_at_dispatch')
                         and row.get('valid_at_sink_complete') and row.get('source_epoch', -1) > old_epoch]
                if not fresh:
                    errors.append('distinct post-recovery capture/detection/output with new epoch missing')
                # Monotonic clocks may quantize a pre-injection capture and the
                # immediately following marker to one value. Equality cannot
                # establish their order; output validity and epoch rules above
                # still enforce the observed-failure boundary without grace.
                if any(begin < row.get('captured_at', -1) < actual_end for row in frames if row.get('record_type') == 'detection'):
                    errors.append('capture recorded inside actual read-failure interval')
                if counters.get('read_failure') != manifest.get('injected_read_failures', 0) + manifest.get('actual_source_read_failures', 0):
                    errors.append('injected/native read-failure counters do not conserve')
    else:
        errors.append('unknown fault scenario')
    inventory_errors, detections, outputs = evidence_inventory(manifest, session, frames, events)
    errors.extend(inventory_errors)
    starts = [event.get('monotonic') for event in states if event.get('state') == 'startup']
    terminals = [event.get('monotonic') for event in states if event.get('state') == session.get('state')
                 and session.get('state') in ('fault', 'stop')]
    if (len(starts) != 1 or not terminals or not all(is_number(value) for value in (starts[0], terminals[-1], session.get('elapsed_seconds')))
            or terminals[-1] < starts[0]
            or abs(session.get('elapsed_seconds', 0) - (terminals[-1] - starts[0])) > EVIDENCE_POLICY['session_event_agreement_seconds']):
        errors.append('fault session duration disagrees with startup/terminal timeline')
    elif any(is_number(row.get(key)) and not starts[0] <= row[key] <= terminals[-1]
             for row in frames for key in ('captured_at', 'processed_at', 'output_at', 'callback_finished_at') if key in row):
        errors.append('fault frame timestamps outside observed active interval')
    primary_verified = not errors
    if reaction_evidence is not None:
        reaction_evidence['observed_failed_read_contract'] = primary_verified
    cleanup = cleanup_evidence(manifest, require_direct=True, session=session, startup_stage=startup_stage)
    errors.extend(cleanup['errors'])
    return {'passed': not errors, 'errors': list(dict.fromkeys(errors)), 'scenario': scenario,
            'primary_fault_verified': primary_verified, 'cleanup_evidence': cleanup,
            'startup_failure_stage': startup_stage,
            'source_reaction_evidence': reaction_evidence,
            'physical_camera_tested': False, 'thread_termination_tested': False,
            'injection_method': 'verification wrapper exception/read interruption; real delegation outside injection'}


def validate_suite(manifest, children):
    errors = []
    names = {'engine-exception', 'source-interruption', 'bad-model', 'bad-source'}
    if manifest.get('exit_code') != 0:
        errors.append('faultsuite parent did not exit successfully')
    cases = manifest.get('cases', [])
    if len(cases) != 4 or {case.get('scenario') for case in cases} != names:
        errors.append('four independent child scenarios required')
    for case in cases:
        expected = 0 if case.get('scenario') == 'source-interruption' else 1
        if case.get('child_exit_code') != expected or case.get('expected_exit_code') != expected:
            errors.append('wrong child process exit: ' + str(case.get('scenario')))
    if len(children) != 4 or any(not child.get('passed') for child in children):
        errors.append('one or more child evidence validations failed')
    governors = manifest.get('governors', [])
    if not governors or any(not row.get('restore_success') or row.get('before') != row.get('after') for row in governors):
        errors.append('faultsuite NPU governor restoration evidence missing/failed')
    return {'passed': not errors, 'errors': errors, 'cases': children}


def run_fault_case(args):
    directory = Path(args.output_root) / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    manifest = initial_manifest(args, directory)
    manifest.update(kind='fault-case', scenario=args.scenario, thread_termination_tested=False,
                    fault_at=args.fault_at, recover_at=args.recover_at)
    baseline_model, baseline_input = args.model, args.source
    if args.scenario == 'bad-model':
        args.model = str(directory / 'corrupt.rknn')
        Path(args.model).write_bytes(b'verification intentionally invalid RKNN model\n')
    if args.scenario == 'bad-source':
        args.source = str(directory / 'corrupt.mp4')
        Path(args.source).write_bytes(b'verification intentionally invalid video\n')
    write_json(directory / 'manifest.json', manifest)
    guard = GovernorGuard(requested=args.governor)
    events = monitor = engine = source = pipeline = sdk_monitor = None
    pipeline_ran = False
    code = 1
    try:
        manifest['model'], manifest['input'] = file_record(args.model), file_record(args.source)
        manifest['baseline_model'], manifest['baseline_input'] = file_record(baseline_model), file_record(baseline_input)
        manifest['sources'] = {name: file_record(Path(args.runtime_dir) / name) for name in SOURCE_FILES}
        manifest['sources']['verify_runtime.py'] = file_record(__file__)
        importlib.import_module('rknnlite.api')
        manifest['backend'] = 'rknnlite'
        runtime = runtime_module(args.runtime_dir)
        events = Journal(directory / 'events.jsonl')
        with guard, SDKLifecycleMonitor() as sdk_monitor:
            manifest['governors'] = guard.records
            monitor = ResourceMonitor(runtime.RuntimeSampler(), directory / 'resources.jsonl')
            monitor.start()
            runtime_args = SimpleNamespace(model=args.model, source=args.source, duration=args.duration,
                                           replay_fps=args.replay_fps, result_ttl=args.result_ttl,
                                           no_display=True, jsonl=str(directory / 'frames.jsonl'),
                                           session_json=str(directory / 'session.json'))
            if args.scenario in ('engine-exception', 'source-interruption'):
                source = RecordingSource(runtime.make_source(args.source, replay_fps=args.replay_fps))
                width, height = runtime.parse_model_size(args.model)
                real_engine = runtime.RKNNEngine(args.model, width, height)
                engine = (FaultEngine(real_engine, args.fault_at, event_callback=events.write)
                          if args.scenario == 'engine-exception' else RecordingEngine(real_engine))
                if args.scenario == 'source-interruption':
                    source = InterruptSource(source, args.fault_at, args.recover_at, event_callback=events.write)
            elif args.scenario == 'bad-model':
                source = RecordingSource(runtime.make_source(args.source, replay_fps=args.replay_fps))
            pipeline = runtime.Pipeline(runtime_args, engine=engine, source=source,
                                        event_callback=events.write, output_callback=lambda packet: monitor.check())
            manifest['input_size'] = [pipeline.mw, pipeline.mh]
            manifest['thresholds'] = {'conf': pipeline.args.conf, 'nms': pipeline.args.nms, 'skip': pipeline.args.skip}
            manifest['class_order'] = ['person', 'hat']
            write_json(directory / 'manifest.json', manifest)
            pipeline_ran = True
            pipeline.run()
            code = 0
    except BaseException as error:
        manifest['error'] = str(error)
        (directory / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
    finally:
        if not pipeline_ran:
            for resource in (source, engine):
                if resource is not None:
                    try:
                        resource.release()
                    except BaseException as error:
                        manifest.setdefault('cleanup_errors', []).append(str(error))
        for resource in (monitor, events):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as error:
                    manifest.setdefault('cleanup_errors', []).append(str(error))
                    code = 1
        collect_cleanup(manifest, pipeline, engine, source, sdk_monitor.records if sdk_monitor is not None else None)
        manifest.update(exit_code=code, status='completed' if code == 0 else 'failed', governors=guard.records,
                        actual_engine_calls=engine.actual_engine_calls if engine is not None else 0,
                        injected_exceptions=getattr(engine, 'injected_exceptions', 0),
                        actual_source_reads=getattr(source, 'actual_source_reads', 0),
                        actual_source_read_failures=getattr(source, 'actual_source_read_failures', 0),
                        injected_read_failures=getattr(source, 'injected_read_failures', 0),
                        recovered=getattr(source, 'recovered', False), finished_wall_time=utc_now(),
                        elapsed_seconds=time.monotonic() - manifest['started_monotonic'],
                        engine_started_monotonic=getattr(engine, 'started', None),
                        source_started_monotonic=getattr(source, 'started', None))
        manifest['artifacts'] = {name: file_record(directory / name) for name in
                                 ('frames.jsonl', 'events.jsonl', 'resources.jsonl', 'session.json', 'error.txt', 'corrupt.rknn', 'corrupt.mp4')
                                 if (directory / name).is_file()}
        if args.scenario == 'bad-model':
            manifest['malformed_model_file_verified'] = (0 < Path(args.model).stat().st_size <= 128
                                                         and file_record(args.model)['sha256'] == manifest['model']['sha256']
                                                         and manifest['model']['sha256'] != manifest['baseline_model']['sha256'])
        if (directory / 'session.json').is_file() and (directory / 'events.jsonl').is_file():
            manifest['validation'] = validate_fault_case(manifest, json.loads((directory / 'session.json').read_text(encoding='utf-8')),
                                                         read_jsonl(directory / 'frames.jsonl'), read_jsonl(directory / 'events.jsonl'))
        else:
            manifest['validation'] = {'passed': False, 'errors': ['pipeline evidence missing; bootstrap failed']}
        write_json(directory / 'manifest.json', manifest)
    print(json.dumps({'run_dir': str(directory), 'exit_code': code, 'validation': manifest['validation']}))
    return code


def run_faultsuite(args):
    directory = Path(args.output_root) / (args.run_id or ('faultsuite-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')))
    directory.mkdir(parents=True, exist_ok=False)
    manifest = initial_manifest(args, directory)
    manifest['cases'] = []
    write_json(directory / 'manifest.json', manifest)
    guard = GovernorGuard(requested=args.governor)
    code = 1
    try:
        with guard:
            for scenario in ('engine-exception', 'source-interruption', 'bad-model', 'bad-source'):
                command = [sys.executable, str(Path(__file__).resolve()), '_case', '--scenario', scenario,
                           '--runtime-dir', args.runtime_dir, '--model', args.model, '--source', args.source,
                           '--output-root', str(directory), '--run-id', scenario, '--duration', str(args.duration),
                           '--replay-fps', str(args.replay_fps), '--result-ttl', str(args.result_ttl),
                           '--fault-at', str(args.fault_at), '--recover-at', str(args.recover_at),
                           '--input-description', args.input_description]
                if args.governor:
                    command.extend(['--governor', args.governor])
                with (directory / (scenario + '.log')).open('wb') as log:
                    try:
                        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                                timeout=args.duration + 30)
                        observed_code = result.returncode
                    except subprocess.TimeoutExpired:
                        observed_code = 124
                child = directory / scenario / 'manifest.json'
                child_manifest = json.loads(child.read_text(encoding='utf-8')) if child.is_file() else {}
                validation = child_manifest.get('validation', {'passed': False, 'errors': ['child manifest missing']})
                if observed_code == 124:
                    validation = {'passed': False, 'errors': ['child timed out; no fault acceptance claimed']}
                expected_code = 0 if scenario == 'source-interruption' else 1
                if observed_code != expected_code:
                    validation = {'passed': False, 'errors': ['unexpected child process exit code', *validation.get('errors', [])]}
                manifest['cases'].append({'scenario': scenario, 'command': command, 'child_exit_code': observed_code,
                                          'expected_exit_code': expected_code, 'validation': validation})
                write_json(directory / 'manifest.json', manifest)
        code = 0 if all(case['validation'].get('passed') for case in manifest['cases']) else 1
    except BaseException as error:
        manifest['error'] = str(error)
        (directory / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
    finally:
        manifest.update(governors=guard.records, exit_code=code, status='completed' if code == 0 else 'failed',
                        finished_wall_time=utc_now(), physical_camera_tested=False,
                        thread_termination_tested=False)
        write_json(directory / 'manifest.json', manifest)
    print(json.dumps({'run_dir': str(directory), 'exit_code': code}))
    return code


def plot_analysis(frames, resources, destination):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    outputs = [row for row in frames if row.get('record_type') == 'output']
    detections = [row for row in frames if row.get('record_type') == 'detection']
    origin = min([row.get('captured_at', math.inf) for row in frames] +
                 [row.get('monotonic', math.inf) for row in resources], default=0)
    figure, axes = plt.subplots(2, 1, figsize=(10, 7), constrained_layout=True)
    for name, label in [('capture_to_output_ms', 'Capture to callback dispatch'),
                        ('capture_to_sink_complete_ms', 'Capture to completed sink')]:
        rows = [row for row in outputs if isinstance(row.get(name), (int, float))]
        axes[0].plot([row['captured_at'] - origin for row in rows], [row[name] for row in rows], label=label)
    for name in ('prep_ms', 'infer_ms', 'post_ms'):
        rows = [row for row in detections if isinstance(row.get(name), (int, float))]
        axes[1].plot([row['captured_at'] - origin for row in rows], [row[name] for row in rows], label=name)
    for axis in axes:
        axis.set_xlabel('Elapsed replay time (s)')
        axis.set_ylabel('Latency (ms)')
        axis.legend()
        axis.grid(alpha=.25)
    figure.savefig(destination / 'latency.png', dpi=160)
    plt.close(figure)
    figure, axes = plt.subplots(4, 1, figsize=(10, 10), constrained_layout=True)
    for axis, field, scale, label in zip(axes, ('cpu_percent', 'rss_bytes', 'temperature_c', 'npu_frequency_hz'),
                                       (1, 1 / 1024**2, 1, 1 / 1e6),
                                       ('Process CPU (%)', 'Process RSS (MiB)', 'Max thermal zone (C)', 'Max NPU frequency (MHz)')):
        stamps, values = [], []
        for row in resources:
            value = row.get(field)
            if isinstance(value, dict) and value:
                value = max(value.values())
            if isinstance(value, (int, float)):
                stamps.append(row['monotonic'] - origin)
                values.append(value * scale)
        axis.plot(stamps, values)
        if not values:
            axis.text(.5, .5, 'Measurement unavailable', transform=axis.transAxes, ha='center')
        axis.set_ylabel(label)
        axis.set_xlabel('Elapsed replay time (s)')
        axis.grid(alpha=.25)
    figure.savefig(destination / 'resources.png', dpi=160)
    plt.close(figure)
    analysis = {name: percentiles([row[name] for row in outputs if isinstance(row.get(name), (int, float))])
                for name in ('capture_to_output_ms', 'sink_ms', 'capture_to_sink_complete_ms')}
    rss = [row['rss_bytes'] for row in resources if isinstance(row.get('rss_bytes'), (int, float))]
    analysis['rss'] = {'start_bytes': rss[0] if rss else None, 'end_bytes': rss[-1] if rss else None,
                       'peak_bytes': max(rss) if rss else None, 'growth_bytes': rss[-1] - rss[0] if rss else None}
    write_json(destination / 'analysis.json', analysis)
    return analysis


def process_start_id(pid):
    try:
        text = Path('/proc/%d/stat' % pid).read_text()
        return text[text.rfind(')') + 2:].split()[19]
    except (OSError, IndexError):
        return None


def load_evidence(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    errors = []
    if manifest.get('kind') != 'faultsuite':
        for name in ('frames.jsonl', 'events.jsonl', 'resources.jsonl', 'session.json'):
            if name not in manifest.get('artifacts', {}):
                errors.append('unhashed/missing required artifact: ' + name)
    for name, record in manifest.get('artifacts', {}).items():
        if Path(name).name != name or not (directory / name).is_file():
            errors.append('missing artifact: ' + name)
        elif file_record(directory / name)['sha256'] != record.get('sha256'):
            errors.append('artifact sha256 mismatch: ' + name)
    if manifest.get('scenario') == 'bad-model':
        model = manifest.get('model', {})
        malformed = directory / Path(model.get('path', '')).name
        verified = False
        if malformed.is_file():
            actual = file_record(malformed)
            verified = (0 < actual['bytes'] <= 128 and actual['bytes'] == model.get('bytes')
                        and actual['sha256'] == model.get('sha256')
                        and actual['sha256'] != manifest.get('baseline_model', {}).get('sha256'))
        manifest['malformed_model_file_verified'] = verified
        if not verified:
            errors.append('malformed model payload hash/bytes not verified')
    session = json.loads((directory / 'session.json').read_text(encoding='utf-8')) if (directory / 'session.json').is_file() else {}
    def rows(name):
        return read_jsonl(directory / name) if (directory / name).is_file() else []
    return manifest, session, rows('frames.jsonl'), rows('resources.jsonl'), rows('events.jsonl'), errors


def validate_observation(manifest, evidence):
    common = (evidence.get('alive') and evidence.get('pid') == manifest.get('pid')
              and evidence.get('start_id') is not None and evidence.get('operator_asserted_disconnection')
              and bool(evidence.get('note')) and evidence.get('monotonic', 0) >= manifest.get('started_monotonic', 0))
    if not common:
        return False
    if manifest.get('process_start_id') is not None:
        return evidence.get('start_id') == manifest['process_start_id']
    # The first deployed v1 runner omitted a start token. Require observed
    # command identity as well as PID and the explicit external operator note.
    return evidence.get('identity_command_matches_manifest') is True


def verify_directory(args):
    directory = Path(args.run_dir)
    manifest, session, frames, resources, events, integrity_errors = load_evidence(directory)
    if manifest.get('kind') == 'faultsuite':
        cases = []
        for name in ('engine-exception', 'source-interruption', 'bad-model', 'bad-source'):
            child = directory / name
            if not (child / 'manifest.json').is_file():
                cases.append({'scenario': name, 'passed': False, 'errors': ['child evidence missing']})
                continue
            m, s, f, r, e, errors = load_evidence(child)
            validation = validate_fault_case(m, s, f, e)
            validation['errors'].extend(errors)
            validation['passed'] = not validation['errors']
            cases.append(validation)
        result = validate_suite(manifest, cases)
        result['errors'].extend(integrity_errors)
    elif manifest.get('kind') == 'fault-case':
        result = validate_fault_case(manifest, session, frames, events)
        result['errors'].extend(integrity_errors)
    else:
        result = validate_longrun(manifest, session, frames, resources, events)
        result['errors'].extend(integrity_errors)
        observation = directory / 'ssh-observation.json'
        if observation.is_file():
            evidence = json.loads(observation.read_text(encoding='utf-8'))
            if validate_observation(manifest, evidence):
                result['ssh_survival'] = 'operator_observed_alive_after_disconnect'
        if args.require_ssh_observation and result['ssh_survival'] == 'not_observed':
            result['errors'].append('SSH survival was not externally observed')
    result['passed'] = result.get('passed', False) and not result.get('errors')
    result['verifier'] = file_record(__file__)
    if args.plots and frames:
        plot_analysis(frames, resources, directory / 'plots')
    write_json(Path(args.out) if args.out else directory / 'verification.json', result)
    print(json.dumps(result))
    return 0 if result['passed'] else 1


def record_observation(args):
    directory = Path(args.run_dir)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    pid = manifest['pid']
    try:
        os.kill(pid, 0)
        alive = True
    except OSError:
        alive = False
    observed_command = []
    try:
        observed_command = [part.decode('utf-8', errors='replace') for part in
                            Path('/proc/%d/cmdline' % pid).read_bytes().split(b'\0') if part]
    except OSError:
        pass
    expected_command = manifest.get('command', [])
    command_matches = (bool(observed_command) and len(observed_command) == len(expected_command)
                       and Path(observed_command[0]).name == Path(expected_command[0]).name
                       and observed_command[1:] == expected_command[1:])
    record = {'pid': pid, 'alive': alive, 'start_id': process_start_id(pid), 'wall_time': utc_now(),
              'monotonic': time.monotonic(), 'operator_asserted_disconnection': args.after_ssh_disconnect,
              'note': args.note, 'observed_command': observed_command,
              'identity_command_matches_manifest': command_matches,
              'claim_scope': 'operator observation; runner cannot observe SSH transport closure'}
    write_json(directory / 'ssh-observation.json', record)
    print(json.dumps(record))
    return 0 if validate_observation(manifest, record) else 1


def runtime_module(directory):
    directory = Path(directory).resolve()
    sys.path.insert(0, str(directory))
    return importlib.import_module('pipeline_runtime')


def initial_manifest(args, directory):
    return {'schema_version': 1, 'kind': args.command, 'status': 'running', 'run_id': directory.name,
            'pid': os.getpid(), 'host': socket.gethostname(), 'platform': platform.platform(),
            'python': sys.version, 'command': [sys.executable, *sys.argv], 'started_wall_time': utc_now(),
            'started_monotonic': time.monotonic(), 'requested_duration_seconds': args.duration,
            'replay_fps': args.replay_fps, 'result_ttl': args.result_ttl,
            'source_kind': 'video-replay', 'input_description': args.input_description,
            'physical_camera_tested': False, 'backend': None, 'exit_code': None, 'governors': []}


def run_longrun(args):
    directory = Path(args.output_root) / (args.run_id or ('longrun-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')))
    directory.mkdir(parents=True, exist_ok=False)
    manifest = initial_manifest(args, directory)
    manifest.update(schema_version=2, cleanup_contract='direct-sdk-v1')
    write_json(directory / 'manifest.json', manifest)
    guard = GovernorGuard(requested=args.governor)
    events = monitor = engine = source = pipeline = sdk_monitor = None
    pipeline_ran = False
    old_handlers = {}
    interrupted = {'signal': None}
    def handle_signal(number, frame):
        interrupted['signal'] = number
        if pipeline is not None:
            pipeline.stop_event.set()
    code = 1
    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            old_handlers[number] = signal.signal(number, handle_signal)
        manifest['model'] = file_record(args.model)
        manifest['input'] = file_record(args.source)
        manifest['sources'] = {name: file_record(Path(args.runtime_dir) / name) for name in SOURCE_FILES}
        manifest['sources']['verify_runtime.py'] = file_record(__file__)
        # No simulator or injected NPU is accepted for this board experiment.
        importlib.import_module('rknnlite.api')
        manifest['backend'] = 'rknnlite'
        runtime = runtime_module(args.runtime_dir)
        runtime_args = SimpleNamespace(model=args.model, source=args.source, no_display=True,
                                       duration=args.duration, replay_fps=args.replay_fps,
                                       result_ttl=args.result_ttl, jsonl=str(directory / 'frames.jsonl'),
                                       session_json=str(directory / 'session.json'),
                                       save_dir=str(directory / 'snapshots'), save_interval=args.save_interval)
        events = Journal(directory / 'events.jsonl')
        with guard, SDKLifecycleMonitor() as sdk_monitor:
            manifest['governors'] = guard.records
            write_json(directory / 'manifest.json', manifest)
            monitor = ResourceMonitor(runtime.RuntimeSampler(), directory / 'resources.jsonl')
            monitor.start()
            source = RecordingSource(runtime.make_source(args.source, replay_fps=args.replay_fps))
            width, height = runtime.parse_model_size(args.model)
            engine = RecordingEngine(runtime.RKNNEngine(args.model, width, height))
            manifest['input_size'] = [width, height]
            manifest['thresholds'] = {'conf': .35, 'nms': .45, 'skip': 0}
            manifest['class_order'] = ['person', 'hat']
            pipeline = runtime.Pipeline(runtime_args, engine=engine, source=source,
                                        event_callback=events.write, output_callback=lambda packet: monitor.check())
            write_json(directory / 'manifest.json', manifest)
            if interrupted['signal'] is not None:
                raise RuntimeError('interrupted before runtime start')
            pipeline_ran = True
            pipeline.run()
            code = 0
    except BaseException as error:
        manifest['error'] = str(error)
        (directory / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
    finally:
        if not pipeline_ran:
            for resource in (source, engine):
                if resource is not None:
                    try:
                        resource.release()
                    except BaseException as error:
                        manifest.setdefault('cleanup_errors', []).append(str(error))
        for resource in (monitor, events):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as error:
                    manifest.setdefault('cleanup_errors', []).append(str(error))
                    code = 1
        collect_cleanup(manifest, pipeline, engine, source,
                        sdk_monitor.records if sdk_monitor is not None else None)
        if manifest.get('cleanup_errors'):
            code = 1
        manifest['governors'] = guard.records
        manifest['actual_engine_calls'] = engine.actual_engine_calls if engine is not None else 0
        manifest['actual_source_reads'] = getattr(source, 'actual_source_reads', 0)
        manifest['actual_source_read_failures'] = getattr(source, 'actual_source_read_failures', 0)
        manifest['finished_wall_time'] = utc_now()
        manifest['elapsed_seconds'] = time.monotonic() - manifest['started_monotonic']
        manifest['signal'] = interrupted['signal']
        if interrupted['signal'] is not None:
            code = 128 + interrupted['signal']
        manifest['exit_code'] = code
        manifest['status'] = 'completed' if code == 0 else 'failed'
        artifacts = {}
        for name in ('frames.jsonl', 'events.jsonl', 'resources.jsonl', 'session.json', 'error.txt'):
            if (directory / name).is_file():
                artifacts[name] = file_record(directory / name)
        manifest['artifacts'] = artifacts
        if code == 0:
            session = json.loads((directory / 'session.json').read_text(encoding='utf-8'))
            manifest['validation'] = validate_longrun(manifest, session, read_jsonl(directory / 'frames.jsonl'),
                                                     read_jsonl(directory / 'resources.jsonl'), read_jsonl(directory / 'events.jsonl'))
            if not manifest['validation']['passed']:
                code = manifest['exit_code'] = 1
                manifest['status'] = 'validation-failed'
        write_json(directory / 'manifest.json', manifest)
        for number, handler in old_handlers.items():
            signal.signal(number, handler)
    print(json.dumps({'run_dir': str(directory), 'exit_code': code, 'status': manifest['status']}))
    return code


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--runtime-dir', default='/userdata/helmet')
    common.add_argument('--model', required=True)
    common.add_argument('--source', required=True, help='actual local replay video file')
    common.add_argument('--output-root', default=DEFAULT_ROOT)
    common.add_argument('--run-id', default='')
    common.add_argument('--replay-fps', type=float, default=10)
    common.add_argument('--result-ttl', type=float, default=1)
    common.add_argument('--save-interval', type=float, default=30)
    common.add_argument('--governor', default=None, help='optional governor; original values restored in finally')
    common.add_argument('--input-description', default='file video replay; camera not tested')
    longrun = commands.add_parser('longrun', parents=[common])
    longrun.add_argument('--duration', type=float, default=1800)
    faults = commands.add_parser('faultsuite', parents=[common])
    faults.add_argument('--duration', type=float, default=5)
    faults.add_argument('--fault-at', type=float, default=1)
    faults.add_argument('--recover-at', type=float, default=3)
    child = commands.add_parser('_case', parents=[common], help=argparse.SUPPRESS)
    child.add_argument('--scenario', choices=('engine-exception', 'source-interruption', 'bad-model', 'bad-source'), required=True)
    child.add_argument('--duration', type=float, default=5)
    child.add_argument('--fault-at', type=float, default=1)
    child.add_argument('--recover-at', type=float, default=3)
    verify = commands.add_parser('verify', help='validate copied artifacts locally; no RKNN import')
    verify.add_argument('run_dir')
    verify.add_argument('--plots', action='store_true')
    verify.add_argument('--require-ssh-observation', action='store_true')
    verify.add_argument('--out', default='', help='derived result path; keep immutable raw directories untouched')
    observe = commands.add_parser('observe', help='root records PID survival from a fresh SSH session')
    observe.add_argument('run_dir')
    observe.add_argument('--after-ssh-disconnect', action='store_true')
    observe.add_argument('--note', required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command in ('longrun', 'faultsuite', '_case'):
        for name in ('duration', 'replay_fps', 'result_ttl'):
            if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
                raise ValueError(name + ' must be positive and finite')
        if not math.isfinite(args.save_interval) or args.save_interval < 0:
            raise ValueError('save interval must be nonnegative and finite')
        if args.run_id and (Path(args.run_id).name != args.run_id or '/' in args.run_id or '\\' in args.run_id or args.run_id in ('.', '..')):
            raise ValueError('run ID must be a single directory name')
        if args.command != 'longrun' and not 0 < args.fault_at < args.recover_at < args.duration:
            raise ValueError('require 0 < fault-at < recover-at < duration')
        return {'longrun': run_longrun, 'faultsuite': run_faultsuite, '_case': run_fault_case}[args.command](args)
    if args.command == 'verify':
        return verify_directory(args)
    return record_observation(args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError) as error:
        print('Verification error: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
