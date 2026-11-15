#!/usr/bin/env python3
"""Dump existing RKNN predictions for an annotated dataset; no model conversion.

Input is RGB uint8 NHWC batch-one; decode/NMS/letterbox use yolo_post.py.
AP dump confidence is .001, operating confidence .25, NMS IoU .45. Every
image has a hash and status. Partial/failed runs exit nonzero and cannot score.
"""
import argparse
from datetime import datetime, timezone
import platform
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / 'pc'))
from dataset_tools import image_path, sha256_file, write_json
from eval_map import (CLIPPED_BOX_POLICY, CLIPPED_BOX_REASON, load_dataset,
                      partition_clipped_candidates, validate_rows)


def resolve_size(model, size=None):
    name = Path(model).stem
    match = re.search(r'(?:^|_)(\d+)x(\d+)(?:$|_)', name)
    legacy = re.fullmatch(r'helmet-(\d+)-(\d+)', name)
    derived = tuple(map(int, (match or legacy).groups())) if match or legacy else None
    if size is not None:
        if len(size) != 2 or any(isinstance(v, bool) or not isinstance(v, int) for v in size):
            raise ValueError('explicit size must be two integers')
        size = tuple(size)
        if derived and size != derived:
            raise ValueError('explicit size differs from dimensions encoded in model name')
    else:
        size = derived
    if not size or any(v <= 0 or v % 32 for v in size):
        raise ValueError('model needs known positive multiple-of-32 dimensions; provide --size W H')
    return size


