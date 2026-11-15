#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ONNX -> RKNN conversion for RK3568 (rknn-toolkit2 2.3.2, must match board librknnrt).

Follows airockchip/rknn_model_zoo (examples/yolov5/python/convert.py) and adds:
  * head cut: the model is truncated at the three detection Conv outputs, so the
    Reshape/Transpose tail (CPU fallback, not quantisation friendly) is removed
    and decoded in yolo_post.py instead;
  * arbitrary input size (--size W H) without re-exporting the ONNX, e.g.
    640x640 / 512x512 / 416x416 / 640x384 (16:9 camera, no wasted padding);
  * calibration images are letterboxed exactly like the runtime does.

Usage:
  python convert.py helmet.onnx --size 640 640 --calib-dir ../data/valid -o helmet_640.rknn
"""
import argparse
import glob
import os
import random
import sys
import tempfile

import cv2

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from yolo_post import letterbox  # noqa: E402


def find_head_convs(onnx_path):
    """Names of the tensors produced by the last Conv feeding each graph output."""
    import onnx
    g = onnx.load(onnx_path).graph
    prod = {o: n for n in g.node for o in n.output}
    names = []
    for out in g.output:
        n = prod[out.name]
        while n.op_type != 'Conv':
            n = prod[n.input[0]]
        names.append(n.output[0])
    return g.input[0].name, names


def make_calib_list(img_dir, w, h, n=100, seed=0):
    files = sorted(glob.glob(os.path.join(img_dir, '*.jpg')) + glob.glob(os.path.join(img_dir, '*.png')))
    random.Random(seed).shuffle(files)
    files = files[:n]
    out_dir = tempfile.mkdtemp(prefix='calib_%dx%d_' % (w, h))
    lst = os.path.join(out_dir, 'dataset.txt')
    with open(lst, 'w') as f:
        for i, p in enumerate(files):
            im = cv2.imread(p)
            if im is None:
                continue
            lb, _, _ = letterbox(im, w, h)
            q = os.path.join(out_dir, '%04d.jpg' % i)
            cv2.imwrite(q, lb, [cv2.IMWRITE_JPEG_QUALITY, 95])
            f.write(q + '\n')
    return lst


def build_rknn(onnx_path, w, h, calib, quant=True, algo='normal', cut_head=True,
               platform='rk3568', verbose=False):
    from rknn.api import RKNN
    in_name, heads = find_head_convs(onnx_path)
    rknn = RKNN(verbose=verbose)
    rknn.config(mean_values=[[0, 0, 0]], std_values=[[255, 255, 255]],
                target_platform=platform, quantized_algorithm=algo,
                quantized_method='channel', optimization_level=3)
    kw = dict(model=onnx_path, inputs=[in_name], input_size_list=[[1, 3, h, w]])
    if cut_head:
        kw['outputs'] = heads
    assert rknn.load_onnx(**kw) == 0, 'load_onnx failed'
    assert rknn.build(do_quantization=quant, dataset=calib if quant else None) == 0, 'build failed'
    return rknn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('onnx')
    ap.add_argument('--size', type=int, nargs=2, default=[640, 640], metavar=('W', 'H'))
    ap.add_argument('--calib-dir', help='folder of images used for int8 calibration')
    ap.add_argument('--calib-n', type=int, default=100)
    ap.add_argument('--algo', default='normal', choices=['normal', 'kl_divergence', 'mmse'])
    ap.add_argument('--fp', action='store_true', help='no quantisation (fp16)')
    ap.add_argument('--keep-head', action='store_true', help='keep Reshape/Transpose tail')
    ap.add_argument('--platform', default='rk3568')
    ap.add_argument('-o', '--out', required=True)
    a = ap.parse_args()

    w, h = a.size
    assert w % 32 == 0 and h % 32 == 0, 'size must be a multiple of 32'
    calib = None
    if not a.fp:
        assert a.calib_dir, '--calib-dir is required for int8'
        calib = make_calib_list(a.calib_dir, w, h, a.calib_n)
    rknn = build_rknn(a.onnx, w, h, calib, quant=not a.fp, algo=a.algo,
                      cut_head=not a.keep_head, platform=a.platform)
    assert rknn.export_rknn(a.out) == 0, 'export failed'
    rknn.release()
    print('saved %s  (%.1f MB)' % (a.out, os.path.getsize(a.out) / 1e6))


if __name__ == '__main__':
    main()
