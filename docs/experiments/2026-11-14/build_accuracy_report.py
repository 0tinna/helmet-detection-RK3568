#!/usr/bin/env python3
"""Verify frozen six-model evidence and rebuild the diagnostic accuracy report.

Offline only: no inference, threshold search, dataset rewrite or network access.
Run from any directory with the repository's .venv Python. All numeric values
come from accuracy-matrix-1; benchmark timing comes from 2026-11-13 run2.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
MATRIX = HERE / 'accuracy-matrix-1'
FIGURES = HERE / 'figures'
MODELS = ('helmet-640-640.rknn', 'helmet_416x416.rknn', 'helmet_512x288.rknn',
          'helmet_512x512.rknn', 'helmet_640x384.rknn', 'helmet_640x640.rknn')
LABELS = ('Original 640x640', '416x416', '512x288', '512x512', '640x384', '640x640')
SOURCES = ('shwd', 'hardhat')
CLASSES = ('person', 'hat')
AREAS = ('small', 'medium', 'large')
SEED = 'graduation-fixed-before-error-inspection'
sys.path.insert(0, str(ROOT))
from helmet.pc.eval_map import (evaluate, evaluate_areas, load_dataset, load_detections,
                                metric_definition, prediction_coverage)
from helmet.pc.analyze_errors import compare_models, match_errors, select_images


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def equal(actual, expected, label):
    """Compare recomputed numeric trees without rounding away disagreements."""
    if isinstance(expected, dict):
        require(isinstance(actual, dict) and set(actual) == set(expected), label + ': keys differ')
        for key, value in expected.items():
            equal(actual[key], value, label + '/' + str(key))
    elif isinstance(expected, list):
        require(isinstance(actual, (list, tuple)) and len(actual) == len(expected), label + ': length differs')
        for index, value in enumerate(expected):
            equal(actual[index], value, label + '/' + str(index))
    elif isinstance(expected, float):
        require(isinstance(actual, (int, float)) and math.isclose(actual, expected, rel_tol=0, abs_tol=1e-12),
                label + ': numeric disagreement')
    else:
        require(actual == expected, label + ': value differs')


def verify_evidence():
    manifest = read(MATRIX / 'run-manifest.json')
    require(manifest['status'] == 'complete', 'matrix run is incomplete')
    require(manifest['original_governor'] == manifest['restored_governor'], 'governor was not restored')
    require(manifest['thresholds'] == {'conf_min': .001, 'op_conf': .25, 'nms_iou': .45},
            'prespecified thresholds differ')
    require(len(manifest['runs']) == 12, 'expected twelve runs')
    require({(r['dataset'], r['model']) for r in manifest['runs']} ==
            {(source, model) for source in SOURCES for model in MODELS}, 'matrix pair inventory differs')
    benchmark_path = ROOT / 'docs/performance/2026-11-13-comparison-run2.json'
    benchmark = {r['model']: r for r in read(benchmark_path)}
    require(set(benchmark) == set(MODELS), 'benchmark model inventory differs')
    environment_path = ROOT / 'docs/performance/2026-11-13-environment-run2.txt'
    environment = environment_path.read_text(encoding='utf-8')
    benchmark_hashes = {Path(name).name: digest for digest, name in re.findall(
        r'([0-9a-f]{64})\s+(/userdata/\S+\.rknn)', environment)}
    require(benchmark_hashes == manifest['models'], 'benchmark and accuracy model hashes differ')
    evidence = {'schema_version': 1, 'status': 'verified', 'runs': [], 'datasets': {},
                'manifest_sha256': sha256(MATRIX / 'run-manifest.json'),
                'benchmark_sha256': sha256(benchmark_path),
                'benchmark_environment_sha256': sha256(environment_path),
                'builder_sha256': sha256(__file__), 'recorded_code_sha256': manifest['sources'],
                'current_local_code': {}, 'accuracy_target_pass': None,
                'numerical_mAP85_pass': False, 'independence': 'unknown'}
    # Frozen run hashes describe the board execution. Local code may since have
    # changed; record this explicitly rather than relabeling the old experiment.
    for name, digest in manifest['sources'].items():
        actual = sha256(ROOT / 'helmet' / name)
        evidence['current_local_code'][name] = {'sha256': actual, 'matches_recorded_run': actual == digest}
    snapshot_dir = MATRIX / 'runtime-snapshot'
    snapshot_record = read(snapshot_dir / 'snapshot-verification.json')
    require({row['source'] for row in snapshot_record} == set(manifest['sources']), 'snapshot inventory differs')
    for row in snapshot_record:
        actual = sha256(snapshot_dir / row['source'])
        require(actual == row['actual'] == row['expected'] == manifest['sources'][row['source']] and row['match'] is True,
                'archived execution-source hash differs: ' + row['source'])
    evidence['runtime_snapshot_verified'] = True
    evidence['runtime_snapshot_record_sha256'] = sha256(snapshot_dir / 'snapshot-verification.json')
    datasets, metrics, errors = {}, {source: {} for source in SOURCES}, {source: {} for source in SOURCES}
    for source in SOURCES:
        directory = ROOT / 'datasets/evaluation' / source
        images, gt, metadata = load_dataset(directory, require_dimensions=True)
        require(len(images) == 200, source + ': expected 200 images')
        dataset_manifest = read(directory / 'manifest.json')
        require(len(dataset_manifest['records']) == 200, source + ': incomplete manifest')
        for key in ('annotation_sha256', 'manifest_sha256'):
            require(metadata[key] == manifest['datasets'][source][key], source + ': ' + key + ' mismatch')
        image_hashes = {}
        for row in dataset_manifest['records']:
            actual = sha256(directory / row['file_name'])
            require(actual == row['image_sha256'], source + ': image hash differs: ' + row['file_name'])
            image_hashes[row['file_name']] = actual
            raw = row['raw_annotation']
            require(sha256(ROOT / raw['path']) == raw['sha256'], source + ': raw annotation hash differs')
        require(len(image_hashes) == 200, source + ': image names are not unique')
        coco = read(directory / '_annotations.coco.json')
        info = coco['info']
        require(sum(a.get('source_difficult', 0) for a in coco['annotations']) == (139 if source == 'shwd' else 0),
                source + ': difficult-object count differs')
        require(info.get('ignored_label_counts', {}) == ({} if source == 'shwd' else {'person': 35}),
                source + ': ignored raw label count differs')
        require(dataset_manifest['counts']['groups'] == 1, source + ': artificial scene split')
        if source == 'shwd':
            require(info['filename_resolution_counts'] == {'exact': 193, 'unique-stem': 6, 'case-insensitive': 1},
                    'seven recorded SHWD filename mappings differ')
        evidence['datasets'][source] = {'annotation_sha256': metadata['annotation_sha256'],
            'manifest_sha256': metadata['manifest_sha256'], 'counts': dataset_manifest['counts'],
            'image_hashes_verified': 200, 'raw_annotation_hashes_verified': 200,
            'filename_resolution_counts': info['filename_resolution_counts']}
        datasets[source] = (directory, images, gt, metadata, image_hashes)
    for row in manifest['runs']:
        source, model = row['dataset'], row['model']
        require(row['status'] == 'complete' and row['dump_exit'] == row['eval_exit'] == 0, 'failed run: ' + model)
        dump_path, metric_path = (MATRIX / row[key] for key in ('dump_file', 'metrics_file'))
        require(sha256(dump_path) == row['dump_sha256'], 'dump hash mismatch: ' + dump_path.name)
        require(sha256(metric_path) == row['metrics_sha256'], 'metric hash mismatch: ' + metric_path.name)
        dump, metric = read(dump_path), read(metric_path)
        require(dump['status'] == 'complete' and not dump['errors'], 'incomplete dump: ' + dump_path.name)
        require(dump['counts']['requested'] == dump['counts']['completed'] == 200 and dump['counts']['errors'] == 0,
                'dump coverage is not complete200')
        directory, images, gt, metadata, image_hashes = datasets[source]
        require(len(dump['inputs']) == len(dump['detections']) == 200, 'input/prediction inventory incomplete')
        require(set(dump['detections']) == set(image_hashes), 'empty-image coverage was lost')
        for item in dump['inputs']:
            require(item['status'] == 'ok' and item['sha256'] == image_hashes[item['file_name']],
                    'dump image hash/status mismatch')
        require(dump['model'] == metric['model'] == {'name': model, 'sha256': manifest['models'][model]},
                'model identity differs')
        model_path = ROOT / ('models' if model == MODELS[0] else 'helmet/models') / model
        require(sha256(model_path) == manifest['models'][model], 'local model differs: ' + model)
        require(dump['config']['postprocess_sha256'] == manifest['sources']['yolo_post.py'], 'decoder SHA differs')
        expected_size = [640, 640] if model == MODELS[0] else list(map(int, Path(model).stem.split('_')[1].split('x')))
        require(dump['config']['size'] == expected_size, 'input model dimensions differ')
        require(metric['config'] == {'op_conf': .25, 'iou': .5, 'conf_min': .001, 'nms_iou': .45, 'limit': 0},
                'evaluation thresholds/limit differ')
        require(metric['images'] == 200 and metric['inference']['detections_sha256'] == row['dump_sha256'],
                'metric is not bound to complete dump')
        dets = load_detections(dump, images, directory, metadata)
        recomputed = evaluate(dets, gt, .25, .5)
        equal(recomputed, {key: metric[key] for key in recomputed}, source + '/' + model + '/metrics')
        area_values = evaluate_areas(dets, gt, .25, .5)
        for area in area_values.values():
            coverage = prediction_coverage(.001, .25)
            area.update(ap_coverage=coverage, metric_definition=metric_definition(coverage))
        equal(area_values, metric['areas'], source + '/' + model + '/areas')
        error_dir = MATRIX / 'errors' / source / Path(model).stem
        summary = read(error_dir / 'error_summary.json')
        equal(read(error_dir / 'area_metrics.json'), metric['areas'], source + '/' + model + '/area artifact')
        equal(summary['areas'], metric['areas'], source + '/' + model + '/error areas')
        require(summary['confidence'] == .25 and summary['iou'] == .5 and summary['seed'] == SEED and
                summary['requested_samples'] == 12 and summary['images'] == 200, 'error selection config differs')
        chosen = select_images(images, 12, SEED)
        require([im['file_name'] for im in chosen] == [im['file_name'] for im in summary['selected']],
                'error samples were not selected by prespecified hash')
        for item in summary['selected']:
            require(item['image_sha256'] == image_hashes[item['file_name']], 'error image hash differs')
        for name, digest in summary['artifacts'].items():
            require(sha256(error_dir / name) == digest, 'error artifact hash differs: ' + name)
        recomputed_errors = match_errors(dets, gt, .25, .5)
        equal({str(key): values for key, values in recomputed_errors.items()}, summary['errors'],
              source + '/' + model + '/TPFPFN')
        counts = {key: sum(len(value[key]) for value in recomputed_errors.values()) for key in ('tp', 'fp', 'fn')}
        equal(counts, summary['counts'], 'error counts')
        inference_times = [item['inference_ms'] for item in dump['inputs']]
        require(all(math.isfinite(value) and value > 0 for value in inference_times), 'invalid raw NPU timing')
        timing_summary = {'mean_ms': float(np.mean(inference_times)),
                          'p50_ms': float(np.quantile(inference_times, .50, method='linear')),
                          'p95_ms': float(np.quantile(inference_times, .95, method='linear')),
                          'p99_ms': float(np.quantile(inference_times, .99, method='linear')),
                          'count': 200, 'quantile_method': 'numpy linear',
                          'scope': 'engine.inference wall time only; all200 calls including first image'}
        metrics[source][model], errors[source][model] = metric, summary
        evidence['runs'].append({'dataset': source, 'model': model, 'images': 200,
            'dump_sha256': row['dump_sha256'], 'metrics_sha256': row['metrics_sha256'],
            'metrics_recomputed_equal': True, 'areas_recomputed_equal': True, 'errors_recomputed_equal': True,
            'fixed_sample_count': 12, 'discarded_clipped_count': dump['counts']['discarded_clipped_count'],
            'empty_prediction_images': sum(not values for values in dets.values()),
            'inference_timing': timing_summary,
            'error_summary_sha256': sha256(error_dir / 'error_summary.json')})
        print('Verified ' + source + ' / ' + model + ': 200 inputs, metrics/areas/errors agree', flush=True)
    comparison = read(MATRIX / 'cross-source-comparison.json')
    recomputed_comparison = compare_models(metrics['shwd'], metrics['hardhat'], allow_unverified_legacy=False)
    equal(recomputed_comparison, comparison, 'cross-source comparison')
    overlap = read(ROOT / 'datasets/evaluation/cross-source-known-overlap.json')
    require(overlap['pair'] == ['shwd/000764.jpg', 'hardhat/hard_hat_workers1137.png'] and
            overlap['status'] == 'visually-confirmed', 'known overlap evidence differs')
    excluded = read(MATRIX / 'known-overlap-excluded-199.json')
    require(excluded['images'] == 199 and excluded['excluded'] == 'hard_hat_workers1137.png', 'exclusion differs')
    directory, images, gt, metadata, image_hashes = datasets['hardhat']
    remaining_ids = {im['id'] for im in images if im['file_name'] != excluded['excluded']}
    for model in MODELS:
        dump = read(MATRIX / ('hardhat--' + Path(model).stem + '.dump.json'))
        dets = load_detections(dump, images, directory, metadata)
        recomputed = evaluate({key: values for key, values in dets.items() if key in remaining_ids},
                              {key: values for key, values in gt.items() if key in remaining_ids}, .25, .5)
        equal(recomputed, excluded['metrics'][model], 'overlap excluded199/' + model)
    evidence['cross_source_sha256'] = sha256(MATRIX / 'cross-source-comparison.json')
    evidence['known_overlap_excluded_sha256'] = sha256(MATRIX / 'known-overlap-excluded-199.json')
    evidence['cross_source_recomputed_equal'] = evidence['excluded199_recomputed_equal'] = True
    require(all(metric['mAP50'] < .85 for source in metrics.values() for metric in source.values()),
            'report wording about numerical85 requires review')
    return manifest, benchmark, metrics, errors, excluded, evidence


def percent(value, signed=False):
    if value is None:
        return 'N/A'
    return format(100 * value, '+.2f' if signed else '.2f')


def table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |',
                      '| ' + ' | '.join(['---'] * len(headers)) + ' |'] +
                     ['| ' + ' | '.join(map(str, row)) + ' |' for row in rows])


def plot_figures(benchmark, metrics, errors):
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'axes.spines.top': False,
                         'axes.spines.right': False, 'savefig.facecolor': 'white'})
    colors = plt.get_cmap('tab10').colors[:6]
    outputs = []
    def save(fig, name):
        fig.savefig(FIGURES / name, dpi=180, bbox_inches='tight')
        plt.close(fig)
        outputs.append(name)
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    for axis, metric_name in zip(axes, ('mAP50', 'person', 'hat')):
        for index, model in enumerate(MODELS):
            timing = benchmark[model]['e2e_ms']
            for source, marker in (('shwd', 'o'), ('hardhat', '^')):
                value = metrics[source][model][metric_name]
                value = value['ap50'] if isinstance(value, dict) else value
                axis.scatter(timing, value * 100, color=colors[index], marker=marker, s=65)
                offset = (4, 5 if source == 'hardhat' else -12)
                adjustments = {('mAP50', 'shwd', 3): (4, 9), ('mAP50', 'shwd', 4): (-24, -16),
                               ('mAP50', 'hardhat', 3): (-30, 9), ('mAP50', 'hardhat', 5): (5, -14),
                               ('person', 'shwd', 3): (4, -20), ('person', 'shwd', 4): (-26, 9),
                               ('person', 'hardhat', 3): (-28, 9), ('person', 'hardhat', 5): (5, -14),
                               ('hat', 'hardhat', 3): (4, -16), ('hat', 'hardhat', 5): (5, 7)}
                offset = adjustments.get((metric_name, source, index), offset)
                axis.annotate(LABELS[index].replace('Original ', 'Old '), (timing, value * 100),
                              xytext=offset, textcoords='offset points', fontsize=8)
        axis.set_xlabel('run2 image benchmark E2E mean (ms)')
        axis.set_ylabel(('mAP50' if metric_name == 'mAP50' else metric_name + ' AP50') + ' (%)')
        axis.grid(alpha=.2)
    fig.suptitle('Diagnostic quality vs recorded RK3568 latency | circle SHWD200 / triangle HardHat200')
    fig.tight_layout()
    save(fig, 'accuracy-speed-quality.png')
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharex=True)
    for row, source in enumerate(SOURCES):
        for column, metric_name in enumerate(('ap50', 'recall')):
            axis = axes[row, column]
            for cls, color in zip(CLASSES, ('#2166ac', '#b35806')):
                axis.plot(range(6), [100 * metrics[source][model][cls][metric_name] for model in MODELS],
                          'o-', color=color, label=cls + (' (bare head)' if cls == 'person' else ''))
            axis.set_title(source.upper() + '200 | ' + ('all-point AP50' if metric_name == 'ap50' else 'recall at .25'))
            axis.set_ylabel('%'); axis.set_ylim(45, 100); axis.grid(alpha=.2); axis.legend(loc='lower right')
            axis.set_xticks(range(6), LABELS, rotation=25, ha='right')
    fig.suptitle('Six preserved models; thresholds fixed before inference; order is categorical')
    fig.tight_layout()
    save(fig, 'accuracy-class-curves.png')
    for metric_name in ('ap50', 'recall'):
        fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharex=True, sharey=True)
        for row, source in enumerate(SOURCES):
            for column, cls in enumerate(CLASSES):
                axis = axes[row, column]
                for index, model in enumerate(MODELS):
                    values = [metrics[source][model]['areas'][area][cls][metric_name] for area in AREAS]
                    axis.plot(range(3), [100 * value if value is not None else math.nan for value in values],
                              'o-', color=colors[index], label=LABELS[index])
                axis.set_title(source.upper() + '200 | ' + cls)
                axis.set_xticks(range(3), AREAS); axis.set_ylim(25, 102); axis.grid(alpha=.2)
                axis.set_ylabel(('AP50' if metric_name == 'ap50' else 'Recall at .25') + ' (%)')
                if source == 'hardhat' and cls == 'person':
                    axis.text(2, 48, 'N/A: no large\nbare-head GT', ha='center', fontsize=10)
        fig.legend(*axes[0, 0].get_legend_handles_labels(), loc='lower center', ncol=3, bbox_to_anchor=(.5, -.03))
        fig.suptitle('Area curves in original source pixels: small <32^2 / medium <96^2 / large >=96^2')
        fig.tight_layout(rect=(0, .05, 1, .96))
        save(fig, 'accuracy-area-' + metric_name + '.png')
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for axis, metric_name in zip(axes, ('ap50', 'recall')):
        for offset, cls, color in ((-.19, 'person', '#2166ac'), (.19, 'hat', '#b35806')):
            delta = [100 * (metrics['hardhat'][model][cls][metric_name] - metrics['shwd'][model][cls][metric_name])
                     for model in MODELS]
            axis.bar([index + offset for index in range(6)], delta, width=.36, label=cls, color=color)
        axis.axhline(0, color='#444444', linewidth=.8)
        axis.set_xticks(range(6), LABELS, rotation=25, ha='right'); axis.set_ylabel('HardHat200 - SHWD200 (pp)')
        axis.set_title('AP50 difference' if metric_name == 'ap50' else 'Recall difference at .25')
        axis.grid(axis='y', alpha=.2); axis.legend()
    fig.suptitle('Signed cross-source changes; positive values are increases')
    fig.tight_layout()
    save(fig, 'accuracy-cross-source.png')
    # Build contact sheets from the twelve archived, hash-verified annotations,
    # in the existing prespecified order; never sort by errors or confidence.
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 20)
    small_font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 17)
    for source in SOURCES:
        for index, model in enumerate(MODELS):
            summary = errors[source][model]
            sheet = Image.new('RGB', (1440, 1284), 'white')
            draw = ImageDraw.Draw(sheet)
            draw.text((18, 12), source.upper() + '200 / ' + LABELS[index] + ' / 12 fixed hash-selected images',
                      fill='#222222', font=font)
            draw.text((18, 42), 'TP green / FP yellow / FN red | confidence >= .25, IoU >= .50 | no score-based selection',
                      fill='#333333', font=small_font)
            for position, item in enumerate(summary['selected']):
                path = MATRIX / 'errors' / source / Path(model).stem / item['artifact']
                with Image.open(path) as image:
                    tile = ImageOps.contain(image.convert('RGB'), (464, 267), Image.Resampling.LANCZOS)
                x, y = 12 + (position % 3) * 476, 80 + (position // 3) * 300
                sheet.paste(tile, (x + (464 - tile.width) // 2, y))
                draw.text((x, y + 271), item['file_name'], fill='#222222', font=small_font)
            name = 'accuracy-errors-' + source + '--' + Path(model).stem + '.png'
            sheet.save(FIGURES / name)
            outputs.append(name)
    return outputs


def write_report(manifest, benchmark, metrics, errors, excluded, evidence):
    lines = ['# 六模型精度、目标尺寸与跨来源诊断报告（S2–S6）', '',
        '日期：2026-11-14。数值来自 `accuracy-matrix-1` 的真实 RK3568 NPU 检测转储；离线重算全部指标，'
        '没有训练、微调、剪枝、蒸馏或阈值搜索。**85% 精度目标没有通过；S6 仅给出待验收的性能/质量配置，不能据此宣称物理摄像头部署定型。**', '',
        '## 结论与两种暂定配置', '',
        '建议保留两种配置供真实场景验证：**响应优先使用 `helmet_416x416.rknn`；小目标质量优先使用 '
        '`helmet_640x640.rknn`**。两个建议保持操作置信度 `.25`、NMS `.45`，依据固定诊断样本作取舍，'
        '不调整阈值以追求通过。它们均须另行通过真实来源采集、实际显示和稳定性门槛；短时图片基准、'
        '文件回放和软件故障注入分别属于不同证据，不能替代物理摄像头验收。', '',
        '2026-11-13 run2 原模型当次 E2E 均值为 **137.94 ms / 7.2 FPS**。这里使用该次 JSON 原始数值，'
        '没有使用日志末尾硬编码的旧 “141 ms / 7.1 FPS”。此基准对一张 1280×960 图片循环 50 次，'
        '预热 5 次，NPU governor 为 performance，结束回读恢复 rknpu_ondemand；它不是摄像头、'
        '显示、输出完成或 30 分钟稳定性测量。', '']
    rows = []
    for index in (1, 5):
        model = MODELS[index]; timing = benchmark[model]
        rows.append([LABELS[index], f"{timing['infer_ms']:.2f}", f"{timing['e2e_ms']:.2f}", timing['fps'],
                     percent(metrics['shwd'][model]['mAP50']), percent(metrics['hardhat'][model]['mAP50']),
                     f"{benchmark[MODELS[0]]['e2e_ms'] / timing['e2e_ms']:.2f}×"])
    lines += [table(['暂定配置', 'NPU均值 ms', '基准E2E均值 ms', '基准FPS', 'SHWD mAP50 %', 'HardHat mAP50 %',
                     '相对当次原模型E2E速度'], rows), '']
    fast, quality = MODELS[1], MODELS[5]
    lines += [f"416×416 相比新 640×640，SHWD 的 hat AP50 下降 "
              f"{percent(metrics['shwd'][quality]['hat']['ap50'] - metrics['shwd'][fast]['hat']['ap50'])} 个百分点，"
              f"裸头召回下降 {percent(metrics['shwd'][quality]['person']['recall'] - metrics['shwd'][fast]['person']['recall'])} 个百分点；"
              f"HardHat 的 hat AP50 下降 {percent(metrics['hardhat'][quality]['hat']['ap50'] - metrics['hardhat'][fast]['hat']['ap50'])} 个百分点，"
              f"裸头召回下降 {percent(metrics['hardhat'][quality]['person']['recall'] - metrics['hardhat'][fast]['person']['recall'])} 个百分点。"
              f"640×640 的单图 E2E 增加 {benchmark[quality]['e2e_ms'] - benchmark[fast]['e2e_ms']:.2f} ms。", '',
              f"小目标质量取舍更明显：SHWD small hat AP50 为 {percent(metrics['shwd'][fast]['areas']['small']['hat']['ap50'])}%"
              f"（416）和 {percent(metrics['shwd'][quality]['areas']['small']['hat']['ap50'])}%（640）。"
              '512×512 是中间备选：HardHat mAP50 比新640×640略高，但 SHWD 小帽子质量较低；'
              '512×288 的速度和两来源总体 AP 均低于416×416，640×384也没有在两来源同时领先512×512。'
              '原640模型在部分指标略优于新640，但其延迟更高；尺寸之外还包含转换工具/目标平台差异，'
              '不得把原模型与新模型的差异归因于分辨率或量化单一因素。', '',
              '![速度与质量](figures/accuracy-speed-quality.png)', '',
              '## 样本、类别与指标口径', '',
              '**模型 `person` 指裸头（未戴安全帽），`hat` 指戴帽头部，不能把整个人体当作裸头框。** '
              'SHWD 用源 `person→person`、`hat→hat`；HardHat 用 `head→person`、`helmet→hat`，'
              '35 个源 `person` 整体人体对象从规范化 GT 中明确忽略，没有并入裸头。', '',
              table(['固定诊断来源', '图片', '有效GT', '裸头GT', '帽GT', '小/中/大裸头GT', '小/中/大帽GT', '特殊策略'], [
                  [source.upper(), 200, evidence['datasets'][source]['counts']['annotations'],
                   metrics[source][MODELS[0]]['person']['npos'], metrics[source][MODELS[0]]['hat']['npos'],
                   '/'.join(str(metrics[source][MODELS[0]]['areas'][a]['person']['npos']) for a in AREAS),
                   '/'.join(str(metrics[source][MODELS[0]]['areas'][a]['hat']['npos']) for a in AREAS),
                   '139 difficult帽按正常GT计入' if source == 'shwd' else '35 full-body person忽略'] for source in SOURCES]), '',
              'SHWD 是原始 VOC2028 测试列表前200 ID；HardHat 是固定 Kaggle API 文件列表前200标注。'
              '选择在推理前固定，未因预测结果更换样本；不是各来源全量评估。没有场景ID，两来源分别保留一个'
              'unknown-scene诊断组，没有虚构场景独立划分。SHWD 的七处文件名映射（6 unique-stem、'
              '1 case-insensitive）、原标注和图片哈希均按 manifest 校验。', '',
              'AP 是项目 all-point **AP50**：IoU=.50、保留分数下限 `.001`，不是完整分数范围AP，'
              '不是官方COCO AP50:95，也不是标准SHWD/VOC difficult-ignore结果。P/R/F1 在操作阈值 `.25` '
              '上计算；F1=2PR/(P+R)。同类别按分数排序，匹配最高IoU的可用GT。mAP为有GT类别AP的算术均值。'
              '尺寸按原始来源像素的GT框面积：small [0,32²)、medium [32²,96²)、large [96²,∞)。'
              '跨面积匹配及落在其他面积的未匹配预测按记录的area ignore规则处理，area P/R/F1不能与全图计数直接相加。'
              'HardHat large person没有GT，AP/R/F1保留N/A；该large mAP仅是hat AP，不能与双类别large mAP直接比较。', '',
              '训练集独立性**未知**，SHWD还存在社区模型可能使用过该来源的风险。已确认 `shwd/000764.jpg` '
              '与 `hardhat/hard_hat_workers1137.png` 为同一照片的缩放/反射填充版本；排除这一张只移除已知交叉来源'
              '重叠，不能证明与训练集独立。139 difficult帽作为正常GT保留，统计口径明确影响可比较性。', '',
              '## 六模型逐类别结果', '',
              '表内AP/P/R/F1均为百分数；P/R/F1使用固定操作阈值.25。原640与新640是不同模型文件。', '']
    for source in SOURCES:
        lines += ['### ' + source.upper() + '200', '']
        rows = []
        for index, model in enumerate(MODELS):
            metric, timing = metrics[source][model], benchmark[model]
            stem = source + '--' + Path(model).stem
            model_link = f'[{LABELS[index]}](accuracy-matrix-1/{stem}.metrics.json)'
            rows.append([model_link] + [percent(metric[cls][key]) for cls in CLASSES
                for key in ('ap50', 'precision', 'recall', 'f1')] + [percent(metric['mAP50']),
                f"{timing['e2e_ms']:.2f}", timing['fps']])
        lines += [table(['模型', '裸头AP50', '裸头P', '裸头R', '裸头F1', '帽AP50', '帽P', '帽R', '帽F1',
                         'mAP50', 'run2 E2E ms', 'run2 FPS'], rows), '']
    lines += ['![类别曲线](figures/accuracy-class-curves.png)', '', '## 小、中、大目标结果', '',
              '每个来源/类别都保留六模型三面积档AP/P/R/F1。N/A表示没有该面积GT，不用0填补。', '']
    for source in SOURCES:
        for cls in CLASSES:
            lines += ['### ' + source.upper() + '200 / ' + cls + ('（裸头）' if cls == 'person' else '（帽）'), '']
            rows = [[LABELS[index]] + [percent(metrics[source][model]['areas'][a][cls][key])
                    for a in AREAS for key in ('ap50', 'precision', 'recall', 'f1')]
                    for index, model in enumerate(MODELS)]
            lines += [table(['模型'] + [a + ' ' + key for a in AREAS for key in ('AP50', 'P', 'R', 'F1')], rows), '']
    lines += ['![面积AP曲线](figures/accuracy-area-ap50.png)', '',
              '![面积召回曲线](figures/accuracy-area-recall.png)', '',
              '## 跨来源变化（百分点与相对变化分开）', '',
              '下表全部为 **HardHat200 − SHWD200** 的百分点（pp），由未四舍五入的原指标相减。'
              '正号表示增加，负号表示降低。两个来源的组成、标注和尺寸分布不同，因此只能称跨来源诊断变化，'
              '不能称独立测试泛化能力。裸头AP/召回增加而帽AP/召回下降，不能合并描述为全部性能衰减。', '']
    rows = []
    for index, model in enumerate(MODELS):
        a, b = metrics['shwd'][model], metrics['hardhat'][model]
        rows.append([LABELS[index]] + [percent(b[cls][key] - a[cls][key], True) for cls in CLASSES
                    for key in ('ap50', 'precision', 'recall', 'f1')] + [percent(b['mAP50'] - a['mAP50'], True)])
    lines += [table(['模型', '裸头ΔAP', '裸头ΔP', '裸头ΔR', '裸头ΔF1', '帽ΔAP', '帽ΔP', '帽ΔR', '帽ΔF1', 'ΔmAP'], rows), '',
              '相对变化另以 `(HardHat−SHWD)/SHWD×100%` 计算；帽AP衰减率为 '
              '`(SHWD帽AP−HardHat帽AP)/SHWD帽AP×100%`。这两种百分比都不是百分点。', '',
              table(['模型', '裸头AP相对变化 %', '帽AP相对变化 %', '帽AP相对衰减率 %', 'mAP相对变化 %'], [
                  [LABELS[index], percent((metrics['hardhat'][model]['person']['ap50'] - metrics['shwd'][model]['person']['ap50']) /
                                         metrics['shwd'][model]['person']['ap50'], True),
                   percent((metrics['hardhat'][model]['hat']['ap50'] - metrics['shwd'][model]['hat']['ap50']) /
                           metrics['shwd'][model]['hat']['ap50'], True),
                   percent((metrics['shwd'][model]['hat']['ap50'] - metrics['hardhat'][model]['hat']['ap50']) /
                           metrics['shwd'][model]['hat']['ap50']),
                   percent((metrics['hardhat'][model]['mAP50'] - metrics['shwd'][model]['mAP50']) /
                           metrics['shwd'][model]['mAP50'], True)] for index, model in enumerate(MODELS)]), '',
              '![跨来源差值](figures/accuracy-cross-source.png)', '',
              '### 排除已知重复照片后的199张敏感性分析', '',
              '此补充评估在查看分数前依据已知重叠文件名排除，原200张指标和原始数据完整保留。'
              '剩余GT为262裸头+690帽=952，不改操作阈值。', '',
              table(['模型', 'HardHat200裸头AP', 'HardHat200帽AP', 'HardHat200 mAP', '199帽AP', '199 mAP', '199−200 mAP pp'], [
                  [LABELS[index], percent(metrics['hardhat'][model]['person']['ap50']),
                   percent(metrics['hardhat'][model]['hat']['ap50']), percent(metrics['hardhat'][model]['mAP50']),
                   percent(excluded['metrics'][model]['hat']['ap50']), percent(excluded['metrics'][model]['mAP50']),
                   percent(excluded['metrics'][model]['mAP50'] - metrics['hardhat'][model]['mAP50'], True)]
                  for index, model in enumerate(MODELS)]), '',
              '## 可复查的TP/FP/FN抽样图', '',
              '每模型每来源12张，绿色TP、黄色FP、红色FN。使用已归档的 '
              '`SHA256("graduation-fixed-before-error-inspection" + NUL + filename)` 最小12个哈希顺序，'
              '六模型对同一来源使用相同样本；不按置信度、误差数或好坏表现选图。这些图只是固定样本，'
              '不是专门挑选的失败案例。原图、单图标注和contact sheet都保留哈希。', '']
    rows = []
    for index, model in enumerate(MODELS):
        links = []
        for source in SOURCES:
            count = errors[source][model]['counts']
            name = 'accuracy-errors-' + source + '--' + Path(model).stem + '.png'
            links += [f"[{source.upper()} 12张](figures/{name})", f"{count['tp']}/{count['fp']}/{count['fn']}"]
        rows.append([LABELS[index]] + links)
    lines += [table(['模型', 'SHWD contact sheet', 'SHWD全200 TP/FP/FN', 'HardHat contact sheet', 'HardHat全200 TP/FP/FN'], rows), '',
              '![416 SHWD固定样本](figures/accuracy-errors-shwd--helmet_416x416.png)', '',
              '![640 HardHat固定样本](figures/accuracy-errors-hardhat--helmet_640x640.png)', '',
              '## 85%目标与验收边界', '',
              f"六模型SHWD mAP50最高为 {percent(max(m['mAP50'] for m in metrics['shwd'].values()))}%，"
              f"HardHat200最高为 {percent(max(m['mAP50'] for m in metrics['hardhat'].values()))}%，"
              f"排除已知重叠的HardHat199最高为 {percent(max(m['mAP50'] for m in excluded['metrics'].values()))}%。"
              '**以当前报告明确使用的双类别项目mAP50口径，85%数值门槛未达到；独立性门槛也未满足。** '
              '个别hat AP或裸头召回超过85%不代表整体目标通过。学校最终目标定义仍须明确，'
              '此处不把单类最高数值代替整体要求；原 metrics 的 `accuracy_target_pass` 保留null，'
              '表示证据资格不足，并非通过。没有检测是未知证据，不能解释为人人戴帽。', '',
              '本报告不加入仍在进行或另批次产生的30分钟NPU回放和新FP16/INT8转换结果；'
              '两者必须以各自实际结束的manifest、原始产物和独立验收记录为准。性能推荐不是量化精度损失结论。', '',
              '## 证据完整性与重建', '',
              '[run manifest](accuracy-matrix-1/run-manifest.json)记录12次顺序执行、每次退出状态、dump/metric哈希、'
              '模型与源码哈希、数据哈希及governor恢复。生成器重新校验全部12个dump为complete、每个200输入'
              '成功且0错误；验证400张实际图片、400份原标注哈希、200张完整预测键（含合法空预测），'
              '重算12组整体/面积/TPFPFN指标、成对跨来源差值和6组199张补充指标。失败dump不能充当空预测。', '',
              '运行时：板端Python3.10.5、NumPy1.21.2、OpenCV4.5.4；归档日志记录RKNN-Lite2.5.0、'
              'librknnrt2.3.2、driver0.9.2。原模型日志记录旧工具1.4.0/rk3566目标，新尺寸记录工具2.3.2/rk3568目标。'
              '基准环境与温度记录见 [run2 environment](../../performance/2026-11-13-environment-run2.txt)，'
              '[run2 JSON](../../performance/2026-11-13-comparison-run2.json)和'
              '[run2 log](../../performance/2026-11-13-benchmark-run2.log)。基准没有延迟分位数和持续资源曲线，'
              '不能从均值推导p95/p99；精度dump含逐图片NPU时间但与单图基准E2E口径不同。', '',
              '下面直接从每个精度dump的200次 `engine.inference` 墙钟时间计算，包含首张调用，'
              '使用NumPy linear分位数；不含前处理、后处理、采集、显示或保存。两来源图像内容不同、'
              '图像顺序固定，因此不把其p95/p99当作真实场景长跑延迟或run2基准分位数。', '',
              table(['来源', '模型', 'NPU均值ms', 'NPU p50 ms', 'NPU p95 ms', 'NPU p99 ms'], [
                  [row['dataset'].upper(), LABELS[MODELS.index(row['model'])]] +
                  [f"{row['inference_timing'][key]:.2f}" for key in ('mean_ms', 'p50_ms', 'p95_ms', 'p99_ms')]
                  for row in evidence['runs']]), '',
              table(['run2模型', 'Infer均值ms', 'Post均值ms', 'E2E均值ms', '基准FPS', '记录温度℃', '循环数'], [
                  [LABELS[index]] + [f"{benchmark[model][key]:.2f}" for key in ('infer_ms', 'post_ms', 'e2e_ms')] +
                  [benchmark[model][key] for key in ('fps', 'temp', 'loops')] for index, model in enumerate(MODELS)]), '',
              '精度实验的**历史执行源码哈希**如下；后续本地源码变动不改写它们。本生成器另记录当前源码哈希'
              '是否匹配；用当前评估器离线重算并验证数值一致，也不代表当前源码已经在这批NPU实验运行。', '',
              '实际执行的四个板端源码已另行复制到 '
              '[runtime-snapshot核查记录](accuracy-matrix-1/runtime-snapshot/snapshot-verification.json)，'
              '本生成器直接读取该副本并逐字节验证全部四个SHA256与原run manifest一致。'
              '因此历史版本可审计，不因当前注释或校验合同变动丢失原实验身份。', '',
              table(['精度运行源码', '归档 SHA256', '当前本地是否匹配'], [[name, digest,
                     str(evidence['current_local_code'][name]['matches_recorded_run']).lower()]
                    for name, digest in manifest['sources'].items()]), '',
              table(['模型', 'SHA256'], [[model, manifest['models'][model]] for model in MODELS]), '',
              table(['来源', '规范化标注文件SHA256', 'manifest文件SHA256'], [[source,
                  evidence['datasets'][source]['annotation_sha256'], evidence['datasets'][source]['manifest_sha256']]
                  for source in SOURCES]), '',
              '来源/重叠证据：[SHWD manifest](../../../datasets/evaluation/shwd/manifest.json)、'
              '[HardHat manifest](../../../datasets/evaluation/hardhat/manifest.json)、'
              '[已知重复照片](../../../datasets/evaluation/cross-source-known-overlap.json)、'
              '[跨来源comparison](accuracy-matrix-1/cross-source-comparison.json)、'
              '[199张补充metrics](accuracy-matrix-1/known-overlap-excluded-199.json)。', '',
              '离线生成产物：[未舍入整体与面积CSV](figures/accuracy-values.csv)、'
              '[验证与哈希记录](figures/accuracy-verification.json)、'
              '[生成器](build_accuracy_report.py)。只读原数据/模型/结果，重建本报告及figures；不访问板端。', '',
              '```powershell', r'.\.venv\Scripts\python.exe docs/experiments/2026-11-14/build_accuracy_report.py',
              r'.\.venv\Scripts\python.exe docs/experiments/2026-11-14/build_accuracy_report.py --verify-only', '```', '']
    report = '\n'.join(lines)
    # Report is three directory levels below root; repository links need three
    # parent segments from HERE (date -> experiments -> docs -> repository).
    (HERE / 'accuracy-results.md').write_text(report, encoding='utf-8')


def verify_links():
    report = HERE / 'accuracy-results.md'
    text = report.read_text(encoding='utf-8')
    links = re.findall(r'\]\(([^)]+)\)', text)
    for target in links:
        require((report.parent / target).is_file(), 'broken report link: ' + target)
    return len(links)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true', help='verify raw evidence and existing report/artifact hashes')
    args = parser.parse_args()
    manifest, benchmark, metrics, errors, excluded, evidence = verify_evidence()
    if args.verify_only:
        archived = read(FIGURES / 'accuracy-verification.json')
        require(archived['builder_sha256'] == sha256(__file__), 'builder changed since report was generated')
        for name, digest in archived['generated_artifacts'].items():
            require(sha256(HERE / name) == digest, 'generated artifact changed: ' + name)
        print('Verified raw evidence, ' + str(verify_links()) + ' links and all generated artifact hashes.')
        return
    FIGURES.mkdir(exist_ok=True)
    outputs = plot_figures(benchmark, metrics, errors)
    with (FIGURES / 'accuracy-values.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['source', 'model', 'area', 'class', 'npos', 'ap50', 'precision', 'recall', 'f1',
                         'tp', 'fp', 'fn', 'benchmark_infer_ms', 'benchmark_e2e_ms', 'benchmark_fps'])
        for source in SOURCES:
            for model in MODELS:
                scopes = {'all': metrics[source][model], **metrics[source][model]['areas']}
                for area, result in scopes.items():
                    for cls in CLASSES:
                        value = result[cls]
                        writer.writerow([source, model, area, cls] + [value[key] for key in
                            ('npos', 'ap50', 'precision', 'recall', 'f1', 'true_positives', 'false_positives', 'false_negatives')] +
                            [benchmark[model][key] for key in ('infer_ms', 'e2e_ms', 'fps')])
    write_report(manifest, benchmark, metrics, errors, excluded, evidence)
    # Create the verification link target before checking all report links.
    verification_path = FIGURES / 'accuracy-verification.json'
    verification_path.write_text('{}', encoding='utf-8')
    evidence['report_links_verified'] = verify_links()
    from PIL import Image
    for name in outputs:
        with Image.open(FIGURES / name) as image:
            image.load()
            require(image.width >= 1000 and image.height >= 500, 'figure render is too small: ' + name)
    artifacts = [HERE / 'accuracy-results.md', FIGURES / 'accuracy-values.csv'] + [FIGURES / name for name in outputs]
    evidence['generated_artifacts'] = {path.relative_to(HERE).as_posix(): sha256(path) for path in artifacts}
    evidence['rendered_figures'] = len(outputs)
    verification_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Generated full report, ' + str(len(outputs)) + ' PNG figures, 96 metric CSV rows; ' +
          str(evidence['report_links_verified']) + ' links verified.')


if __name__ == '__main__':
    main()
