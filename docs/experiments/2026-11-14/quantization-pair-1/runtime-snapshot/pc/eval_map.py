#!/usr/bin/env python3
"""Head/helmet evaluation, retaining empty images and rejecting failed inference.

AP50 uses VOC2010+ all-point interpolation at IoU=.50. It is not COCO's
101-point AP or COCO AP50:95. Classes without GT have null AP/recall/F1 and
are excluded from mAP. Operating P/R/F1 uses confidence >= .25 by default.
Boxes use zero-based half-open xyxy; source categories hardhat -> hat(1),
no-hardhat -> person(0), where person means a bare head, not a full body.
"""
import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys

if __package__:
    from .dataset_tools import (CLASSES, DS_TO_MODEL, finite_number, integer_id,
                                image_path, sha256_file, validate_coco, write_json)
else:
    from dataset_tools import (CLASSES, DS_TO_MODEL, finite_number, integer_id,
                               image_path, sha256_file, validate_coco, write_json)

AREA_RANGES = {'small': (0., 32. ** 2), 'medium': (32. ** 2, 96. ** 2),
               'large': (96. ** 2, math.inf)}
CLIPPED_BOX_POLICY = 'discard-finite-zero-area-after-shared-postprocess-with-trace'
CLIPPED_BOX_REASON = 'zero_area_after_shared_source_clipping_or_postprocess'
PROJECT_AP_CONFIDENCE_FLOOR = .001


def prediction_coverage(confidence_floor, operating_confidence):
    operating = finite_number(operating_confidence, 'operating confidence')
    if not 0 < operating <= 1:
        raise ValueError('operating confidence must be within (0,1]')
    if confidence_floor is None:
        status, floor = 'unknown', None
    else:
        floor = finite_number(confidence_floor, 'prediction confidence floor')
        if not 0 < floor <= operating:
            raise ValueError('operating/visualization confidence is below available prediction floor')
        status = ('project-floor' if floor == PROJECT_AP_CONFIDENCE_FLOOR else
                  'truncated' if floor > PROJECT_AP_CONFIDENCE_FLOOR else 'extended')
    return {'status': status, 'confidence_floor': floor,
            'project_confidence_floor': PROJECT_AP_CONFIDENCE_FLOOR,
            'comparable_project_ap': status == 'project-floor', 'full_score_range': False,
            'limitation': 'Predictions below the declared floor are unavailable; AP may change with lower confidence coverage.'
                          if floor is not None else 'Legacy prediction coverage is unknown; AP is unverified.'}


def metric_definition(coverage):
    status, floor = coverage['status'], coverage['confidence_floor']
    if status == 'unknown':
        description = 'All-point AP50 from legacy supplied predictions; confidence coverage unknown, unverified'
    elif status == 'truncated':
        description = 'Confidence-truncated all-point AP50 at score floor %.6g; not project-floor AP50' % floor
    elif status == 'extended':
        description = 'Extended-confidence all-point AP50 at score floor %.6g; differs from project-floor policy' % floor
    else:
        description = 'Project all-point AP50 at retained-score floor .001; not full-score-range AP or official COCO AP'
    return {'metric_version': 'helmet-all-point-ap50-v1', 'ap50': description,
            'iou': .5, 'interpolation': 'all-point',
            'gt_matching': 'highest-IoU available same-class GT; in-range GT before ignored other-area GT',
            'class_mean': 'mean over classes with GT; absent classes null',
            'coco_ap50_95': None, 'coco_status': 'not computed; requires official COCO evaluator',
            'area_rules': 'bbox area in source pixels: small [0,1024), medium [1024,9216), large [9216,infinity); other-area matches/unmatched predictions ignored'}


