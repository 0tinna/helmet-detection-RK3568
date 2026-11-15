#!/usr/bin/env python3
"""Read a terminal native run and create new descriptive stability artifacts.

Run on the host, never on the board while an experiment is running. Raw input,
manifests and production code are read-only. The existing strict verifier owns
acceptance policy; these calculations are descriptive analysis, not an
independent audit, a leak diagnosis or a matched speedup experiment.
"""
import sys

sys.dont_write_bytecode = True

import argparse
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import statistics


MIB = 1024 ** 2
WARMUP_SECONDS = 60.0
TAIL_SECONDS = 300.0
STAGES = {
    'detection': ('prep_ms', 'infer_ms', 'post_ms'),
    'output': ('capture_to_output_ms', 'sink_ms', 'capture_to_sink_complete_ms'),
}
LIMITS = [
    'RSS is reported separately for Python and Qt. Shared mappings can appear in both; no sum is unique physical memory.',
    'RSS growth and ordinary least-squares slopes describe this run; zero or positive slopes do not prove absence or presence of a leak.',
    'GUI ratios count observations, not rendered frames. They do not establish continuous visibility or actual display FPS.',
    'Callback dispatch and sink completion exclude later HTTP transfer, Qt image decoding and physical presentation.',
    'Replay supply 20 FPS and public detection/output target 10 FPS are distinct. File-header FPS and camera capture are separate.',
    'Historical pure-Pipeline, old-service and native runs differ in pacing, GUI/JPEG, logging, snapshots, code and thermal conditions; changes are not a causal pure speedup.',
    'Physical camera, human viewing/touch, WiFi switching/authentication/DHCP, independent 85% accuracy and final model/configuration acceptance remain separate.',
    'This script implements analysis and does not independently audit its own conclusions. A separate reviewer must check raw evidence and formulae.',
]


def is_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def distribution(values):
    """Equal-sample mean and exact linear-interpolated quantiles at (n-1)*q."""
    values = list(values)
    kept = sorted(float(v) for v in values if is_number(v))
    result = {'count': len(kept), 'rejected_count': len(values) - len(kept),
              'mean': None, 'min': None, 'max': None, 'p50': None,
              'p95': None, 'p99': None, 'population_stddev': None,
              'method': 'all finite samples; equal-sample mean; linear quantile at (n-1)*q'}
    if kept:
        def quantile(q):
            index = (len(kept) - 1) * q
            low, high = math.floor(index), math.ceil(index)
            return kept[low] + (kept[high] - kept[low]) * (index - low)
        result.update(mean=math.fsum(kept) / len(kept), min=kept[0], max=kept[-1],
                      p50=quantile(.5), p95=quantile(.95), p99=quantile(.99),
                      population_stddev=statistics.pstdev(kept))
    return result


def linear_trend(points):
    """Unweighted OLS y versus actual monotonic seconds; no significance claim."""
    points = list(points)
    if any(not is_number(t) or not is_number(y) for t, y in points):
        raise ValueError('trend requires finite timestamps and measurements')
    if any(b[0] < a[0] for a, b in zip(points, points[1:])):
        raise ValueError('trend timestamps must be ordered; never sort away a clock defect')
    result = {'count': len(points), 'reference_time': None, 'span_seconds': None,
              'observed_change': None, 'slope_per_second': None,
              'fitted_value_at_reference': None, 'r_squared': None,
              'method': 'unweighted ordinary least squares against actual monotonic time; descriptive only'}
    if not points:
        return result
    origin = points[0][0]
    xs = [t - origin for t, _ in points]
    ys = [y for _, y in points]
    result.update(reference_time=origin, span_seconds=points[-1][0] - origin,
                  observed_change=ys[-1] - ys[0])
    x_mean, y_mean = math.fsum(xs) / len(xs), math.fsum(ys) / len(ys)
    xx = math.fsum((x - x_mean) ** 2 for x in xs)
    if len(points) < 2 or xx == 0:
        return result
    slope = math.fsum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / xx
    intercept = y_mean - slope * x_mean
    yy = math.fsum((y - y_mean) ** 2 for y in ys)
    residual = math.fsum((y - intercept - slope * x) ** 2 for x, y in zip(xs, ys))
    result.update(slope_per_second=slope, fitted_value_at_reference=intercept,
                  r_squared=None if yy == 0 else 1 - residual / yy)
    return result


