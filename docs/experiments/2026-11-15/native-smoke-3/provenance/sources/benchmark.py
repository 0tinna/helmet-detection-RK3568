#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Board benchmark with explicit governor restoration and SDK ownership.

Inference, repeated postprocessing of the final raw heads, and end-to-end
preprocess/infer/postprocessing remain separate phases. --json preserves the
legacy result list. --manifest optionally records identities, raw loop timings,
failures and governor read-backs. SIGINT/TERM/HUP unwind cleanup; SIGKILL cannot.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import signal
import sys
import time

import cv2
import numpy as np

if __package__:
    from .yolo_post import letterbox, postprocess
else:
    from yolo_post import letterbox, postprocess

GOVERNOR_PATH = Path('/sys/class/devfreq/fde40000.npu/governor')
RKNN_CLS = None


class BenchmarkInterrupted(BaseException):
    def __init__(self, number):
        self.number = number
        super().__init__('shutdown signal %d' % number)


@contextmanager
def shutdown_signals():
    """Defer signals received during cleanup; restore the caller's handlers."""
    state = {'signal': None, 'cleaning': False}
    previous = {}
    def interrupt(number, frame):
        if state['signal'] is None:
            state['signal'] = number
            if not state['cleaning']:
                raise BenchmarkInterrupted(number)
    try:
        for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, interrupt)
        yield state
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def get_sdk_class():
    if RKNN_CLS is not None:
        return RKNN_CLS
    try:
        from rknnlite.api import RKNNLite
        return RKNNLite
    except ImportError:
        from rknn.api import RKNN
        return RKNN


def get_temp():
    try:
        return int(Path('/sys/class/thermal/thermal_zone0/temp').read_text().strip()) / 1000.
    except (OSError, ValueError):
        return -1  # Retained legacy unavailable-temperature sentinel.


def parse_size(path):
    base = os.path.splitext(os.path.basename(path))[0]
    for part in base.split('_'):
        if 'x' in part:
            try:
                w, h = part.split('x')
                return int(w), int(h)
            except ValueError:
                pass
    return 640, 640


def validate_parameters(loops, warmup, conf):
    if isinstance(loops, bool) or not isinstance(loops, int) or loops <= 0:
        raise ValueError('loops must be a positive integer')
    if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 0:
        raise ValueError('warmup must be a nonnegative integer')
    if isinstance(conf, bool) or not isinstance(conf, (float, int)) or not math.isfinite(conf) or not 0 < conf <= 1:
        raise ValueError('conf must be finite and within (0,1]')


def run_phase(loops, action, samples=None):
    started = time.perf_counter()
    result = None
    for _ in range(loops):
        before = time.perf_counter() if samples is not None else None
        result = action()
        if samples is not None:
            samples.append((time.perf_counter() - before) * 1000.)
    return result, time.perf_counter() - started


