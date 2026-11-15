#!/usr/bin/env python3
"""Run the six preserved models on both fixed datasets in one isolated board run.

No training or threshold search. Restore the measured NPU policy in finally;
retain every dump (including failed), metric, per-process log and run manifest.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import time


MODELS = ('helmet-640-640.rknn', 'helmet_416x416.rknn', 'helmet_512x288.rknn',
          'helmet_512x512.rknn', 'helmet_640x384.rknn', 'helmet_640x640.rknn')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/userdata/helmet-validation/20261114')
    parser.add_argument('--run', required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not root.is_relative_to(Path('/userdata/helmet-validation')):
        parser.error('root must be in isolated validation tree')
    out = root / 'results' / args.run
    out.mkdir(parents=True, exist_ok=False)
    governor = Path('/sys/class/devfreq/fde40000.npu/governor')
    original = governor.read_text().strip()
    manifest = {'started_utc': datetime.now(timezone.utc).isoformat(),
                'status': 'running', 'original_governor': original, 'runs': [],
                'thresholds': {'conf_min': .001, 'op_conf': .25, 'nms_iou': .45},
                'sources': {}, 'models': {name: digest(root/'models'/name) for name in MODELS}}
    for name in ('dump_dets.py', 'yolo_post.py', 'pc/eval_map.py', 'pc/dataset_tools.py'):
        manifest['sources'][name] = digest(root/'src'/name)
    def save():
        (out/'run-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf8')
    def terminate(signum, frame):
        raise KeyboardInterrupt('termination signal %d' % signum)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, terminate)
    save()
    try:
        governor.write_text('performance')
        manifest['benchmark_governor'] = governor.read_text().strip()
        for dataset in ('shwd', 'hardhat'):
            data = root/dataset
            manifest.setdefault('datasets', {})[dataset] = {
                'annotation_sha256': digest(data/'_annotations.coco.json'),
                'manifest_sha256': digest(data/'manifest.json')}
            for model in MODELS:
                stem = dataset + '--' + Path(model).stem
                row = {'dataset': dataset, 'model': model, 'status': 'running'}
                manifest['runs'].append(row)
                start = time.monotonic()
                dump = out/(stem+'.dump.json')
                metrics = out/(stem+'.metrics.json')
                with (out/(stem+'.log')).open('wb') as log:
                    run = subprocess.run([sys.executable, str(root/'src/dump_dets.py'),
                        '--model', str(root/'models'/model), '--data', str(data),
                        '--conf-min', '.001', '--op-conf', '.25', '--nms-iou', '.45',
                        '--out', str(dump)], cwd=root/'src', stdout=log,
                        stderr=subprocess.STDOUT, timeout=900)
                    row['dump_exit'] = run.returncode
                    if run.returncode != 0:
                        raise RuntimeError('incomplete dump: ' + stem)
                    value = json.loads(dump.read_text())
                    if value['status'] != 'complete' or value['counts']['completed'] != 200:
                        raise RuntimeError('not all 200 images completed: ' + stem)
                    evaluated = subprocess.run([sys.executable, str(root/'src/pc/eval_map.py'),
                        '--data', str(data), '--dets', str(dump), '--op-conf', '.25',
                        '--out', str(metrics)], stdout=log, stderr=subprocess.STDOUT,
                        timeout=120)
                    row['eval_exit'] = evaluated.returncode
                    if evaluated.returncode != 0:
                        raise RuntimeError('metric validation failed: ' + stem)
                row.update(status='complete', elapsed_seconds=time.monotonic()-start,
                           dump_sha256=digest(dump), metrics_sha256=digest(metrics),
                           metrics_file=metrics.name, dump_file=dump.name)
                save()
                print(stem + ' complete', flush=True)
        manifest['status'] = 'complete'
    except BaseException as error:
        manifest.update(status='failed', error=type(error).__name__ + ': ' + str(error))
        save()
        raise
    finally:
        governor.write_text(original)
        restored = governor.read_text().strip()
        manifest.update(restored_governor=restored,
                        finished_utc=datetime.now(timezone.utc).isoformat())
        save()
        if restored != original:
            raise RuntimeError('NPU governor restoration failed')


if __name__ == '__main__':
    main()