def rss_statistics(points):
    values = [v for _, v in points]
    result = distribution(values)
    trend = linear_trend(points)
    result.update(start_bytes=values[0] if values else None,
                  end_bytes=values[-1] if values else None,
                  peak_bytes=max(values) if values else None,
                  growth_bytes=values[-1] - values[0] if values else None,
                  slope_bytes_per_second=trend['slope_per_second'],
                  slope_mib_per_minute=None if trend['slope_per_second'] is None
                  else trend['slope_per_second'] * 60 / MIB,
                  trend=trend)
    return result


def window_bounds(start, stop, warmup_seconds=60.0, tail_seconds=300.0):
    if not all(is_number(v) for v in (start, stop, warmup_seconds, tail_seconds)):
        raise ValueError('window bounds must be finite')
    if stop < start or warmup_seconds < 0 or tail_seconds <= 0:
        raise ValueError('invalid window bounds')
    return {'active': (start, stop), 'after_warmup': (start + warmup_seconds, stop),
            'tail': (max(start, stop - tail_seconds), stop)}


def process_statistics(resources, process, start, stop):
    selected, rejected, identities = [], 0, set()
    for parent in resources:
        row = parent if process == 'python' else parent.get('gui')
        if not isinstance(row, dict):
            continue
        stamp = row.get('monotonic')
        if not is_number(stamp):
            rejected += 1
            continue
        if start <= stamp <= stop:
            selected.append(row)
            if row.get('pid') is not None:
                identities.add((row['pid'], row.get('start_id')))
    rss_points = [(r['monotonic'], r['rss_bytes']) for r in selected
                  if is_number(r.get('rss_bytes')) and r['rss_bytes'] > 0]
    cpu = [r.get('cpu_percent') for r in selected
           if is_number(r.get('cpu_percent')) and r['cpu_percent'] >= 0]
    result = {'sample_rows': len(selected), 'invalid_timestamp_rows': rejected,
              'missing_or_invalid_rss_rows': len(selected) - len(rss_points),
              'missing_or_invalid_cpu_rows': len(selected) - len(cpu),
              'identities': [{'pid': p, 'start_id': s} for p, s in sorted(identities, key=str)],
              'identity_consistent': len(identities) == 1 if identities else None,
              'rss': rss_statistics(rss_points), 'cpu_percent': distribution(cpu)}
    if len(identities) > 1:
        result['rss'].update(slope_bytes_per_second=None, slope_mib_per_minute=None,
                             trend={'reason': 'multiple process identities; no cross-process slope fitted'})
    return result


def summarize_resource_windows(resources, active_start, active_stop,
                               warmup_seconds=60.0, tail_seconds=300.0):
    result = {}
    for name, (start, stop) in window_bounds(active_start, active_stop, warmup_seconds, tail_seconds).items():
        result[name] = {'start_monotonic': start, 'stop_monotonic': stop,
                        'empty_requested_window': start > stop,
                        'python': process_statistics(resources, 'python', start, stop),
                        'qt': process_statistics(resources, 'qt', start, stop)}
        for field in ('temperature_c', 'npu_frequency_hz'):
            series = {}
            for row in resources:
                if not is_number(row.get('monotonic')) or not start <= row['monotonic'] <= stop:
                    continue
                for channel, value in (row.get(field) or {}).items():
                    if is_number(value) and (field != 'npu_frequency_hz' or value > 0):
                        series.setdefault(channel, []).append(value)
            result[name][field] = {channel: distribution(values) for channel, values in series.items()}
    return result


