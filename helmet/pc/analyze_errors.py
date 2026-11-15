#!/usr/bin/env python3
"""Reproducible TP/FP/FN images, area metrics, and six-model source comparisons."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

if __package__:
    from .dataset_tools import CLASSES, finite_number, integer_id, image_path, sha256_file, write_json
    from .eval_map import (evaluate_areas, load_dataset, load_detections, match_class, prepare, read_json,
                           prediction_coverage, metric_definition, valid_sha256, validate_inference_config)
else:
    from dataset_tools import CLASSES, finite_number, integer_id, image_path, sha256_file, write_json
    from eval_map import (evaluate_areas, load_dataset, load_detections, match_class, prepare, read_json,
                          prediction_coverage, metric_definition, valid_sha256, validate_inference_config)

COLORS = {'tp': (0, 200, 0), 'fp': (0, 220, 255), 'fn': (0, 0, 255)}


def match_errors(dets, gt, confidence=.25, iou=.5):
    if not 0 < finite_number(confidence, 'confidence') <= 1 or not 0 < finite_number(iou, 'iou') <= 1:
        raise ValueError('confidence/IoU must be within (0,1]')
    dets, gt = prepare(dets, gt)
    filtered = {key: [d for d in rows if d[1] >= confidence] for key, rows in dets.items()}
    result = {key: {'tp': [], 'fp': [], 'fn': []} for key in gt}
    used = {key: set() for key in gt}
    for cls in range(len(CLASSES)):
        _, matches = match_class(filtered, gt, cls, iou)
        for match in matches:
            key = match['image_id']
            result[key][match['outcome']].append(filtered[key][match['detection_index']])
            if match['outcome'] == 'tp':
                used[key].add(match['gt_index'])
    for key, rows in gt.items():
        result[key]['fn'] = [row for index, row in enumerate(rows) if index not in used[key]]
    return result


def select_images(images, count=20, seed='helmet-errors-v1'):
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError('sample count must be a nonnegative integer')
    return sorted(images, key=lambda im: (hashlib.sha256(
        (str(seed) + '\0' + im['file_name']).encode('utf-8')).hexdigest(), im['file_name']))[:count]


def save_area_artifacts(areas, output, coverage=None):
    """Save literal result values; absent-class metrics remain null/blank."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / 'area_metrics.json', areas)
    with open(output / 'area_metrics.csv', 'w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['area', 'class', 'npos', 'ap50', 'precision', 'recall', 'f1', 'tp', 'fp', 'fn'])
        for area, values in areas.items():
            for name in CLASSES:
                r = values[name]
                writer.writerow([area, name, r['npos'], r['ap50'], r['precision'], r['recall'], r['f1'],
                                 r['true_positives'], r['false_positives'], r['false_negatives']])
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    labels = list(areas)
    for cls, name in enumerate(CLASSES):
        positions = [i + (cls - .5) * .35 for i in range(len(labels))]
        axes[0].bar(positions, [areas[k][name]['npos'] for k in labels], width=.35, label=name)
        axes[1].bar(positions, [areas[k][name]['ap50'] or 0. for k in labels], width=.35, label=name)
        for i, k in enumerate(labels):
            if areas[k][name]['ap50'] is None:
                axes[1].text(positions[i], .02, 'N/A', ha='center', fontsize=8)
    for ax in axes:
        ax.set_xticks(range(len(labels)), labels)
        ax.legend()
    axes[0].set_ylabel('Ground-truth count')
    axes[1].set_ylabel('All-point AP50 (%s)' % (coverage['status'] if coverage else 'coverage unknown'))
    axes[1].set_ylim(0, 1.05)
    fig.suptitle('Source-pixel bbox area: small <32²; medium <96²; large ≥96²')
    fig.tight_layout()
    fig.savefig(output / 'area_metrics.png', dpi=160)
    plt.close(fig)


def create_error_artifacts(root, images, dets, gt, output, confidence=.25, iou=.5,
                           count=20, seed='helmet-errors-v1', confidence_floor=None):
    import cv2
    coverage = prediction_coverage(confidence_floor, confidence)
    errors = match_errors(dets, gt, confidence, iou)
    chosen = select_images(images, count, seed)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    selections = []
    for im in chosen:
        path = image_path(root, im['file_name'])
        raw = cv2.imread(str(path))
        if raw is None:
            raise ValueError('cannot read error visualization image: %s' % path)
        if 'width' in im and raw.shape[:2] != (im['height'], im['width']):
            raise ValueError('visualization dimensions differ from annotation')
        for outcome in ('tp', 'fp', 'fn'):
            for row in errors[im['id']][outcome]:
                cls = row[0]
                box = row[1:] if outcome == 'fn' else row[2:]
                x1, y1, x2, y2 = [int(round(v)) for v in box]
                # Half-open box endpoint is shown at its last covered pixel.
                cv2.rectangle(raw, (x1, y1), (max(x1, x2 - 1), max(y1, y2 - 1)), COLORS[outcome], 2)
                text = '%s %s' % (outcome.upper(), CLASSES[cls])
                if outcome != 'fn':
                    text += ' %.3f' % row[1]
                cv2.putText(raw, text, (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, .45,
                            COLORS[outcome], 1, cv2.LINE_AA)
        header = 'conf>=%.3f IoU>=%.2f TP green / FP yellow / FN red' % (confidence, iou)
        cv2.putText(raw, header, (5, 16), cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1, cv2.LINE_AA)
        filename = hashlib.sha256(im['file_name'].encode()).hexdigest()[:12] + '_' + Path(im['file_name']).stem + '.png'
        if not cv2.imwrite(str(output / filename), raw):
            raise OSError('failed to write visualization')
        selections.append({'file_name': im['file_name'], 'image_sha256': sha256_file(path), 'artifact': filename})
    counts = {outcome: sum(len(values[outcome]) for values in errors.values()) for outcome in ('tp', 'fp', 'fn')}
    areas = evaluate_areas(dets, gt, confidence, iou)
    for values in areas.values():
        values['ap_coverage'] = coverage
        values['metric_definition'] = metric_definition(coverage)
    save_area_artifacts(areas, output, coverage)
    summary = {'schema_version': 1, 'confidence': confidence, 'iou': iou, 'seed': seed,
               'selection_method': 'SHA256(seed NUL filename), lowest hashes; selected before inspecting errors',
               'requested_samples': count, 'selected': selections, 'counts': counts,
               'images': len(images), 'errors': {str(k): v for k, v in errors.items()}, 'areas': areas,
               'ap_coverage': coverage, 'metric_definition': metric_definition(coverage)}
    summary['artifacts'] = {p.name: sha256_file(p) for p in sorted(output.iterdir())
                            if p.is_file() and p.name != 'error_summary.json'}
    write_json(output / 'error_summary.json', summary)
    return summary


def validate_comparison_metric(metric):
    required = ('model', 'dataset', 'config', 'classes_with_gt', 'metric_definition', 'inference', 'ap_coverage')
    if any(key not in metric for key in required):
        raise ValueError('actual comparison requires model/dataset/config/classes/metric-definition/inference/AP-coverage evidence')
    model, dataset, config, inference = (metric[key] for key in ('model', 'dataset', 'config', 'inference'))
    if (not isinstance(model, dict) or not valid_sha256(model.get('sha256'))
            or not isinstance(model.get('name'), str) or not model['name']):
        raise ValueError('comparison requires valid model name and SHA256')
    if not isinstance(dataset, dict) or not valid_sha256(dataset.get('annotation_sha256')):
        raise ValueError('comparison requires dataset annotation SHA256')
    if 'manifest_sha256' in dataset and not valid_sha256(dataset['manifest_sha256']):
        raise ValueError('invalid comparison dataset manifest SHA256')
    images = integer_id(metric.get('images'), 'metric image count')
    if images <= 0 or integer_id(dataset.get('total_images'), 'dataset total_images') != images:
        raise ValueError('comparison requires the complete declared dataset image inventory')
    if not isinstance(config, dict) or config.get('iou') != .5 or config.get('limit') != 0:
        raise ValueError('comparison requires AP50 IoU=.5 and complete dataset limit=0')
    coverage = prediction_coverage(config.get('conf_min'), config.get('op_conf'))
    declared_coverage = metric['ap_coverage']
    if not isinstance(declared_coverage, dict) or any(declared_coverage.get(k) != coverage[k]
                                                    for k in ('status', 'confidence_floor', 'project_confidence_floor',
                                                              'comparable_project_ap', 'full_score_range')):
        raise ValueError('AP confidence coverage contradicts evaluation configuration')
    if coverage['status'] == 'unknown':
        raise ValueError('actual comparison cannot verify unknown AP confidence coverage')
    definition = metric['metric_definition']
    if (not isinstance(definition, dict) or definition.get('metric_version') != 'helmet-all-point-ap50-v1'
            or definition.get('iou') != .5 or definition.get('interpolation') != 'all-point'
            or definition.get('coco_ap50_95') is not None
            or any(not isinstance(definition.get(k), str) or not definition[k]
                   for k in ('ap50', 'gt_matching', 'class_mean', 'area_rules'))):
        raise ValueError('missing/unsupported actual AP matching/area/mean definition')
    if (not isinstance(inference, dict) or inference.get('status') != 'complete'
            or inference.get('dataset_identity_verified') is not True
            or inference.get('image_hashes_verified') is not True):
        raise ValueError('actual comparison requires verified complete input/dataset identity')
    descriptor = validate_inference_config(inference.get('dump_config', inference.get('config')))
    if (not valid_sha256(descriptor.get('postprocess_sha256')) or 'clipped_box_policy' not in descriptor
            or descriptor['conf_min'] != config.get('conf_min') or descriptor['nms_iou'] != config.get('nms_iou')):
        raise ValueError('inference decoder identity/config differs from declared evaluation')
    if inference.get('backend') == 'dets' and not valid_sha256(inference.get('detections_sha256')):
        raise ValueError('comparison requires detection artifact SHA256')
    classes = metric['classes_with_gt']
    if not isinstance(classes, list) or not classes or classes != [c for c in CLASSES if c in classes]:
        raise ValueError('invalid/duplicate class coverage')
    class_counts, ap_values = {}, []
    for name in CLASSES:
        row = metric.get(name)
        if not isinstance(row, dict):
            raise ValueError('comparison requires both class records')
        npos = integer_id(row.get('npos'), 'class GT count')
        if npos < 0 or (npos > 0) != (name in classes):
            raise ValueError('class coverage contradicts GT counts')
        class_counts[name] = npos
        for key in ('ap50', 'precision', 'recall', 'f1'):
            value = row.get(key)
            if npos == 0 and key in ('ap50', 'recall', 'f1'):
                if value is not None:
                    raise ValueError('absent-class metric must be null')
            elif value is None or not 0 <= finite_number(value, 'class ' + key) <= 1:
                raise ValueError('class metric outside [0,1]')
        if npos:
            ap_values.append(row['ap50'])
    if abs(sum(ap_values) / len(ap_values) - metric['mAP50']) > 1e-12:
        raise ValueError('mAP contradicts declared present-class AP mean')
    return {'model_sha256': model['sha256'], 'model_name': model['name'], 'descriptor': descriptor,
            'eval_config': {k: config[k] for k in ('op_conf', 'iou', 'conf_min', 'nms_iou', 'limit')},
            'dataset_identity': (dataset['annotation_sha256'], dataset.get('manifest_sha256'), images),
            'class_counts': class_counts, 'definition': definition, 'coverage': coverage}


def compare_models(reference, external, independence='unknown', target=.85, independence_evidence=None,
                   allow_unverified_legacy=True):
    if len(reference) != 6 or len(external) != 6 or set(reference) != set(external):
        raise ValueError('comparison requires the same six distinct models in each source')
    if independence not in ('unknown', 'verified', 'overlap'):
        raise ValueError('independence must be unknown/verified/overlap')
    if independence == 'verified' and not independence_evidence:
        raise ValueError('verified independence requires documented evidence')
    if not 0 <= finite_number(target, 'target') <= 1:
        raise ValueError('accuracy target outside [0,1]')
    all_rows = list(reference.values()) + list(external.values())
    evidence_keys = ('model', 'dataset', 'config', 'classes_with_gt', 'metric_definition', 'inference', 'ap_coverage')
    legacy = all(not any(key in metric for key in evidence_keys) for metric in all_rows)
    if legacy and not allow_unverified_legacy:
        raise ValueError('legacy comparison requires explicit --allow-unverified-legacy; no accuracy-target pass')
    evidence = {}
    for source_name, source in (('reference', reference), ('external', external)):
        verified = {}
        for metric in source.values():
            if metric.get('images', 0) <= 0 or metric.get('mAP50') is None:
                raise ValueError('source metrics must contain images and defined mAP50')
            if not 0 <= finite_number(metric['mAP50'], 'mAP50') <= 1:
                raise ValueError('mAP50 outside [0,1]')
        if not legacy:
            verified = {name: validate_comparison_metric(metric) for name, metric in source.items()}
            if len({row['model_sha256'] for row in verified.values()}) != 6:
                raise ValueError('six distinct model artifact SHA256 identities required')
            rows = list(verified.values())
            for key in ('eval_config', 'dataset_identity', 'class_counts', 'definition'):
                if any(row[key] != rows[0][key] for row in rows[1:]):
                    raise ValueError('within-source dataset/threshold/class/metric evidence differs: ' + key)
            decoder = {k: v for k, v in rows[0]['descriptor'].items() if k != 'size'}
            if any({k: v for k, v in row['descriptor'].items() if k != 'size'} != decoder for row in rows[1:]):
                raise ValueError('within-source decoder configuration differs beyond model input size')
        evidence[source_name] = verified
    models = {}
    for name in sorted(reference):
        a, b = reference[name], external[name]
        if not legacy:
            ra, rb = evidence['reference'][name], evidence['external'][name]
            for key in ('model_sha256', 'model_name', 'descriptor', 'eval_config', 'definition'):
                if ra[key] != rb[key]:
                    raise ValueError('paired cross-source model/config/definition mismatch for %s: %s' % (name, key))
        if a.get('classes_with_gt') != b.get('classes_with_gt'):
            raise ValueError('cross-source mAP compares different sets of classes')
        models[name] = {'reference': a, 'external': b, 'mAP50_delta': b['mAP50'] - a['mAP50']}
        classes = {}
        for cls in CLASSES:
            if cls in a and cls in b:
                classes[cls] = {'reference_npos': a[cls]['npos'], 'external_npos': b[cls]['npos']}
                for metric in ('ap50', 'precision', 'recall', 'f1'):
                    av, bv = a[cls].get(metric), b[cls].get(metric)
                    classes[cls][metric + '_delta'] = bv - av if av is not None and bv is not None else None
        models[name]['classes'] = classes
    target_reasons = []
    if legacy:
        target_reasons.append('legacy metrics lack verified model/dataset/config/class/AP-coverage evidence')
    elif any(not row['coverage']['comparable_project_ap'] for rows in evidence.values() for row in rows.values()):
        target_reasons.append('confidence coverage differs from project .001 AP policy')
    if independence != 'verified':
        target_reasons.append('independence is not verified')
    if not legacy:
        if any(metric['classes_with_gt'] != list(CLASSES) for metric in external.values()):
            target_reasons.append('both model classes require GT for the project accuracy target')
        if any(metric['dataset'].get('provenance', {}).get('independence') != 'verified'
               or not metric['dataset'].get('provenance', {}).get('independence_evidence') for metric in external.values()):
            target_reasons.append('source provenance does not verify independence with evidence')
    return {'schema_version': 1, 'models': models, 'independence': independence,
            'independence_evidence': independence_evidence, 'accuracy_target': target,
            'verification_status': 'unverified-legacy' if legacy else 'verified-pairs',
            'accuracy_target_ineligible_reasons': target_reasons,
            'accuracy_target_pass': all(v['mAP50'] >= target for v in external.values()) if not target_reasons else None,
            'conclusion': 'diagnostic cross-source comparison; independence unresolved' if independence == 'unknown'
                          else 'known overlap: do not claim independent held-out accuracy' if independence == 'overlap'
                          else 'independence declared with supplied evidence; AP definition remains all-point AP50'}


def metric_inputs(values):
    output = {}
    for item in values:
        if '=' not in item:
            raise ValueError('expected model=metrics.json')
        name, path = item.split('=', 1)
        if not name or name in output:
            raise ValueError('duplicate/empty comparison model name')
        output[name] = read_json(path)
        output[name]['metrics_file_sha256'] = sha256_file(path)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    errors = subs.add_parser('errors')
    errors.add_argument('--data', required=True)
    errors.add_argument('--dets', required=True)
    errors.add_argument('--out', required=True)
    errors.add_argument('--confidence', type=float, default=.25)
    errors.add_argument('--iou', type=float, default=.5)
    errors.add_argument('--count', type=int, default=20)
    errors.add_argument('--seed', default='helmet-errors-v1')
    comparison = subs.add_parser('compare')
    comparison.add_argument('--reference', action='append', required=True, help='model=metrics.json; repeat six times')
    comparison.add_argument('--external', action='append', required=True, help='model=metrics.json; repeat six times')
    comparison.add_argument('--independence', choices=['unknown', 'verified', 'overlap'], default='unknown')
    comparison.add_argument('--independence-evidence')
    comparison.add_argument('--target', type=float, default=.85)
    comparison.add_argument('--allow-unverified-legacy', action='store_true',
                            help='explicit legacy diagnostic deltas only; prohibits accuracy-target pass')
    comparison.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'compare':
            result = compare_models(metric_inputs(args.reference), metric_inputs(args.external),
                                    args.independence, args.target, args.independence_evidence, args.allow_unverified_legacy)
            write_json(args.out, result)
        else:
            if args.iou != .5:
                raise ValueError('area plots labelled AP50 require --iou .5')
            images, gt, metadata = load_dataset(args.data, require_dimensions=True)
            dump = read_json(args.dets)
            dets = load_detections(dump, images, args.data, metadata)
            floor = dump['config']['conf_min'] if 'schema_version' in dump else None
            prediction_coverage(floor, args.confidence)
            summary = create_error_artifacts(args.data, images, dets, gt, args.out,
                                             args.confidence, args.iou, args.count, args.seed, floor)
            summary['dataset'] = metadata
            summary['detections_sha256'] = sha256_file(args.dets)
            write_json(Path(args.out) / 'error_summary.json', summary)
    except (OSError, ValueError, ImportError, KeyError) as error:
        parser.exit(1, 'analysis error: %s\n' % error)


if __name__ == '__main__':
    main()
