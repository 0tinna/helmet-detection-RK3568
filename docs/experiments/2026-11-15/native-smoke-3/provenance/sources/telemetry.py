"""Bounded live statistics; exact disk-backed quantiles for explicit logs."""
from collections import Counter, deque
import glob
import json
import os
from pathlib import Path
import random
import struct
import tempfile
import threading
import time

import numpy as np


TIMINGS = ('prep_ms', 'infer_ms', 'post_ms', 'capture_to_output_ms',
           'sink_ms', 'capture_to_sink_complete_ms')
COUNTERS = ('captured', 'consumed', 'detected', 'overwritten', 'active_skip',
            'read_failure', 'displayed', 'output', 'output_overwritten', 'detection_failure')
TIMING_SAMPLE_LIMIT = 2048
RUNTIME_SAMPLE_LIMIT = 1024


def quantiles(values):
    if not values:
        return {'count': 0, 'mean': None, 'p50': None, 'p95': None, 'p99': None}
    data = np.asarray(values, dtype=np.float64)
    percentiles = np.percentile(data, [50, 95, 99])
    return {'count': len(values), 'mean': float(data.mean()),
            **dict(zip(('p50', 'p95', 'p99'), map(float, percentiles)))}


class RuntimeSampler:
    def __init__(self):
        self.previous_wall = time.monotonic()
        self.previous_cpu = time.process_time()
        try:
            import psutil
            self.process = psutil.Process()
        except ImportError:
            self.process = None

    @staticmethod
    def _read(path):
        try:
            return Path(path).read_text().strip()
        except (OSError, UnicodeError):
            return None

    def sample(self):
        now, cpu = time.monotonic(), time.process_time()
        cpu_percent = 100.0 * (cpu - self.previous_cpu) / max(now - self.previous_wall, 1e-9)
        self.previous_wall, self.previous_cpu = now, cpu
        rss = None
        if self.process is not None:
            try:
                rss = self.process.memory_info().rss
            except OSError:
                pass
        if rss is None:
            status = self._read('/proc/self/status')
            if status:
                for line in status.splitlines():
                    if line.startswith('VmRSS:'):
                        rss = int(line.split()[1]) * 1024
        if rss is None and os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                            ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                            ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
            memory = ProcessMemoryCounters()
            memory.cb = ctypes.sizeof(memory)
            current_process = ctypes.windll.kernel32.GetCurrentProcess
            current_process.restype = wintypes.HANDLE
            get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
            get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
            if get_memory(current_process(), ctypes.byref(memory), memory.cb):
                rss = int(memory.WorkingSetSize)
        temperatures = {}
        for path in glob.glob('/sys/class/thermal/thermal_zone*/temp'):
            value = self._read(path)
            try:
                temperatures[path] = float(value) / 1000.0
            except (TypeError, ValueError):
                pass
        frequencies = {}
        for path in glob.glob('/sys/class/devfreq/*npu*/cur_freq'):
            value = self._read(path)
            try:
                frequencies[path] = int(value)
            except (TypeError, ValueError):
                pass
        return {'monotonic': now, 'cpu_percent': cpu_percent, 'rss_bytes': rss,
                'temperature_c': temperatures or None, 'npu_frequency_hz': frequencies or None,
                'npu_load': self._read('/sys/kernel/debug/rknpu/load')}


