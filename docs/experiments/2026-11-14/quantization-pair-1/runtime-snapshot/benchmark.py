#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
benchmark.py — Board-side benchmark for all RKNN models.

Runs each .rknn in a directory through N inference loops, measures:
  * Pure NPU inference time (ms)
  * Post-processing time (ms)
  * End-to-end time (preprocess + infer + post) (ms)
  * Equivalent FPS
  * SOC temperature

Outputs a human-readable table and an optional JSON for further analysis.

Usage:
  python3 benchmark.py                                     # defaults
  python3 benchmark.py --model-dir /userdata/models --loops 50 --image /userdata/models/1.jpg
"""
import argparse
import glob
import json
import os
import sys
import time

import cv2
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from yolo_post import letterbox, postprocess, decode, nms

try:
    from rknnlite.api import RKNNLite as RKNN_CLS
except ImportError:
    from rknn.api import RKNN as RKNN_CLS


def get_temp():
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return -1


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


def bench_one(model_path, image, loops=30, warmup=5, conf=0.25):
    w, h = parse_size(model_path)
    rknn = RKNN_CLS(verbose=False)
    if rknn.load_rknn(model_path) != 0:
        return None
    if rknn.init_runtime() != 0:
        rknn.release()
        return None

    lb, ratio, pad = letterbox(image, w, h)
    rgb = cv2.cvtColor(lb, cv2.COLOR_BGR2RGB)
    inp = rgb[np.newaxis].astype(np.uint8)

    # warmup
    for _ in range(warmup):
        rknn.inference(inputs=[inp], data_format='nhwc')

    # pure inference
    t0 = time.monotonic()
    for _ in range(loops):
        outs = rknn.inference(inputs=[inp], data_format='nhwc')
    infer_total = time.monotonic() - t0

    # post-processing
    sh, sw = image.shape[:2]
    t1 = time.monotonic()
    for _ in range(loops):
        postprocess(outs, ratio, pad, sw, sh, conf_th=conf)
    post_total = time.monotonic() - t1

    # end-to-end
    t2 = time.monotonic()
    for _ in range(loops):
        lb2, r2, p2 = letterbox(image, w, h)
        rgb2 = cv2.cvtColor(lb2, cv2.COLOR_BGR2RGB)
        o2 = rknn.inference(inputs=[rgb2[np.newaxis]], data_format='nhwc')
        postprocess(o2, r2, p2, sw, sh, conf_th=conf)
    e2e_total = time.monotonic() - t2

    temp = get_temp()
    rknn.release()

    infer_ms = infer_total / loops * 1000
    post_ms = post_total / loops * 1000
    e2e_ms = e2e_total / loops * 1000
    return dict(
        model=os.path.basename(model_path),
        size='%dx%d' % (w, h),
        infer_ms=round(infer_ms, 2),
        post_ms=round(post_ms, 2),
        e2e_ms=round(e2e_ms, 2),
        fps=round(1000 / e2e_ms, 1),
        temp=round(temp, 1),
        loops=loops,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--model-dir', default='/userdata/models',
                    help='directory containing .rknn files')
    ap.add_argument('--model', help='single model file (overrides --model-dir)')
    ap.add_argument('--image', default='/userdata/models/1.jpg', help='test image')
    ap.add_argument('--loops', type=int, default=30)
    ap.add_argument('--warmup', type=int, default=5)
    ap.add_argument('--conf', type=float, default=0.25)
    ap.add_argument('--json', help='save results to JSON')
    args = ap.parse_args()

    image = cv2.imread(args.image)
    if image is None:
        print('Cannot read image:', args.image)
        sys.exit(1)

    if args.model:
        models = [args.model]
    else:
        models = sorted(glob.glob(os.path.join(args.model_dir, '*.rknn')))
    if not models:
        print('No .rknn files found')
        sys.exit(1)

    print('Image: %s (%dx%d)  Loops: %d  Warmup: %d' %
          (args.image, image.shape[1], image.shape[0], args.loops, args.warmup))
    print()

    # set NPU to performance mode
    try:
        with open('/sys/class/devfreq/fde40000.npu/governor', 'w') as f:
            f.write('performance')
        print('[NPU governor -> performance]')
    except Exception:
        pass

    results = []
    hdr = '%-30s %8s %8s %8s %8s %6s' % ('Model', 'Infer', 'Post', 'E2E', 'FPS', 'Temp')
    print(hdr)
    print('-' * len(hdr))
    for mp in models:
        print('  benchmarking %s ...' % os.path.basename(mp), end='', flush=True)
        r = bench_one(mp, image, args.loops, args.warmup, args.conf)
        if r is None:
            print(' FAILED')
            continue
        print('\r', end='')
        print('%-30s %7.1fms %7.1fms %7.1fms %7.1f  %5.1fC' %
              (r['model'], r['infer_ms'], r['post_ms'], r['e2e_ms'], r['fps'], r['temp']))
        results.append(r)

    if args.json:
        json.dump(results, open(args.json, 'w'), indent=1)
        print('\nSaved:', args.json)

    # compare with baseline
    if results:
        best = min(results, key=lambda r: r['e2e_ms'])
        print('\nBest: %s @ %.1f FPS (E2E %.1fms)' % (best['model'], best['fps'], best['e2e_ms']))
        print('Baseline (old helmet-640-640.rknn): 141ms / 7.1 FPS')
        speedup = 141.0 / best['e2e_ms']
        print('Speedup: %.2fx' % speedup)


if __name__ == '__main__':
    main()