def read_json(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key: %s' % key)
            result[key] = value
        return result
    with open(path, encoding='utf-8') as stream:
        return json.load(stream, object_pairs_hook=unique_pairs)


def load_dataset(root, limit=0, require_dimensions=False, annotation_file='_annotations.coco.json'):
    if limit < 0:
        raise ValueError('limit must be nonnegative')
    path = image_path(root, annotation_file)
    images, gt, metadata = validate_coco(read_json(path), require_dimensions)
    metadata['annotation_sha256'] = sha256_file(path)
    manifest_path = Path(root) / 'manifest.json'
    if manifest_path.is_file():
        metadata['manifest_sha256'] = sha256_file(manifest_path)
        metadata['provenance'] = read_json(manifest_path).get('provenance', {})
    else:
        metadata['provenance'] = {'origin': 'unknown', 'independence': 'unknown', 'license': 'unknown'}
    metadata['total_images'] = len(images)
    if limit:
        images = images[:limit]
        gt = {im['id']: gt[im['id']] for im in images}
    if not any(gt.values()):
        raise ValueError('selected image subset has no recognized ground truth')
    return images, gt, metadata


def load_gt(root, limit=0):
    """Legacy tuple API; dimensions optional only for metadata-only evaluation."""
    images, gt, _ = load_dataset(root, limit)
    return images, gt


def validate_box(box, dimensions=None, allow_zero_area=False):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError('box requires four xyxy coordinates')
    x1, y1, x2, y2 = [finite_number(v, 'box coordinate') for v in box]
    if (x1 < 0 or y1 < 0 or x2 < x1 or y2 < y1 or not math.isfinite((x2 - x1) * (y2 - y1))
            or (not allow_zero_area and (x2 == x1 or y2 == y1))):
        raise ValueError('box must have positive area and nonnegative origin')
    if dimensions and (x2 > dimensions[0] + 1e-6 or y2 > dimensions[1] + 1e-6):
        raise ValueError('box lies outside image')
    return (x1, y1, x2, y2)


def validate_rows(rows, dimensions=None, ground_truth=False, allow_zero_area=False):
    if not isinstance(rows, (list, tuple)):
        raise ValueError('detections/ground truth must be an array')
    output = []
    for row in rows:
        expected = 5 if ground_truth else 6
        if not isinstance(row, (list, tuple)) or len(row) != expected:
            raise ValueError('invalid %s row; expected %d columns' % ('GT' if ground_truth else 'detection', expected))
        cls = integer_id(row[0], 'model class')
        if cls not in (0, 1):
            raise ValueError('model class must be 0 (bare head) or 1 (helmet head)')
        if ground_truth:
            output.append((cls, *validate_box(row[1:], dimensions)))
        else:
            score = finite_number(row[1], 'confidence')
            if not 0 <= score <= 1:
                raise ValueError('confidence must lie within [0,1]')
            output.append((cls, score, *validate_box(row[2:], dimensions, allow_zero_area)))
    return output


def partition_clipped_candidates(rows, dimensions):
    """Postprocess-only cleanup; normal detection loading keeps strict boxes."""
    candidates = validate_rows(rows, dimensions, allow_zero_area=True)
    kept, discarded = [], []
    for row in candidates:
        (discarded if box_area(row[2:]) == 0 else kept).append(row)
    return kept, discarded


def valid_sha256(digest):
    return isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest) is not None


def validate_inference_config(config):
    if not isinstance(config, dict) or config.get('class_order') != list(CLASSES):
        raise ValueError('invalid inference class-order configuration')
    size = config.get('size')
    if not isinstance(size, list) or len(size) != 2:
        raise ValueError('inference configuration requires [width,height]')
    if any(integer_id(v, 'input dimension') <= 0 or v % 32 for v in size):
        raise ValueError('input dimensions must be positive multiples of 32')
    floor = finite_number(config.get('conf_min'), 'inference conf_min')
    operating = finite_number(config.get('op_conf'), 'inference op_conf')
    nms = finite_number(config.get('nms_iou'), 'inference nms_iou')
    if not 0 < floor <= operating <= 1 or not 0 < nms <= 1:
        raise ValueError('inconsistent inference confidence/NMS configuration')
    if integer_id(config.get('max_det'), 'max_det') <= 0:
        raise ValueError('max_det must be positive')
    if config.get('input_layout') not in ('NHWC RGB uint8 batch=1', 'NCHW RGB float32 /255 batch=1'):
        raise ValueError('unsupported/missing input layout configuration')
    if 'clipped_box_policy' in config and config['clipped_box_policy'] != CLIPPED_BOX_POLICY:
        raise ValueError('unsupported clipped-box policy')
    if 'postprocess_sha256' in config and not valid_sha256(config['postprocess_sha256']):
        raise ValueError('invalid shared postprocess SHA256')
    return config


