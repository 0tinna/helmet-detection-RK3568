#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YOLOv5 anchor-based pre/post-processing shared by the PC tools (convert / eval)
and the board runtime. Pure NumPy + OpenCV, no torch.

Two head layouts are accepted, so old and new models run through one code path:
  * "conv"   (1, na*(5+nc), gh, gw)  raw Conv output, rknn_model_zoo style.
             The Reshape/Transpose tail is cut off at conversion time because it
             is not quantisation friendly and is executed on the CPU.
  * "legacy" (1, na, gh, gw, 5+nc)   original helmet-640-640.rknn layout.

Speed trick (the main post-processing optimisation):
  conf = sigmoid(obj) * sigmoid(cls) <= sigmoid(obj), therefore a cell can only
  pass the confidence threshold if  obj_logit > logit(conf_th).  We test the raw
  logit first and run sigmoid / box decoding only on the few surviving cells
  instead of on all 25200 * 7 values.
"""
import math

import cv2
import numpy as np

# The open-source helmet model was trained on SHWD: "person" is a bare head
# (= NOT wearing a helmet), "hat" is a head with a helmet.
CLASSES = ('person', 'hat')
CLS_NO_HELMET = 0
CLS_HELMET = 1

ANCHORS = np.array([[10, 13, 16, 30, 33, 23],
                    [30, 61, 62, 45, 59, 119],
                    [116, 90, 156, 198, 373, 326]], dtype=np.float32).reshape(3, 3, 2)
STRIDES = (8, 16, 32)


def letterbox(im, new_w, new_h, color=(114, 114, 114)):
    """Aspect-preserving resize + padding. Returns (image, ratio, (pad_x, pad_y))."""
    h, w = im.shape[:2]
    r = min(new_w / w, new_h / h)
    nw, nh = int(round(w * r)), int(round(h * r))
    if (nw, nh) != (w, h):
        im = cv2.resize(im, (nw, nh), interpolation=cv2.INTER_LINEAR)
    left = (new_w - nw) // 2
    top = (new_h - nh) // 2
    if nw != new_w or nh != new_h:
        im = cv2.copyMakeBorder(im, top, new_h - nh - top, left, new_w - nw - left,
                                cv2.BORDER_CONSTANT, value=color)
    return im, r, (left, top)


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _as_na_h_w_no(out, na=3):
    """Return a (na, gh, gw, no) view for either supported head layout."""
    o = out[0] if out.ndim in (4, 5) else out
    if o.ndim == 4:                       # legacy: (na, gh, gw, no)
        return o
    c, gh, gw = o.shape                   # conv: (na*no, gh, gw)
    return o.reshape(na, c // na, gh, gw).transpose(0, 2, 3, 1)


def decode(outs, conf_th=0.25, anchors=ANCHORS, strides=STRIDES):
    """
    Decode raw head outputs.
    Returns boxes (N,4) xyxy in network-input pixels, scores (N,), class ids (N,).
    """
    obj_logit_th = math.log(conf_th / (1.0 - conf_th)) if 0.0 < conf_th < 1.0 else -1e9
    all_boxes, all_scores, all_cls = [], [], []
    # sort heads by grid size so stride/anchor always match (80 -> 40 -> 20)
    outs = sorted(outs, key=lambda o: -o.size)
    for out, stride, anc in zip(outs, strides, anchors):
        o = _as_na_h_w_no(out, anc.shape[0])
        a_idx, gy, gx = np.nonzero(o[..., 4] > obj_logit_th)
        if a_idx.size == 0:
            continue
        p = _sigmoid(o[a_idx, gy, gx].astype(np.float32))     # (K, no) only
        cls_id = p[:, 5:].argmax(axis=1)
        score = p[:, 4] * p[np.arange(p.shape[0]), 5 + cls_id]
        keep = score > conf_th
        if not keep.any():
            continue
        p, cls_id, score = p[keep], cls_id[keep], score[keep]
        a_idx, gy, gx = a_idx[keep], gy[keep], gx[keep]

        cx = (p[:, 0] * 2.0 - 0.5 + gx) * stride
        cy = (p[:, 1] * 2.0 - 0.5 + gy) * stride
        wh = (p[:, 2:4] * 2.0) ** 2 * anc[a_idx]
        all_boxes.append(np.stack([cx - wh[:, 0] / 2, cy - wh[:, 1] / 2,
                                   cx + wh[:, 0] / 2, cy + wh[:, 1] / 2], axis=1))
        all_scores.append(score)
        all_cls.append(cls_id)

    if not all_boxes:
        return (np.zeros((0, 4), np.float32), np.zeros((0,), np.float32),
                np.zeros((0,), np.int32))
    return (np.concatenate(all_boxes).astype(np.float32),
            np.concatenate(all_scores).astype(np.float32),
            np.concatenate(all_cls).astype(np.int32))


def nms(boxes, scores, classes, iou_th=0.45, max_det=300, agnostic=False):
    """Greedy NMS in NumPy. Per-class by default (class-offset trick)."""
    if boxes.shape[0] == 0:
        return np.zeros((0,), dtype=np.int64)
    b = boxes if agnostic else boxes + (classes.astype(np.float32) * 4096.0)[:, None]
    x1, y1, x2, y2 = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0 and len(keep) < max_det:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        iw = np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest])
        ih = np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest])
        inter = np.clip(iw, 0, None) * np.clip(ih, 0, None)
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_th]
    return np.array(keep, dtype=np.int64)


def postprocess(outs, ratio, pad, src_w, src_h, conf_th=0.25, iou_th=0.45, max_det=300):
    """Full post-processing. Returns xyxy boxes in SOURCE image pixels."""
    boxes, scores, classes = decode(outs, conf_th)
    keep = nms(boxes, scores, classes, iou_th, max_det)
    boxes, scores, classes = boxes[keep], scores[keep], classes[keep]
    if boxes.shape[0]:
        boxes[:, [0, 2]] = np.clip((boxes[:, [0, 2]] - pad[0]) / ratio, 0, src_w - 1)
        boxes[:, [1, 3]] = np.clip((boxes[:, [1, 3]] - pad[1]) / ratio, 0, src_h - 1)
    return boxes, scores, classes
