#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dump raw prediction values to find out why decoded boxes are wrong."""
import numpy as np
import cv2

from rknnlite.api import RKNNLite

MODEL = '/userdata/models/helmet-640-640.rknn'
IMG = '/userdata/models/1.jpg'
ANCHORS = [[10, 13, 16, 30, 33, 23],
           [30, 61, 62, 45, 59, 119],
           [116, 90, 156, 198, 373, 326]]
STRIDES = [8, 16, 32]


def sig(x):
    return 1.0 / (1.0 + np.exp(-x))


rknn = RKNNLite(verbose=False)
rknn.load_rknn(MODEL)
rknn.init_runtime()

raw = cv2.imread(IMG)
h, w = raw.shape[:2]
r = min(640.0 / w, 640.0 / h)
nw, nh = int(round(w * r)), int(round(h * r))
im = cv2.resize(raw, (nw, nh))
top = (640 - nh) // 2
left = (640 - nw) // 2
lb = cv2.copyMakeBorder(im, top, 640 - nh - top, left, 640 - nw - left,
                        cv2.BORDER_CONSTANT, value=(128, 128, 128))
inp = np.expand_dims(cv2.cvtColor(lb, cv2.COLOR_BGR2RGB), 0).astype(np.uint8)
print('image %dx%d  ratio=%.4f  pad(left,top)=(%d,%d)' % (w, h, r, left, top))

outs = rknn.inference(inputs=[inp], data_format='nhwc')
print('output shapes:', [o.shape for o in outs])

for si, (out, stride, anchor) in enumerate(zip(outs, STRIDES, ANCHORS)):
    o = out[0].astype(np.float32)          # (3, gh, gw, 7)
    na, gh, gw, _ = o.shape
    p = sig(o)
    gy, gx = np.meshgrid(np.arange(gh), np.arange(gw), indexing='ij')
    anc = np.array(anchor, dtype=np.float32).reshape(na, 1, 1, 2)
    cx = (p[..., 0] * 2 - 0.5 + gx) * stride
    cy = (p[..., 1] * 2 - 0.5 + gy) * stride
    bw = (p[..., 2] * 2) ** 2 * anc[..., 0]
    bh = (p[..., 3] * 2) ** 2 * anc[..., 1]
    conf = p[..., 4] * p[..., 5:].max(axis=-1)
    ci = p[..., 5:].argmax(axis=-1)

    print('\n--- scale %d (stride %d) raw range [%.2f, %.2f] ---'
          % (si, stride, o.min(), o.max()))
    idx = np.argsort(conf.ravel())[::-1][:6]
    for flat in idx:
        a, yy, xx = np.unravel_index(flat, (na, gh, gw))
        print(' a%d grid(%2d,%2d) raw=(%6.2f,%6.2f,%6.2f,%6.2f) obj=%.3f cls=%s conf=%.3f'
              ' => cx=%.1f cy=%.1f bw=%.1f bh=%.1f'
              % (a, yy, xx, o[a, yy, xx, 0], o[a, yy, xx, 1],
                 o[a, yy, xx, 2], o[a, yy, xx, 3], p[a, yy, xx, 4],
                 np.round(p[a, yy, xx, 5:], 3), conf[a, yy, xx],
                 cx[a, yy, xx], cy[a, yy, xx], bw[a, yy, xx], bh[a, yy, xx]))

rknn.release()
print('\n=== done ===')