def load_detections(value, images, root=None, dataset=None):
    """Legacy filename dict: absent images are misses. Structured dumps must complete."""
    if isinstance(value, (str, Path)):
        value = read_json(value)
    if not isinstance(value, dict):
        raise ValueError('detection file must be an object keyed by image filename')
    by_name = {im['file_name']: im for im in images}
    structured = 'schema_version' in value or 'detections' in value
    if structured:
        if (value.get('schema_version') != 1 or value.get('status') != 'complete'
                or not isinstance(value.get('errors'), list) or value['errors']):
            raise ValueError('failed/unsupported board dump; do not score incomplete inference')
        mapping = value.get('detections')
        if not isinstance(mapping, dict) or set(mapping) != set(by_name):
            raise ValueError('structured dump must cover every dataset image exactly')
        if any(not isinstance(rows, list) for rows in mapping.values()):
            raise ValueError('structured per-image detections must be arrays')
        if (not isinstance(value.get('model'), dict) or not valid_sha256(value['model'].get('sha256'))
                or not isinstance(value['model'].get('name'), str) or not value['model']['name']):
            raise ValueError('structured dump lacks valid model SHA256')
        config = validate_inference_config(value.get('config'))
        runtime = value.get('runtime')
        if not isinstance(runtime, dict) or any(not isinstance(runtime.get(k), str) or not runtime[k]
                                                for k in ('python', 'numpy', 'opencv')):
            raise ValueError('structured dump lacks runtime versions')
        try:
            created = datetime.fromisoformat(value.get('created_utc', ''))
            if created.tzinfo is None:
                raise ValueError('timestamp must contain timezone')
        except (ValueError, TypeError) as error:
            raise ValueError('invalid structured dump timestamp') from error
        counts = value.get('counts')
        if not isinstance(counts, dict) or any(integer_id(counts.get(k), 'aggregate ' + k) != expected
                                              for k, expected in (('requested', len(images)),
                                                                  ('completed', len(images)), ('errors', 0))):
            raise ValueError('aggregate completion counts contradict complete dump')
        declared = value.get('dataset')
        if dataset is None and root is not None:
            _, _, dataset = load_dataset(root, require_dimensions=True)
        if dataset is not None:
            if not isinstance(declared, dict):
                raise ValueError('structured dump lacks dataset annotation identity')
            for key in ('annotation_sha256', 'manifest_sha256'):
                if key in declared or key in dataset:
                    if not valid_sha256(declared.get(key)) or declared.get(key) != dataset.get(key):
                        raise ValueError('dump dataset identity differs: ' + key)
        elif declared is not None:
            if not isinstance(declared, dict) or not valid_sha256(declared.get('annotation_sha256')):
                raise ValueError('invalid declared dataset annotation identity')
        inputs = value.get('inputs')
        if not isinstance(inputs, list) or len(inputs) != len(images):
            raise ValueError('structured dump lacks complete per-input status')
        seen = set()
        discarded_total = 0
        for row in inputs:
            if not isinstance(row, dict):
                raise ValueError('invalid input status record')
            name = row.get('file_name')
            if (name not in by_name or name in seen or row.get('status') != 'ok'
                    or row.get('error') or not valid_sha256(row.get('sha256'))):
                raise ValueError('invalid/duplicate/failed input status')
            im = by_name[name]
            if 'width' not in im or 'height' not in im:
                raise ValueError('structured dump requires dataset dimensions')
            if integer_id(row.get('id'), 'input id') != im['id']:
                raise ValueError('input id differs from dataset')
            dimensions = row.get('dimensions')
            if (not isinstance(dimensions, list) or len(dimensions) != 2
                    or any(integer_id(v, 'input dimension') <= 0 for v in dimensions)
                    or dimensions != [im['width'], im['height']]):
                raise ValueError('input dimensions differ from dataset')
            if (integer_id(row.get('detections'), 'input detections') != len(mapping[name])
                    or len(mapping[name]) > config['max_det']):
                raise ValueError('per-input detection count contradicts rows/max_det')
            if finite_number(row.get('inference_ms'), 'inference_ms') < 0:
                raise ValueError('inference time cannot be negative')
            if 'clipped_box_policy' in config:
                kept, discarded = partition_clipped_candidates(row.get('discarded_clipped_boxes'),
                                                                 (im['width'], im['height']))
                if (kept or integer_id(row.get('discarded_clipped_count'), 'discarded_clipped_count') != len(discarded)
                        or row.get('discarded_clipped_reason') != CLIPPED_BOX_REASON
                        or any(candidate[1] < config['conf_min'] for candidate in discarded)):
                    raise ValueError('discarded clipped-candidate trace contradicts configuration')
                discarded_total += len(discarded)
            if root is not None and row['sha256'] != sha256_file(image_path(root, name)):
                raise ValueError('dump image hash differs from dataset: %s' % name)
            seen.add(name)
        if ('clipped_box_policy' in config
                and integer_id(counts.get('discarded_clipped_count'), 'aggregate discarded_clipped_count') != discarded_total):
            raise ValueError('aggregate discarded candidate count contradicts input traces')
    else:
        mapping = value
    if any(name not in by_name for name in mapping):
        raise ValueError('detections reference an unknown image')
    result = {}
    for name, im in by_name.items():
        dims = (im['width'], im['height']) if 'width' in im else None
        result[im['id']] = validate_rows(mapping.get(name, []), dims)
        if structured and any(row[1] < config['conf_min'] for row in result[im['id']]):
            raise ValueError('detection score below declared confidence floor')
    return result