class Telemetry:
    def __init__(self, window_seconds=5.0, jsonl='', session_json=''):
        self.window_seconds = window_seconds
        self.lock = threading.Lock()
        self.counters = Counter({name: 0 for name in COUNTERS})
        self.counter_times = {name: deque() for name in COUNTERS}
        self.values = {name: [] for name in TIMINGS}
        self.timing_counts = Counter({name: 0 for name in TIMINGS})
        self.timing_sums = Counter({name: 0.0 for name in TIMINGS})
        self.random = random.Random(0)
        self.window_rows = deque()
        self.detector_window = deque()
        self.runtime_samples = deque(maxlen=RUNTIME_SAMPLE_LIMIT)
        self.runtime_sample_count = 0
        self.started = time.monotonic()
        self.stream = None
        self.resource_stream = None
        self.raw_timing_log = None
        self.raw_resource_log = None
        self.spools = {}
        self.exact_enabled = bool(jsonl or session_json)
        self.exact_cache = None
        self.exact_cache_counts = None
        try:
            if self.exact_enabled:
                path = Path(jsonl or (str(session_json) + '.frames.jsonl'))
                path.parent.mkdir(parents=True, exist_ok=True)
                # Explicit evidence is exclusive; preserve any previous run.
                self.stream = path.open('x', encoding='utf-8')
                self.raw_timing_log = str(path)
                resource_path = path.with_name(path.name + '.resources.jsonl')
                self.resource_stream = resource_path.open('x', encoding='utf-8')
                self.raw_resource_log = str(resource_path)
                # Spools stay alongside the explicit run, including on boards
                # where evidence must remain under /userdata rather than /tmp.
                for name in TIMINGS:
                    self.spools[name] = tempfile.TemporaryFile(dir=path.parent)
        except BaseException:
            self.close()
            raise

    def increment(self, name, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            self.counters[name] += 1
            self.counter_times[name].append(now)
            cutoff = now - self.window_seconds
            while self.counter_times[name] and self.counter_times[name][0] < cutoff:
                self.counter_times[name].popleft()

    def _write_row(self, row):
        if self.stream is not None:
            self.stream.write(json.dumps(row, allow_nan=False) + '\n')
            self.stream.flush()

    def _record_timing(self, name, value):
        value = float(value)
        self.timing_counts[name] += 1
        self.timing_sums[name] += value
        count = self.timing_counts[name]
        retained = self.values[name]
        if len(retained) < TIMING_SAMPLE_LIMIT:
            retained.append(value)
        else:
            # Algorithm R: each sample has the same inclusion probability.
            selected = self.random.randrange(count)
            if selected < TIMING_SAMPLE_LIMIT:
                retained[selected] = value
        if self.exact_enabled:
            self.spools[name].write(struct.pack('<d', value))

    def _timing_summary(self):
        if self.exact_enabled and self.exact_cache_counts == dict(self.timing_counts):
            return {name: dict(row) for name, row in self.exact_cache.items()}
        result = {}
        for name in TIMINGS:
            count = self.timing_counts[name]
            if self.exact_enabled and count:
                spool = self.spools[name]
                spool.flush()
                # Sort the disk-backed values in place. Linear interpolation
                # matches NumPy's exact all-point percentiles; no full Python
                # timing list or percentile copy is materialized in RAM.
                data = np.memmap(spool, dtype='<f8', mode='r+', shape=(count,))
                try:
                    data.sort(kind='quicksort')
                    row = {}
                    for key, fraction in (('p50', .5), ('p95', .95), ('p99', .99)):
                        position = (count - 1) * fraction
                        low, high = int(np.floor(position)), int(np.ceil(position))
                        row[key] = float(data[low] + (data[high] - data[low]) * (position - low))
                    data.flush()
                finally:
                    data._mmap.close()
            else:
                row = quantiles(self.values[name])
            row.update(count=count, mean=self.timing_sums[name] / count if count else None,
                       mean_method='all_samples',
                       sample_count=count if self.exact_enabled else len(self.values[name]),
                       percentile_method=('exact_linear_disk_spool' if self.exact_enabled else
                                          'exact_linear' if count <= TIMING_SAMPLE_LIMIT else
                                          'uniform_reservoir_linear'))
            result[name] = row
        if self.exact_enabled:
            self.exact_cache = {name: dict(row) for name, row in result.items()}
            self.exact_cache_counts = dict(self.timing_counts)
        return result

    def record_detection(self, row):
        """Count every detector timing, including results never delivered."""
        with self.lock:
            self._write_row({**row, 'record_type': 'detection', 'capture_to_output_ms': None})
            for name in ('prep_ms', 'infer_ms', 'post_ms'):
                self._record_timing(name, row[name])
            self.detector_window.append(row)
            cutoff = row['processed_at'] - self.window_seconds
            while self.detector_window and self.detector_window[0]['processed_at'] < cutoff:
                self.detector_window.popleft()

    def record(self, row):
        with self.lock:
            self._write_row({**row, 'record_type': 'output'})
            for name in ('capture_to_output_ms', 'sink_ms', 'capture_to_sink_complete_ms'):
                self._record_timing(name, row[name])
            self.window_rows.append(row)
            cutoff = row['output_at'] - self.window_seconds
            while self.window_rows and self.window_rows[0]['output_at'] < cutoff:
                self.window_rows.popleft()

    def add_sample(self, sample):
        with self.lock:
            if self.resource_stream is not None:
                self.resource_stream.write(json.dumps(sample, allow_nan=False) + '\n')
                self.resource_stream.flush()
            self.runtime_sample_count += 1
            self.runtime_samples.append(sample)

    def summary(self, now=None, include_full=True):
        now = time.monotonic() if now is None else now
        elapsed = max(now - self.started, 1e-9)
        with self.lock:
            cutoff = now - self.window_seconds
            for times in self.counter_times.values():
                while times and times[0] < cutoff:
                    times.popleft()
            while self.window_rows and self.window_rows[0]['output_at'] < cutoff:
                self.window_rows.popleft()
            while self.detector_window and self.detector_window[0]['processed_at'] < cutoff:
                self.detector_window.popleft()
            window_elapsed = max(min(elapsed, self.window_seconds), 1e-9)
            return {'elapsed_seconds': elapsed, 'counters': dict(self.counters),
                    'full_session': self._timing_summary() if include_full else None,
                    'timing_retention': {'method': 'disk_spool' if self.exact_enabled else 'uniform_reservoir',
                                         'live_sample_limit_per_timing': TIMING_SAMPLE_LIMIT,
                                         'reservoir_seed': 0, 'raw_log': self.raw_timing_log},
                    'full_session_fps': {name: self.counters[name] / elapsed
                                         for name in ('captured', 'consumed', 'detected', 'displayed', 'output')},
                    'sliding_window': {
                        'seconds': self.window_seconds,
                        'observed_seconds': window_elapsed,
                        'fps': {name: len(self.counter_times[name]) / window_elapsed
                                for name in ('captured', 'consumed', 'detected', 'displayed', 'output')},
                        'timings': {name: quantiles([row[name] for row in
                                     (self.detector_window if name in ('prep_ms', 'infer_ms', 'post_ms') else self.window_rows)])
                                    for name in TIMINGS}},
                    'runtime_samples': list(self.runtime_samples) if include_full else None,
                    'runtime_sample_retention': {'method': 'latest_samples', 'limit': RUNTIME_SAMPLE_LIMIT,
                                                 'total_count': self.runtime_sample_count,
                                                 'retained_count': len(self.runtime_samples),
                                                 'raw_log': self.raw_resource_log}}

    def close(self):
        with self.lock:
            failure = None
            try:
                if self.exact_enabled and len(self.spools) == len(TIMINGS) and any(not stream.closed for stream in self.spools.values()):
                    self._timing_summary()
            except BaseException as error:
                failure = error
            for name in ('stream', 'resource_stream'):
                stream = getattr(self, name)
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException as error:
                        failure = failure or error
                    setattr(self, name, None)
            for spool in self.spools.values():
                try:
                    spool.close()
                except BaseException as error:
                    failure = failure or error
            if failure is not None:
                raise failure


def write_session(path, summary):
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement permits --session-json to be polled without partial JSON.
    temporary = target.with_name(target.name + '.%d.tmp' % os.getpid())
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(summary, stream, indent=2, allow_nan=False)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