def summarize_timings(frames, start=None, stop=None):
    result = {}
    for kind, fields in STAGES.items():
        boundary = 'processed_at' if kind == 'detection' else 'callback_finished_at'
        rows = [r for r in frames if r.get('record_type') == kind and
                (start is None or is_number(r.get(boundary)) and start <= r[boundary] <= stop)]
        for field in fields:
            result[field] = {**distribution([r.get(field) for r in rows]),
                             'record_type': kind, 'window_timestamp': boundary, 'unit': 'ms'}
    return result


def gui_sample_statistics(log):
    rows, malformed = [], 0
    for line in log.splitlines():
        if 'NATIVE_PREVIEW_OBSERVATION ' not in line:
            continue
        try:
            row = json.loads(line.split('NATIVE_PREVIEW_OBSERVATION ', 1)[1])
            if not isinstance(row, dict) or not is_number(row.get('observed_wall_ms')):
                raise ValueError('invalid GUI observation')
            rows.append(row)
        except (ValueError, TypeError):
            malformed += 1
    groups = {}
    for name, subset in [('all_samples', rows), ('control_phase_running', [r for r in rows if r.get('phase') == 'running'])]:
        available = sum(r.get('ready') is True and r.get('visible') is True and
                        r.get('sourceKind') == 'inline-jpeg' for r in subset)
        healthy = sum(r.get('ready') is True and r.get('visible') is True and
                      r.get('sourceKind') == 'inline-jpeg' and r.get('valid') is True and
                      r.get('running') is True for r in subset)
        groups[name] = {'sample_count': len(subset), 'ready_visible_inline_samples': available,
                        'healthy_ready_visible_inline_samples': healthy,
                        'unavailable_samples': len(subset) - available,
                        'ready_visible_inline_ratio': available / len(subset) if subset else None,
                        'healthy_ready_visible_inline_ratio': healthy / len(subset) if subset else None}
    return {'groups': groups, 'malformed_rows': malformed, 'nominal_poll_seconds': .5,
            'population': 'recorded GUI control observations; phase-running is not an exact monotonic Pipeline active-window filter',
            'actual_display_fps': None, 'continuous_visibility_accepted': False,
            'observations': rows}