def validate_heads(outputs, width, height):
    """Require all three finite YOLO heads (21-channel conv or 3x7 legacy)."""
    import numpy as np
    if not isinstance(outputs, (list, tuple)) or len(outputs) != 3:
        raise ValueError('inference must return all three raw YOLO heads')
    grids = set()
    for head in outputs:
        if not isinstance(head, np.ndarray) or not np.issubdtype(head.dtype, np.number) or not np.isfinite(head).all():
            raise ValueError('inference head must be a finite numeric tensor')
        if head.ndim == 4 and head.shape[:2] == (1, 21):
            grid = head.shape[2:]
        elif head.ndim == 5 and head.shape[:2] == (1, 3) and head.shape[-1] == 7:
            grid = head.shape[2:4]
        else:
            raise ValueError('unsupported head layout; expected (1,21,H,W) or (1,3,H,W,7)')
        if grid in grids:
            raise ValueError('duplicate inference grid')
        grids.add(grid)
    if grids != {(height // s, width // s) for s in (8, 16, 32)}:
        raise ValueError('inference grids do not agree with declared model dimensions')


def make_engine():
    try:
        from rknnlite.api import RKNNLite
        return RKNNLite(verbose=False)
    except ImportError:
        from rknn.api import RKNN
        return RKNN(verbose=False)


def dump_dataset(model, root, images, size=None, conf_min=.001, op_conf=.25,
                 nms_iou=.45, engine_factory=None):
    """Inject only the external engine; preprocessing/decoder remain real."""
    import cv2
    import numpy as np
    if __package__:
        from .yolo_post import letterbox, postprocess
    else:
        from yolo_post import letterbox, postprocess
    width, height = resolve_size(model, size)
    if not images or not 0 < conf_min <= op_conf <= 1 or not 0 < nms_iou <= 1:
        raise ValueError('nonempty images and valid confidence/NMS required')
    if len({im['file_name'] for im in images}) != len(images):
        raise ValueError('duplicate image filenames')
    result = {'schema_version': 1, 'status': 'failed',
              'created_utc': datetime.now(timezone.utc).isoformat(),
              'config': {'size': [width, height], 'conf_min': conf_min, 'op_conf': op_conf,
                         'nms_iou': nms_iou, 'max_det': 300, 'class_order': ['person', 'hat'],
                         'input_layout': 'NHWC RGB uint8 batch=1', 'clipped_box_policy': CLIPPED_BOX_POLICY,
                         'postprocess_sha256': sha256_file(Path(__file__).resolve().parent / 'yolo_post.py')},
              'model': {'name': Path(model).name, 'sha256': sha256_file(model)},
              'runtime': {'python': platform.python_version(), 'numpy': np.__version__, 'opencv': cv2.__version__},
              'inputs': [], 'detections': {}, 'errors': []}
    engine = None
    try:
        engine = (engine_factory or make_engine)()
        if engine.load_rknn(str(model)) != 0:
            raise RuntimeError('load_rknn failed')
        if engine.init_runtime() != 0:
            raise RuntimeError('init_runtime failed')
        for im in images:
            item = {'file_name': im['file_name'], 'id': im['id'], 'status': 'error'}
            result['inputs'].append(item)
            try:
                path = image_path(root, im['file_name'])
                item['sha256'] = sha256_file(path)
                raw = cv2.imread(str(path))
                if raw is None or raw.shape[:2] != (im['height'], im['width']):
                    raise ValueError('unreadable image or dimensions differ from annotation')
                item['dimensions'] = [im['width'], im['height']]
                lb, ratio, pad = letterbox(raw, width, height)
                rgb = cv2.cvtColor(lb, cv2.COLOR_BGR2RGB)
                start = time.perf_counter()
                outputs = engine.inference(inputs=[rgb[None]], data_format='nhwc')
                item['inference_ms'] = (time.perf_counter() - start) * 1000
                validate_heads(outputs, width, height)
                boxes, scores, classes = postprocess(outputs, ratio, pad, im['width'], im['height'],
                                                    conf_th=conf_min, iou_th=nms_iou, max_det=300)
                rows = [(int(c), float(s), *map(float, b)) for b, s, c in zip(boxes, scores, classes)]
                kept, discarded = partition_clipped_candidates(rows, (im['width'], im['height']))
                result['detections'][im['file_name']] = validate_rows(kept, (im['width'], im['height']))
                item['discarded_clipped_boxes'] = discarded
                item['discarded_clipped_count'] = len(discarded)
                item['discarded_clipped_reason'] = CLIPPED_BOX_REASON
                item['status'] = 'ok'
                item['detections'] = len(kept)
            except Exception as error:
                item['error'] = '%s: %s' % (type(error).__name__, error)
                result['errors'].append({'file_name': im['file_name'], 'error': item['error']})
        if not result['errors'] and len(result['detections']) == len(images):
            result['status'] = 'complete'
    except Exception as error:
        result['errors'].append({'stage': 'engine', 'error': '%s: %s' % (type(error).__name__, error)})
    finally:
        if engine is not None:
            try:
                engine.release()
            except Exception as error:
                result['status'] = 'failed'
                result['errors'].append({'stage': 'release', 'error': str(error)})
    result['counts'] = {'requested': len(images), 'completed': len(result['detections']), 'errors': len(result['errors']),
                        'discarded_clipped_count': sum(item.get('discarded_clipped_count', 0) for item in result['inputs'])}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--data', required=True)
    parser.add_argument('--annotations', default='_annotations.coco.json')
    parser.add_argument('--size', nargs=2, type=int, metavar=('W', 'H'))
    parser.add_argument('--conf-min', type=float, default=.001)
    parser.add_argument('--op-conf', type=float, default=.25)
    parser.add_argument('--nms-iou', type=float, default=.45)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        images, _, metadata = load_dataset(args.data, require_dimensions=True, annotation_file=args.annotations)
        result = dump_dataset(args.model, args.data, images, args.size, args.conf_min, args.op_conf, args.nms_iou)
        result['dataset'] = metadata
        write_json(args.out, result)
        print('%s: %d/%d images -> %s' % (result['status'], len(result['detections']), len(images), args.out))
        if result['status'] != 'complete':
            parser.exit(1, 'board dump incomplete; inspect errors in output\n')
    except (OSError, ValueError, ImportError) as error:
        parser.exit(1, 'dump error: %s\n' % error)


if __name__ == '__main__':
    main()