def bench_one(model_path, image, loops=30, warmup=5, conf=.25,
              measurement=None, shutdown_state=None):
    validate_parameters(loops, warmup, conf)
    w, h = parse_size(model_path)
    rknn = get_sdk_class()(verbose=False)
    try:
        if measurement is not None:
            measurement.update(size=[w, h], sdk='%s.%s' % (type(rknn).__module__, type(rknn).__name__),
                               stage='load', infer_ms_samples=[], post_ms_samples=[], e2e_ms_samples=[])
        if rknn.load_rknn(str(model_path)) != 0:
            return None
        if measurement is not None:
            measurement['stage'] = 'init'
        if rknn.init_runtime() != 0:
            return None
        lb, ratio, pad = letterbox(image, w, h)
        inp = cv2.cvtColor(lb, cv2.COLOR_BGR2RGB)[np.newaxis].astype(np.uint8)
        sh, sw = image.shape[:2]
        if measurement is not None:
            measurement['stage'] = 'warmup'
        for _ in range(warmup):
            rknn.inference(inputs=[inp], data_format='nhwc')
        if measurement is not None:
            measurement['stage'] = 'infer'
        outs, infer_total = run_phase(loops, lambda: rknn.inference(inputs=[inp], data_format='nhwc'),
                                     measurement['infer_ms_samples'] if measurement is not None else None)
        if measurement is not None:
            measurement['stage'] = 'post'
        _, post_total = run_phase(loops, lambda: postprocess(outs, ratio, pad, sw, sh, conf_th=conf),
                                 measurement['post_ms_samples'] if measurement is not None else None)
        def end_to_end():
            lb2, r2, p2 = letterbox(image, w, h)
            rgb2 = cv2.cvtColor(lb2, cv2.COLOR_BGR2RGB)
            heads = rknn.inference(inputs=[rgb2[np.newaxis]], data_format='nhwc')
            return postprocess(heads, r2, p2, sw, sh, conf_th=conf)
        if measurement is not None:
            measurement['stage'] = 'e2e'
        _, e2e_total = run_phase(loops, end_to_end,
                                measurement['e2e_ms_samples'] if measurement is not None else None)
        infer_ms, post_ms, e2e_ms = [value / loops * 1000. for value in (infer_total, post_total, e2e_total)]
        if e2e_ms <= 0:
            raise RuntimeError('end-to-end clock did not advance')
        if measurement is not None:
            measurement.update(stage='complete', phase_seconds={'infer': infer_total, 'post': post_total, 'e2e': e2e_total})
        return dict(model=os.path.basename(model_path), size='%dx%d' % (w, h),
                    infer_ms=round(infer_ms, 2), post_ms=round(post_ms, 2), e2e_ms=round(e2e_ms, 2),
                    fps=round(1000. / e2e_ms, 1), temp=round(get_temp(), 1), loops=loops)
    finally:
        if shutdown_state is not None:
            shutdown_state['cleaning'] = True
        if measurement is not None:
            measurement['release_attempted'] = True
        try:
            rknn.release()
            if measurement is not None:
                measurement['release_completed'] = True
        finally:
            if shutdown_state is not None:
                shutdown_state['cleaning'] = False


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def save_json(path, value, exclusive=False):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x' if exclusive else 'w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=1, allow_nan=False)
        stream.write('\n')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--model-dir', default='/userdata/models', help='directory containing .rknn files')
    ap.add_argument('--model', help='single model file (overrides --model-dir)')
    ap.add_argument('--image', default='/userdata/models/1.jpg', help='test image')
    ap.add_argument('--loops', type=int, default=30)
    ap.add_argument('--warmup', type=int, default=5)
    ap.add_argument('--conf', type=float, default=.25)
    ap.add_argument('--json', help='legacy successful result list')
    ap.add_argument('--manifest', help='new exclusive measurement/evidence JSON path')
    args = ap.parse_args(argv)
    try:
        validate_parameters(args.loops, args.warmup, args.conf)
    except ValueError as error:
        ap.error(str(error))
    if args.json and args.manifest and Path(args.json).resolve() == Path(args.manifest).resolve():
        ap.error('--json and --manifest must use different paths')
    if any(value and Path(value).resolve() == Path(GOVERNOR_PATH).resolve()
           for value in (args.json, args.manifest)):
        ap.error('benchmark output cannot overwrite the NPU governor')
    image = cv2.imread(args.image)
    if image is None:
        print('Cannot read image: ' + args.image, file=sys.stderr)
        return 1
    models = [args.model] if args.model else sorted(glob.glob(os.path.join(args.model_dir, '*.rknn')))
    if not models:
        print('No .rknn files found', file=sys.stderr)
        return 1
    evidence = {'schema_version': 1, 'status': 'failed', 'created_utc': datetime.now(timezone.utc).isoformat(),
                'config': {'loops': args.loops, 'warmup': args.warmup, 'conf': args.conf, 'nms_iou': .45,
                           'input_layout': 'NHWC RGB uint8 batch=1',
                           'timing': 'perf_counter wall time; separate inference/final-head postprocess/end-to-end phases'},
                'runtime': {'python': platform.python_version(), 'numpy': np.__version__, 'opencv': cv2.__version__},
                'governor': {'path': str(GOVERNOR_PATH), 'original': None, 'requested': 'performance',
                             'performance_readback': None, 'restored_readback': None},
                'measurements': [], 'errors': [],
                'limits': 'Signals unwind Python cleanup; SIGKILL/power loss/uninterruptible native SDK are not covered. '
                          'Optional loop timing adds instrumentation; temp=-1 means unavailable. No historical paired-speedup claim.'}
    governor = evidence['governor']
    results, code = [], 0
    try:
        with shutdown_signals() as shutdown:
            try:
                if args.manifest:
                    evidence['input'] = {'path': args.image, 'sha256': sha256_file(args.image),
                                         'dimensions': [image.shape[1], image.shape[0]]}
                    evidence['code_sha256'] = sha256_file(__file__)
                    evidence['postprocess_sha256'] = sha256_file(Path(__file__).with_name('yolo_post.py'))
                governor['original'] = Path(GOVERNOR_PATH).read_text().strip()
                if not governor['original']:
                    raise RuntimeError('empty original NPU governor')
                try:
                    Path(GOVERNOR_PATH).write_text('performance')
                finally:
                    governor['performance_readback'] = Path(GOVERNOR_PATH).read_text().strip()
                if governor['performance_readback'] != 'performance':
                    raise RuntimeError('NPU performance governor read-back mismatch')
                print('[NPU governor: %s -> %s]' % (governor['original'], governor['performance_readback']))
                print('Image: %s (%dx%d)  Loops: %d  Warmup: %d' %
                      (args.image, image.shape[1], image.shape[0], args.loops, args.warmup))
                print('%-30s %8s %8s %8s %8s %6s' % ('Model', 'Infer', 'Post', 'E2E', 'FPS', 'Temp'))
                for model in models:
                    measurement = {'model': os.path.basename(model), 'status': 'failed'}
                    evidence['measurements'].append(measurement)
                    try:
                        if args.manifest:
                            measurement['model_sha256'] = sha256_file(model)
                        row = bench_one(model, image, args.loops, args.warmup, args.conf,
                                        measurement=measurement if args.manifest else None, shutdown_state=shutdown)
                        if shutdown['signal'] is not None:
                            raise BenchmarkInterrupted(shutdown['signal'])
                        if row is None:
                            raise RuntimeError('RKNN load/init failed')
                        results.append(row)
                        measurement.update(status='complete', result=row)
                        print('%-30s %7.1fms %7.1fms %7.1fms %7.1f %5.1fC' %
                              (row['model'], row['infer_ms'], row['post_ms'], row['e2e_ms'], row['fps'], row['temp']))
                    except Exception as error:
                        code = 1
                        measurement['error'] = '%s: %s' % (type(error).__name__, error)
                        evidence['errors'].append({'model': os.path.basename(model), 'error': measurement['error']})
                        print('Benchmark FAILED: %s: %s' % (model, error), file=sys.stderr)
            except (BenchmarkInterrupted, KeyboardInterrupt) as error:
                number = error.number if isinstance(error, BenchmarkInterrupted) else signal.SIGINT
                code = 128 + number
                evidence['signal'] = number
                evidence['errors'].append({'stage': 'interrupted', 'error': str(error) or 'KeyboardInterrupt'})
                print('Benchmark interrupted; restoring NPU governor', file=sys.stderr)
            except Exception as error:
                code = 1
                evidence['errors'].append({'stage': 'benchmark', 'error': '%s: %s' % (type(error).__name__, error)})
                print('Benchmark error: ' + str(error), file=sys.stderr)
            finally:
                shutdown['cleaning'] = True
                if governor['original'] is not None:
                    try:
                        try:
                            Path(GOVERNOR_PATH).write_text(governor['original'])
                        finally:
                            governor['restored_readback'] = Path(GOVERNOR_PATH).read_text().strip()
                        if governor['restored_readback'] != governor['original']:
                            raise RuntimeError('restored NPU governor read-back mismatch')
                        print('[NPU governor restored: %s]' % governor['restored_readback'])
                    except Exception as error:
                        code = code or 1
                        evidence['errors'].append({'stage': 'governor_restore', 'error': str(error)})
                        print('NPU governor restoration FAILED: ' + str(error), file=sys.stderr)
                if shutdown['signal'] is not None:
                    code = 128 + shutdown['signal']
                    evidence['signal'] = shutdown['signal']
    except Exception as error:
        code = code or 1
        evidence['errors'].append({'stage': 'signal_handlers', 'error': str(error)})
        print('Benchmark setup/cleanup error: ' + str(error), file=sys.stderr)
    evidence['status'] = 'complete' if code == 0 else 'interrupted' if 'signal' in evidence else 'failed'
    evidence['counts'] = {'requested': len(models), 'completed': len(results)}
    try:
        if args.json:
            save_json(args.json, results)
            print('Saved: ' + args.json)
        if args.manifest:
            save_json(args.manifest, evidence, exclusive=True)
            print('Saved measurement manifest: ' + args.manifest)
    except (OSError, ValueError) as error:
        print('Benchmark output error: ' + str(error), file=sys.stderr)
        code = code or 1
    if results:
        best = min(results, key=lambda row: row['e2e_ms'])
        print('Best measured here: %s @ %.1f FPS (E2E %.1fms)' % (best['model'], best['fps'], best['e2e_ms']))
        print('Historical baseline reference: 141ms / 7.1 FPS; not remeasured in this comparison')
    return code


if __name__ == '__main__':
    sys.exit(main())
