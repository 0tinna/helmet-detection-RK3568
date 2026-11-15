#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YOLOv5 helmet detection on RK3568 NPU (RKNNLite).
Model output layout (per head): (1, 3, gh, gw, 7) where 7 = 4 box + 1 obj + 2 cls
"""
import os
import sys
import time
import numpy as np
import cv2
from rknnlite.api import RKNNLite

MODEL = '/userdata/models/helmet-640-640.rknn'
DEFAULT_IMG = '/userdata/models/1.jpg'

# class order taken from the reference test.py: CLASSES = ("person", "hat")
CLASSES = ['person', 'hat']
ANCHORS = [[10, 13, 16, 30, 33, 23],
           [30, 61, 62, 45, 59, 119],
           [116, 90, 156, 198, 373, 326]]
STRIDES = [8, 16, 32]
CONF_TH = 0.25
NMS_TH = 0.45
IN_SIZE = 640


def letterbox(im, new_size=IN_SIZE, color=(114, 114, 114)):
    h, w = im.shape[:2]
    r = min(new_size / w, new_size / h)
    nw, nh = int(round(w * r)), int(round(h * r))
    if (nw, nh) != (w, h):
        im = cv2.resize(im, (nw, nh), interpolation=cv2.INTER_LINEAR)
    top = (new_size - nh) // 2
    bottom = new_size - nh - top
    left = (new_size - nw) // 2
    right = new_size - nw - left
    im = cv2.copyMakeBorder(im, top, bottom, left, right,
                            cv2.BORDER_CONSTANT, value=color)
    return im, r, (left, top)


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


_GRID_CACHE = {}


def _grid(gh, gw, stride):
    key = (gh, gw, stride)
    g = _GRID_CACHE.get(key)
    if g is None:
        gy, gx = np.meshgrid(np.arange(gh, dtype=np.float32),
                             np.arange(gw, dtype=np.float32), indexing='ij')
        g = (gx * stride, gy * stride)
        _GRID_CACHE[key] = g
    return g


def decode(outs):
    """Fully vectorised YOLOv5 head decoding. Returns boxes in xywh (input scale)."""
    boxes, scores, classes = [], [], []
    for out, stride, anchor in zip(outs, STRIDES, ANCHORS):
        o = out[0].astype(np.float32)     # (3, gh, gw, 7)
        na, gh, gw, _ = o.shape
        p = sigmoid(o)                    # one sigmoid for the whole tensor
        gx, gy = _grid(gh, gw, stride)
        anc = np.array(anchor, dtype=np.float32).reshape(na, 1, 1, 2)

        cx = (p[..., 0] * 2.0 - 0.5) * stride + gx
        cy = (p[..., 1] * 2.0 - 0.5) * stride + gy
        bw = (p[..., 2] * 2.0) ** 2 * anc[..., 0]
        bh = (p[..., 3] * 2.0) ** 2 * anc[..., 1]

        conf = p[..., 4] * p[..., 5:].max(axis=-1)
        cls_id = p[..., 5:].argmax(axis=-1)

        mask = conf > CONF_TH
        if not mask.any():
            continue

        # xywh, matching what cv2.dnn.NMSBoxes expects
        boxes.append(np.stack([cx[mask] - bw[mask] / 2.0,
                               cy[mask] - bh[mask] / 2.0,
                               bw[mask], bh[mask]], axis=-1))
        scores.append(conf[mask])
        classes.append(cls_id[mask])

    if not boxes:
        return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32), \
            np.zeros((0,), dtype=np.int32)
    return (np.concatenate(boxes), np.concatenate(scores),
            np.concatenate(classes).astype(np.int32))


def nms(boxes, scores, classes):
    """Per-class NMS. `boxes` is already xywh."""
    keep_all = []
    for c in np.unique(classes):
        idx = np.where(classes == c)[0]
        b = boxes[idx]
        idxs = cv2.dnn.NMSBoxes(b.tolist(), scores[idx].tolist(), CONF_TH, NMS_TH)
        if len(idxs) > 0:
            keep_all.extend(idx[np.array(idxs).reshape(-1)].tolist())
    if not keep_all:
        return (np.zeros((0, 4), np.float32), np.zeros((0,), np.float32),
                np.zeros((0,), dtype=np.int32))
    keep = np.array(sorted(keep_all), dtype=int)
    return boxes[keep], scores[keep], classes[keep]


def draw(im, boxes, scores, classes):
    """boxes are xywh."""
    colors = [(0, 200, 0), (0, 0, 255), (255, 128, 0), (255, 0, 255)]
    counts = {}
    for (bx, by, bw, bh), s, c in zip(boxes, scores, classes):
        name = CLASSES[c] if c < len(CLASSES) else str(c)
        counts[name] = counts.get(name, 0) + 1
        col = colors[c % len(colors)]
        p1 = (int(bx), int(by))
        p2 = (int(bx + bw), int(by + bh))
        cv2.rectangle(im, p1, p2, col, 2)
        label = '%s %.2f' % (name, s)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(im, (p1[0], p1[1] - th - 6), (p1[0] + tw, p1[1]), col, -1)
        cv2.putText(im, label, (p1[0], p1[1] - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    return counts


def main():
    img_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IMG
    out_path = sys.argv[2] if len(sys.argv) > 2 else '/userdata/models/detect_result.jpg'
    loops = int(sys.argv[3]) if len(sys.argv) > 3 else 1

    if not os.path.exists(img_path):
        print('image not found:', img_path)
        sys.exit(1)

    print('=== init RKNNLite ===')
    rknn = RKNNLite(verbose=False)
    if rknn.load_rknn(MODEL) != 0:
        print('load_rknn failed')
        sys.exit(2)
    if rknn.init_runtime() != 0:
        print('init_runtime failed')
        sys.exit(3)

    raw = cv2.imread(img_path)
    if raw is None:
        print('imread failed:', img_path)
        sys.exit(4)
    print('input image: %s  size=%dx%d' % (img_path, raw.shape[1], raw.shape[0]))

    lb, ratio, (dw, dh) = letterbox(raw)
    inp = np.expand_dims(cv2.cvtColor(lb, cv2.COLOR_BGR2RGB), 0).astype(np.uint8)

    t0 = time.time()
    for _ in range(loops):
        outs = rknn.inference(inputs=[inp], data_format='nhwc')
    infer_ms = (time.time() - t0) / loops * 1000.0
    print('inference: %.2f ms  (%.1f FPS, pure NPU)' % (infer_ms, 1000.0 / infer_ms))

    t1 = time.time()
    for _ in range(10):
        b0, s0, c0 = decode(outs)
        b0, s0, c0 = nms(b0, s0, c0)
    post_ms = (time.time() - t1) / 10.0 * 1000.0
    boxes, scores, classes = b0, s0, c0
    print('postprocess: %.2f ms (avg of 10)' % post_ms)

    # end-to-end: letterbox + inference + postprocess
    t2 = time.time()
    for _ in range(10):
        lb2, _, _ = letterbox(raw)
        inp2 = np.expand_dims(cv2.cvtColor(lb2, cv2.COLOR_BGR2RGB), 0).astype(np.uint8)
        o2 = rknn.inference(inputs=[inp2], data_format='nhwc')
        nms(*decode(o2))
    e2e_ms = (time.time() - t2) / 10.0 * 1000.0
    print('end-to-end: %.2f ms  (%.1f FPS)' % (e2e_ms, 1000.0 / e2e_ms))

    # un-letterbox: only the top-left corner carries the padding offset
    if len(boxes):
        boxes[:, 0] = (boxes[:, 0] - dw) / ratio
        boxes[:, 1] = (boxes[:, 1] - dh) / ratio
        boxes[:, 2] = boxes[:, 2] / ratio
        boxes[:, 3] = boxes[:, 3] / ratio
        boxes[:, 0] = boxes[:, 0].clip(0, raw.shape[1] - 1)
        boxes[:, 1] = boxes[:, 1].clip(0, raw.shape[0] - 1)
        boxes[:, 2] = boxes[:, 2].clip(1, raw.shape[1] - boxes[:, 0])
        boxes[:, 3] = boxes[:, 3].clip(1, raw.shape[0] - boxes[:, 1])

    print('detected: %d boxes' % len(boxes))
    counts = draw(raw, boxes, scores, classes)
    print('summary:', counts)

    cv2.imwrite(out_path, raw)
    print('saved:', out_path)

    rknn.release()


if __name__ == '__main__':
    main()