def box_area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def box_iou(a, b):
    inter = max(0., min(a[2], b[2]) - max(a[0], b[0])) * max(0., min(a[3], b[3]) - max(a[1], b[1]))
    union = box_area(a) + box_area(b) - inter
    return inter / union if union > 0 else 0.


def iou_1n(box, boxes):
    """Compatibility helper; NumPy is loaded only when callers request arrays."""
    import numpy as np
    return np.asarray([box_iou(box, b) for b in boxes], dtype=np.float64)


def prepare(dets, gt):
    if not isinstance(dets, dict) or not isinstance(gt, dict) or not gt:
        raise ValueError('nonempty image -> GT map and detection map required')
    if any(key not in gt for key in dets):
        raise ValueError('detections reference unknown image ID')
    return ({key: validate_rows(dets.get(key, [])) for key in gt},
            {key: validate_rows(rows, ground_truth=True) for key, rows in gt.items()})


def match_class(dets, gt, cls, iou_th=.5, area_range=None):
    """COCO-style area ignore matching, with no crowd support.

    All GT remain matchable. In-range available GT take priority; matching an
    out-of-range GT ignores the detection. Unmatched predictions whose own box
    area is out of range are ignored. Deleting other-area GT would create FP.
    """
    low, high = area_range or (0., math.inf)
    gts = {key: [(i, g[1:], not low <= box_area(g[1:]) < high)
                 for i, g in enumerate(rows) if g[0] == cls] for key, rows in gt.items()}
    npos = sum(not ignored for rows in gts.values() for _, _, ignored in rows)
    used = {key: set() for key in gt}
    rows = [(d[1], key, index, d) for key, values in dets.items() for index, d in enumerate(values) if d[0] == cls]
    rows.sort(key=lambda r: (-r[0], r[1], r[2]))
    matches = []
    for score, key, index, detection in rows:
        candidates = [(ignored, -box_iou(detection[2:], box), i)
                      for i, box, ignored in gts[key]
                      if i not in used[key] and box_iou(detection[2:], box) >= iou_th]
        if candidates:
            ignored, _, target = min(candidates)
            used[key].add(target)
            outcome = 'ignore' if ignored else 'tp'
        else:
            target = None
            outcome = 'fp' if low <= box_area(detection[2:]) < high else 'ignore'
        matches.append({'image_id': key, 'detection_index': index, 'score': score,
                        'outcome': outcome, 'gt_index': target})
    return npos, matches


def all_point_ap(matches, npos):
    if not npos:
        return None
    tp, fp, recall, precision = 0, 0, [0.], [0.]
    for row in matches:
        if row['outcome'] == 'ignore':
            continue
        tp += row['outcome'] == 'tp'
        fp += row['outcome'] == 'fp'
        recall.append(tp / npos)
        precision.append(tp / (tp + fp))
    recall.append(1.)
    precision.append(0.)
    for i in range(len(precision) - 2, -1, -1):
        precision[i] = max(precision[i], precision[i + 1])
    return sum((recall[i] - recall[i - 1]) * precision[i]
               for i in range(1, len(recall)) if recall[i] != recall[i - 1])