def file_record(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return {'path': str(path.resolve()), 'bytes': path.stat().st_size, 'sha256': digest.hexdigest()}


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def read_rows(path):
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError('JSONL rows must be objects: ' + path.name)
    return rows


def strict_verification(run_dir):
    path = Path(__file__).with_name('verify_native_runtime.py')
    spec = importlib.util.spec_from_file_location('stability_external_verifier', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.verify_evidence(run_dir)
    names = ('passed', 'errors', 'timeline', 'observed_rates', 'throughput_target_met',
             'public_target_fps', 'requested_replay_fps', 'requested_supply_target_met',
             'native_gui_verified', 'thirty_minute_app_passed', 'http', 'cleanup_evidence')
    return {'implementation': file_record(path), 'method': 'read-only execution of the existing strict verifier; not a separate independent audit',
            **{name: result.get(name) for name in names}}


def analyze_run(run_dir, warmup_seconds=60.0, tail_seconds=300.0):
    run_dir = Path(run_dir).resolve()
    manifest = read_json(run_dir / 'manifest.json')
    if not isinstance(manifest, dict) or manifest.get('status') not in ('completed', 'failed'):
        raise ValueError('analysis requires a terminal completed/failed manifest; never analyze a running job as final')
    if not is_number(warmup_seconds) or warmup_seconds < 0 or not is_number(tail_seconds) or tail_seconds <= 0:
        raise ValueError('warmup must be finite/nonnegative and tail finite/positive')
    frames, resources, events, http = [read_rows(run_dir / name) for name in
                                     ('frames.jsonl', 'resources.jsonl', 'events.jsonl', 'http.jsonl')]
    sidecar = read_rows(run_dir / 'frames.jsonl.resources.jsonl')
    session = read_json(run_dir / 'session.json')
    if not isinstance(session, dict):
        raise ValueError('session must be an object')
    log = (run_dir / 'gui.log').read_text(encoding='utf-8') if (run_dir / 'gui.log').is_file() else ''
    verification = strict_verification(run_dir)
    starts = [e.get('monotonic') for e in events if e.get('state') == 'startup']
    stops = [e.get('monotonic') for e in events if e.get('state') == 'stop']
    valid_interval = len(starts) == len(stops) == 1 and all(is_number(v) for v in starts + stops) and starts[0] < stops[0]
    timeline = {'available': valid_interval, 'warmup_exclusion_seconds': warmup_seconds,
                'tail_requested_seconds': tail_seconds,
                'window_policy': 'inclusive actual monotonic boundaries; short tails truncate to active start; an empty after-warmup window stays empty'}
    resource_windows, timing_windows = {}, {'all_logged': summarize_timings(frames)}
    if valid_interval:
        start, stop = starts[0], stops[0]
        timeline.update(active_start=start, active_stop=stop, observed_active_seconds=stop - start)
        resource_windows = summarize_resource_windows(resources, start, stop, warmup_seconds, tail_seconds)
        for name, (low, high) in window_bounds(start, stop, warmup_seconds, tail_seconds).items():
            timing_windows[name] = summarize_timings(frames, low, high)
    else:
        timeline['reason'] = 'one ordered startup/stop interval is absent; no active or steady-state duration inferred'
    gui = gui_sample_statistics(log)
    result = {'schema_version': 1, 'kind': 'descriptive-native-stability-analysis',
              'run_id': manifest.get('run_id'), 'run_dir': str(run_dir),
              'input_manifest_status': manifest.get('status'), 'input_exit_code': manifest.get('exit_code'),
              'analysis_script': file_record(Path(__file__)), 'verification': verification,
              'gates': {'strict_integrity_passed': verification.get('passed'),
                        'sampled_http_policy_passed': not (verification.get('http') or {}).get('errors', ['missing HTTP validation']),
                        'automatic_native_gui_verified': verification.get('native_gui_verified'),
                        'thirty_minute_app_passed': verification.get('thirty_minute_app_passed'),
                        'fixed_10_fps_met': verification.get('throughput_target_met'),
                        'rss_no_leak_gate': None, 'continuous_display_gate': None,
                        'physical_camera_accepted': False, 'human_display_touch_accepted': False,
                        'actual_wifi_switching_accepted': False, 'independent_85_accuracy_accepted': False,
                        'final_model_configuration_accepted': False},
              'raw_counts': {'frame_rows': len(frames), 'detection_rows': sum(r.get('record_type') == 'detection' for r in frames),
                             'output_rows': sum(r.get('record_type') == 'output' for r in frames),
                             'resource_rows': len(resources), 'pipeline_resource_rows': len(sidecar),
                             'event_rows': len(events), 'http_rows': len(http),
                             'gui_observation_rows': len(gui['observations'])},
              'original_counters': session.get('counters'), 'actual_engine_calls': manifest.get('actual_engine_calls'),
              'actual_source_reads': manifest.get('actual_source_reads'),
              'actual_source_read_failures': manifest.get('actual_source_read_failures'),
              'timeline': timeline, 'resources': resource_windows, 'timings_ms': timing_windows,
              'gui_samples': gui, 'conditions': {k: manifest.get(k) for k in
                  ('model', 'input', 'actual_source', 'thresholds', 'input_size', 'requested_duration_seconds',
                   'result_ttl', 'replay_fps', 'public_target_fps',
                   'environment', 'governors', 'evidence_configuration', 'launch_path', 'sources')},
              'cleanup': {k: manifest.get(k) for k in ('cleanup', 'cleanup_errors', 'service_cleanup', 'gui')},
              'raw_files': {name: file_record(run_dir / name) for name in
                            ('manifest.json', 'session.json', 'frames.jsonl', 'frames.jsonl.resources.jsonl',
                             'resources.jsonl', 'events.jsonl', 'http.jsonl', 'cleanup.json', 'gui.log')
                            if (run_dir / name).is_file()},
              'limitations': LIMITS}
    return result, {'frames': frames, 'resources': resources, 'gui': gui['observations']}


def write_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)


def write_csv(path, rows, fields):
    with path.open('x', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def export_stats(directory, result, raw):
    directory.mkdir()
    origin = result['timeline'].get('active_start')
    rows = []
    for parent in raw['resources']:
        for name, row in [('python', parent), ('qt', parent.get('gui'))]:
            if not isinstance(row, dict):
                continue
            rows.append({'process': name, 'monotonic': row.get('monotonic'),
                         'seconds_from_active_start': row['monotonic'] - origin if is_number(row.get('monotonic')) and origin is not None else None,
                         'pid': row.get('pid'), 'start_id': row.get('start_id'),
                         'rss_bytes': row.get('rss_bytes'), 'cpu_percent': row.get('cpu_percent')})
    write_csv(directory / 'process-resources.csv', rows,
              ('process', 'monotonic', 'seconds_from_active_start', 'pid', 'start_id', 'rss_bytes', 'cpu_percent'))
    metrics = [field for fields in STAGES.values() for field in fields]
    timing_rows = []
    for row in raw['frames']:
        stamp = row.get('processed_at') if row.get('record_type') == 'detection' else row.get('callback_finished_at')
        timing_rows.append({'record_type': row.get('record_type'), 'frame_id': row.get('frame_id'),
                            'boundary_monotonic': stamp,
                            'seconds_from_active_start': stamp - origin if is_number(stamp) and origin is not None else None,
                            **{field: row.get(field) for field in metrics}})
    write_csv(directory / 'timing-samples.csv', timing_rows,
              ('record_type', 'frame_id', 'boundary_monotonic', 'seconds_from_active_start', *metrics))
    gui_rows = [{k: r.get(k) for k in ('observed_wall_ms', 'phase', 'ready', 'visible', 'sourceKind', 'valid', 'running')}
                for r in raw['gui']]
    write_csv(directory / 'gui-sampled-availability.csv', gui_rows,
              ('observed_wall_ms', 'phase', 'ready', 'visible', 'sourceKind', 'valid', 'running'))
    write_json(directory / 'phase-statistics.json', {'timeline': result['timeline'], 'resources': result['resources'],
                                                    'timings_ms': result['timings_ms'],
                                                    'gui_ratios': result['gui_samples']['groups']})


def render_figures(directory, result, raw):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    directory.mkdir()
    origin = result['timeline'].get('active_start')
    if origin is None:
        return {'created': [], 'reason': 'no ordered active interval; no active plots produced'}
    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    for name, color in [('python', '#175d9c'), ('qt', '#b15b19')]:
        points = [r if name == 'python' else r.get('gui') for r in raw['resources']]
        for ax, field, scale in [(axes[0], 'rss_bytes', MIB), (axes[1], 'cpu_percent', 1)]:
            samples = [(r['monotonic'] - origin, r[field] / scale) for r in points
                       if isinstance(r, dict) and is_number(r.get('monotonic')) and is_number(r.get(field))]
            if samples:
                ax.plot(*zip(*samples), label=name, color=color, linewidth=1)
    axes[0].set_ylabel('RSS per process (MiB)')
    axes[0].set_title('Separate process RSS; shared mappings may appear in both')
    axes[1].set_ylabel('Process CPU (%)')
    axes[1].set_xlabel('Seconds from observed active start (monotonic)')
    for ax in axes:
        ax.axvline(0, color='gray', linestyle=':', label='active start')
        ax.axvline(result['timeline']['observed_active_seconds'], color='gray', linestyle=':', label='active stop')
        if result['timeline']['warmup_exclusion_seconds'] <= result['timeline']['observed_active_seconds']:
            ax.axvline(result['timeline']['warmup_exclusion_seconds'], color='gray', linestyle='--', label='after-warmup boundary')
        ax.grid(alpha=.2)
        ax.legend(loc='best')
    fig.tight_layout()
    fig.savefig(directory / 'process-rss-cpu.png', dpi=160)
    plt.close(fig)
    fig, axes = plt.subplots(2, 1, figsize=(10, 7))
    for ax, kind in zip(axes, ('detection', 'output')):
        fields = STAGES[kind]
        for offset, phase in [(-.18, 'active'), (.18, 'tail')]:
            values = [result['timings_ms'].get(phase, {}).get(f, {}).get('p95') for f in fields]
            ax.bar([i + offset for i in range(len(fields))], [v if v is not None else float('nan') for v in values],
                   width=.35, label=phase + ' p95')
        ax.set_xticks(range(len(fields)), fields)
        ax.set_ylabel('Milliseconds')
        ax.legend()
        ax.grid(axis='y', alpha=.2)
    axes[0].set_title('All-active versus requested-tail p95; exact quantiles in stats/JSON')
    fig.tight_layout()
    fig.savefig(directory / 'latency-active-tail-p95.png', dpi=160)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(10, 3.5))
    if raw['gui']:
        first = raw['gui'][0]['observed_wall_ms']
        for available, color, label in [(True, '#287a44', 'ready/visible JPEG sample'),
                                        (False, '#ab3d35', 'unavailable sample')]:
            points = [((r['observed_wall_ms'] - first) / 1000, int(available)) for r in raw['gui']
                      if (r.get('ready') is True and r.get('visible') is True and r.get('sourceKind') == 'inline-jpeg') == available]
            if points:
                ax.scatter(*zip(*points), s=12, color=color, label=label)
        ax.legend()
    ax.set_title('GUI observations only; no continuous visibility or display FPS claim')
    ax.set_xlabel('Seconds from first GUI observation (wall clock)')
    ax.set_yticks([0, 1], ['unavailable', 'ready/visible'])
    ax.set_ylim(-.2, 1.2)
    ax.grid(alpha=.2)
    fig.tight_layout()
    fig.savefig(directory / 'gui-sampled-availability.png', dpi=160)
    plt.close(fig)
    return {'created': ['process-rss-cpu.png', 'latency-active-tail-p95.png', 'gui-sampled-availability.png'],
            'backend': 'matplotlib Agg', 'raw_samples_smoothed': False}


def report_markdown(result):
    def number(value):
        return 'n/a' if value is None else format(value, '.6g')
    lines = ['# Native stability analysis', '',
             'This is descriptive analysis of a terminal recorded run, not an independent acceptance audit.', '',
             f"Run: `{result['run_id']}`; manifest: `{result['input_manifest_status']}`; exit: `{result['input_exit_code']}`.", '',
             '## Separate acceptance gates', '',
             '| Gate | Recorded strict-verifier result |', '| --- | --- |']
    lines += [f'| {key} | {value} |' for key, value in result['gates'].items()]
    conditions = result['conditions']
    source = conditions.get('actual_source') or {}
    lines += ['', '## Conditions, coverage and original counts', '',
              f"Requested duration: {number(conditions.get('requested_duration_seconds'))} seconds; observed active: {number(result['timeline'].get('observed_active_seconds'))} seconds.",
              f"Windows: full active, after the first {number(result['timeline']['warmup_exclusion_seconds'])} seconds, and requested last {number(result['timeline']['tail_requested_seconds'])} seconds (truncated for a shorter run).",
              f"Model input: {conditions.get('input_size')}; decoded source: {source.get('decoded_image_size')}; source kind: {source.get('kind')}.",
              f"Replay pacer: {number(conditions.get('replay_fps'))} FPS; file header: {number(source.get('header_fps'))} FPS; fixed public target: {number(conditions.get('public_target_fps'))} FPS.",
              f"Operating thresholds: {conditions.get('thresholds')}; result TTL: {number(conditions.get('result_ttl'))} seconds.", '',
              '| Stream | Raw row count |', '| --- | ---: |']
    lines += [f'| {key} | {value} |' for key, value in result['raw_counts'].items()]
    lines += ['', f"Original counters: `{json.dumps(result['original_counters'], sort_keys=True)}`.",
              f"Actual engine calls: {result['actual_engine_calls']}; source reads: {result['actual_source_reads']}; source read failures: {result['actual_source_read_failures']}.",
              f"Strict-verifier observed rates: `{json.dumps(result['verification'].get('observed_rates'), sort_keys=True)}`.",
              'The displayed_fps counter belongs to the OpenCV display path; actual Qt display FPS is not measured. Model/input/code/runtime hashes, versions, governor read-backs and direct cleanup receipts are in stability-results.json.']
    lines += ['', '## RSS by actual process and window', '',
              '| Window/process | Samples | Start MiB | End MiB | Peak MiB | Growth MiB | OLS MiB/min |',
              '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for window, data in result['resources'].items():
        for process in ('python', 'qt'):
            r = data[process]['rss']
            vals = [r.get(k) / MIB if r.get(k) is not None else None
                    for k in ('start_bytes', 'end_bytes', 'peak_bytes', 'growth_bytes')]
            lines.append('| ' + window + '/' + process + ' | ' + str(r['count']) + ' | ' +
                         ' | '.join(number(v) for v in vals + [r['slope_mib_per_minute']]) + ' |')
    lines += ['', 'RSS slopes are unweighted OLS against actual monotonic sample times, with no leak pass/fail threshold.',
              '', '| Window/process | Mean MiB | p50 MiB | p95 MiB | p99 MiB |',
              '| --- | ---: | ---: | ---: | ---: |']
    for window, data in result['resources'].items():
        for process in ('python', 'qt'):
            r = data[process]['rss']
            lines.append('| ' + window + '/' + process + ' | ' +
                         ' | '.join(number(r.get(k) / MIB if r.get(k) is not None else None)
                                    for k in ('mean', 'p50', 'p95', 'p99')) + ' |')
    lines += [
              '', '## Exact timing distributions', '',
              '| Window/metric | Count | Mean ms | p50 ms | p95 ms | p99 ms |',
              '| --- | ---: | ---: | ---: | ---: | ---: |']
    for phase, stats in result['timings_ms'].items():
        for metric, values in stats.items():
            lines.append('| ' + phase + '/' + metric + ' | ' + str(values['count']) + ' | ' +
                         ' | '.join(number(values.get(k)) for k in ('mean', 'p50', 'p95', 'p99')) + ' |')
    lines += ['', 'Quantiles use linear interpolation at (n-1)*q; JSON preserves full numerical precision.',
              '', '## Sampled GUI availability', '']
    for population, data in result['gui_samples']['groups'].items():
        lines.append(f"- {population}: {data['ready_visible_inline_samples']}/{data['sample_count']} ready/visible inline JPEG samples; ratio {number(data['ready_visible_inline_ratio'])}.")
    lines += ['', '## Interpretation limits', ''] + ['- ' + text for text in LIMITS]
    if result['verification'].get('errors'):
        lines += ['', '## Preserved strict-verifier failures', ''] + ['- ' + str(e) for e in result['verification']['errors']]
    return '\n'.join(lines) + '\n'


def build_report(run_dir, out_dir, warmup_seconds=60.0, tail_seconds=300.0, plots=True):
    run_dir, requested_out = Path(run_dir).resolve(), Path(out_dir)
    if requested_out.exists() or requested_out.is_symlink():
        raise FileExistsError('report output must be a new directory')
    out_dir = requested_out.resolve()
    try:
        out_dir.relative_to(run_dir)
    except ValueError:
        pass
    else:
        raise ValueError('derived report directory must stay outside the immutable raw run')
    if out_dir.exists() or out_dir.is_symlink():
        raise FileExistsError('report output must be a new directory')
    result, raw = analyze_run(run_dir, warmup_seconds, tail_seconds)
    out_dir.mkdir(parents=True, mode=0o700)
    export_stats(out_dir / 'stats', result, raw)
    result['figures'] = render_figures(out_dir / 'figures', result, raw) if plots else {'created': [], 'reason': 'plots explicitly disabled'}
    (out_dir / 'stability-report.md').write_text(report_markdown(result), encoding='utf-8')
    result['derived_artifacts'] = {p.relative_to(out_dir).as_posix(): file_record(p)
                                   for p in out_dir.rglob('*') if p.is_file()}
    write_json(out_dir / 'stability-results.json', result)
    return result


def self_check():
    """Small independently hand-computed numerical fixtures; no real run audit."""
    d = distribution([0, 10, 20, 30])
    assert d['count'] == 4 and d['mean'] == 15 and d['p50'] == 15
    assert math.isclose(d['p95'], 28.5) and math.isclose(d['p99'], 29.7)
    t = linear_trend([(0, 100), (60, 130), (120, 160), (180, 190)])
    assert t['slope_per_second'] == .5 and t['r_squared'] == 1 and t['observed_change'] == 90
    rows = [{'monotonic': stamp, 'rss_bytes': value, 'cpu_percent': 0,
             'gui': {'monotonic': stamp, 'rss_bytes': value * 2, 'cpu_percent': 0}}
            for stamp, value in [(0, 100), (60, 130), (120, 160), (180, 190)]]
    w = summarize_resource_windows(rows, 0, 180, 60, 120)
    assert w['after_warmup']['python']['rss']['start_bytes'] == 130
    assert w['after_warmup']['qt']['rss']['start_bytes'] == 260
    assert w['tail']['python']['rss']['count'] == 3 and 'total_rss' not in w['active']
    assert summarize_resource_windows(rows[:1], 0, 10, 60, 300)['after_warmup']['python']['rss']['count'] == 0
    assert distribution([0, False, float('nan')])['count'] == 1
    assert linear_trend([(0, 100), (1, 100)])['r_squared'] is None
    assert linear_trend([(0, 100)])['slope_per_second'] is None
    try:
        linear_trend([(1, 100), (0, 200)])
    except ValueError:
        pass
    else:
        raise AssertionError('reversed timestamps were hidden')
    print('Numerical self-check passed: literal quantiles, OLS, process separation, actual windows and invalid-input cases.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', nargs='?')
    parser.add_argument('--out-dir')
    parser.add_argument('--warmup-seconds', type=float, default=WARMUP_SECONDS)
    parser.add_argument('--tail-seconds', type=float, default=TAIL_SECONDS)
    parser.add_argument('--no-plots', action='store_true')
    parser.add_argument('--self-check', action='store_true')
    args = parser.parse_args(argv)
    if args.self_check:
        self_check()
        return 0
    if not args.run_dir or not args.out_dir:
        parser.error('run_dir and --out-dir are required unless --self-check is used')
    result = build_report(args.run_dir, args.out_dir, args.warmup_seconds, args.tail_seconds, not args.no_plots)
    print(json.dumps({'out_dir': str(Path(args.out_dir).resolve()),
                      'strict_verifier_passed': result['verification'].get('passed'),
                      'thirty_minute_app_passed': result['gates']['thirty_minute_app_passed']}))
    return 0 if result['verification'].get('passed') is True else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, ImportError) as error:
        print('Stability analysis failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