def evaluate(dets, gt, op_conf=.25, iou_th=.5, area_range=None):
    for value, name in ((op_conf, 'op_conf'), (iou_th, 'iou_th')):
        if not 0 < finite_number(value, name) <= 1:
            raise ValueError('%s must be within (0,1]' % name)
    dets, gt = prepare(dets, gt)
    result = {}
    for cls, name in enumerate(CLASSES):
        npos, matches = match_class(dets, gt, cls, iou_th, area_range)
        operating = [m for m in matches if m['score'] >= op_conf]
        tp = sum(m['outcome'] == 'tp' for m in operating)
        fp = sum(m['outcome'] == 'fp' for m in operating)
        precision = tp / (tp + fp) if tp + fp else 0.
        recall = tp / npos if npos else None
        f1 = (2 * precision * recall / (precision + recall) if precision + recall else 0.) if npos else None
        result[name] = {'ap50': all_point_ap(matches, npos), 'npos': npos,
                        'precision': precision, 'recall': recall, 'f1': f1,
                        'true_positives': tp, 'false_positives': fp, 'false_negatives': npos - tp,
                        'ignored_predictions': sum(m['outcome'] == 'ignore' for m in matches),
                        'predictions': len(matches), 'defined': bool(npos)}
    present = [result[name]['ap50'] for name in CLASSES if result[name]['ap50'] is not None]
    result['mAP50'] = sum(present) / len(present) if present else None
    result['classes_with_gt'] = [n for n in CLASSES if result[n]['npos']]
    return result


def evaluate_areas(dets, gt, op_conf=.25, iou_th=.5):
    return {name: evaluate(dets, gt, op_conf, iou_th, bounds) for name, bounds in AREA_RANGES.items()}


def run_backend(args, imgs, root):
    """Optional PC inference libraries are imported only for the chosen backend."""
    import cv2
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from yolo_post import letterbox, postprocess
    engine = None
    try:
        if args.onnx:
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            session = ort.InferenceSession(args.onnx, options, providers=['CPUExecutionProvider'])
            inputs = session.get_inputs()
            if len(inputs) != 1 or len(inputs[0].shape) != 4:
                raise ValueError('expected one NCHW ONNX input')
            shape = inputs[0].shape
            if shape[0] not in (1, 'batch', None) or shape[1] != 3:
                raise ValueError('ONNX input must be batch-one NCHW RGB')
            height, width = shape[2:]
            if not all(isinstance(v, int) and v > 0 for v in (width, height)):
                if not args.size:
                    raise ValueError('dynamic ONNX dimensions need explicit --size W H')
                width, height = args.size
            elif args.size and list(args.size) != [width, height]:
                raise ValueError('--size differs from ONNX dimensions')

            def infer(rgb):
                tensor = (rgb.astype(np.float32) / 255.).transpose(2, 0, 1)[None]
                return session.run(None, {inputs[0].name: tensor})
        else:
            if __package__:
                from .convert import build_rknn
            else:
                from convert import build_rknn
            width, height = args.size or [640, 640]
            engine = build_rknn(args.rknn_sim, width, height, args.calib, quant=True, algo=args.algo)
            if engine.init_runtime() != 0:
                raise RuntimeError('RKNN simulator init failed')

            def infer(rgb):
                return engine.inference(inputs=[rgb[None]], data_format='nhwc')
        output = {}
        from dump_dets import validate_heads
        for im in imgs:
            raw = cv2.imread(str(image_path(root, im['file_name'])))
            if raw is None or raw.shape[:2] != (im['height'], im['width']):
                raise ValueError('cannot read image or dimensions differ: %s' % im['file_name'])
            lb, ratio, pad = letterbox(raw, width, height)
            heads = infer(cv2.cvtColor(lb, cv2.COLOR_BGR2RGB))
            validate_heads(heads, width, height)
            boxes, scores, classes = postprocess(heads, ratio, pad, im['width'], im['height'],
                                                conf_th=args.conf_min, iou_th=args.nms_iou)
            output[im['id']] = validate_rows([(int(c), float(s), *map(float, b))
                                               for b, s, c in zip(boxes, scores, classes)],
                                              (im['width'], im['height']))
        return output
    finally:
        if engine is not None:
            engine.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument('--onnx')
    backend.add_argument('--rknn-sim')
    backend.add_argument('--dets')
    parser.add_argument('--size', type=int, nargs=2, metavar=('W', 'H'))
    parser.add_argument('--calib')
    parser.add_argument('--algo', default='normal')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--conf-min', type=float, default=.001)
    parser.add_argument('--op-conf', type=float, default=.25)
    parser.add_argument('--nms-iou', type=float, default=.45)
    parser.add_argument('--iou', type=float, default=.5)
    parser.add_argument('--annotations', default='_annotations.coco.json')
    parser.add_argument('--save-dets')
    parser.add_argument('--out')
    args = parser.parse_args()
    if args.calib and not args.rknn_sim:
        parser.error('--calib is only valid with --rknn-sim')
    if args.rknn_sim and not args.calib:
        parser.error('--rknn-sim requires --calib; avoid uncalibrated quantization')
    if args.iou != .5:
        parser.error('--iou must be .5 for metrics labelled AP50')
    if args.size and any(v <= 0 or v % 32 for v in args.size):
        parser.error('--size dimensions must be positive multiples of 32')
    if not 0 < args.conf_min <= args.op_conf <= 1 or not 0 < args.nms_iou <= 1:
        parser.error('require 0 < conf-min <= op-conf <= 1 and 0 < nms-iou <= 1')
    try:
        all_images, all_gt, metadata = load_dataset(args.data, require_dimensions=not args.dets,
                                                   annotation_file=args.annotations)
        if args.limit < 0:
            raise ValueError('limit must be nonnegative')
        images = all_images[:args.limit] if args.limit else all_images
        gt = {im['id']: all_gt[im['id']] for im in images}
        if not any(gt.values()):
            raise ValueError('selected subset has no recognized GT')
        dump = read_json(args.dets) if args.dets else None
        if args.dets:
            full_dets = load_detections(dump, all_images, args.data, metadata)
            dets = {im['id']: full_dets[im['id']] for im in images}
        else:
            dets = run_backend(args, images, args.data)
        if args.save_dets:
            write_json(args.save_dets, {im['file_name']: dets[im['id']] for im in images})
        result = evaluate(dets, gt, args.op_conf, args.iou)
        conf_floor = dump['config']['conf_min'] if args.dets and 'schema_version' in dump else None if args.dets else args.conf_min
        nms_iou = dump['config']['nms_iou'] if args.dets and 'schema_version' in dump else None if args.dets else args.nms_iou
        coverage = prediction_coverage(conf_floor, args.op_conf)
        areas = evaluate_areas(dets, gt, args.op_conf)
        for values in areas.values():
            values['ap_coverage'] = coverage
            values['metric_definition'] = metric_definition(coverage)
        result.update({'images': len(images), 'dataset': metadata, 'areas': areas, 'ap_coverage': coverage,
                       'config': {'op_conf': args.op_conf, 'iou': args.iou, 'conf_min': conf_floor,
                                  'nms_iou': nms_iou, 'limit': args.limit},
                       'metric_definition': metric_definition(coverage),
                       'inference': {'backend': 'dets' if args.dets else 'onnx' if args.onnx else 'rknn-sim',
                                     'status': 'complete', 'legacy_missing_predictions': bool(args.dets and 'schema_version' not in dump),
                                     'dataset_identity_verified': bool(args.dets and 'schema_version' in dump),
                                     'image_hashes_verified': bool(args.dets and 'schema_version' in dump)},
                       'accuracy_target_pass': None})
        if args.dets:
            result['inference']['detections_sha256'] = sha256_file(args.dets)
            if 'schema_version' in dump:
                result['inference']['dump_config'] = dump.get('config', {})
                result['model'] = dump.get('model', {})
        else:
            path = args.onnx or args.rknn_sim
            result['model'] = {'name': Path(path).name, 'sha256': sha256_file(path)}
        for name in CLASSES:
            r = result[name]
            print('%s AP50=%s P=%.4f R=%s F1=%s gt=%d' %
                  (name, r['ap50'], r['precision'], r['recall'], r['f1'], r['npos']))
        print('mAP50=%s (%d images; coverage=%s, score floor=%s)' %
              (result['mAP50'], result['images'], coverage['status'], coverage['confidence_floor']))
        if args.out:
            write_json(args.out, result)
    except (ValueError, OSError, ImportError, RuntimeError) as error:
        parser.exit(1, 'evaluation error: %s\n' % error)


if __name__ == '__main__':
    main()
