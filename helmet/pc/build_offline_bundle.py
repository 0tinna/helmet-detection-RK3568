#!/usr/bin/env python3
"""Build and verify an allowlisted RK3568 runtime ZIP without copying secrets.

No network or inference is performed. A prepared Linux aarch64 Python3.10 board
is required; the package is not a complete offline fresh-board OS installer.
"""
import argparse
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import sys
import zipfile

MODELS = ('helmet-640-640.rknn', 'helmet_416x416.rknn', 'helmet_512x288.rknn',
          'helmet_512x512.rknn', 'helmet_640x384.rknn', 'helmet_640x640.rknn')
RUNTIME_MODULES = ('alarm.py', 'benchmark.py', 'dump_dets.py', 'helmet_realtime.py', 'output_sinks.py',
                   'pipeline_runtime.py', 'sources.py', 'telemetry.py', 'validate_comparison.py', 'yolo_post.py')
PC_MODULES = ('analyze_errors.py', 'convert.py', 'dataset_tools.py', 'eval_map.py')
BOARD_DEPENDENCIES = [
    {'name': 'Python', 'expected': '3.10.* Linux aarch64', 'bundled': False, 'role': 'preinstalled OS interpreter'},
    {'name': 'NumPy', 'expected': '1.21.2', 'bundled': False, 'role': 'preinstalled Buildroot Python extension'},
    {'name': 'OpenCV', 'expected': '4.5.4', 'bundled': False, 'role': 'preinstalled cv2 and codec/GUI system libraries'},
    {'name': 'RKNN Lite2', 'expected': '2.5.0', 'bundled': 'optional wheel', 'role': 'preinstalled Python runtime'},
    {'name': 'psutil / ruamel.yaml', 'expected': 'recorded psutil5.8.0 / ruamel.yaml0.17.31', 'bundled': False,
     'role': 'dependencies declared by Lite2 wheel'},
    {'name': 'kernel / NPU driver / librknnrt', 'expected': 'RK3568 working stack; recorded driver0.9.2 runtime2.3.2',
     'bundled': False, 'role': 'board OS/vendor stack, cannot install from this Python ZIP'}]


def safe_member(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('unsafe archive path')
    parts = name.split('/')
    if name.startswith('/') or any(part in ('', '.', '..') for part in parts):
        raise ValueError('unsafe archive path: ' + name)
    if any(part.rstrip(' .') != part for part in parts):
        raise ValueError('unsafe archive path: ' + name)
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if any(part.split('.')[0].upper() in reserved for part in parts):
        raise ValueError('unsafe Windows device path: ' + name)
    return PurePosixPath(name).as_posix()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_bytes(root, relative):
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('source symlink/outside repository: ' + relative)
    if not path.is_file():
        raise ValueError('missing required file: ' + relative)
    return path.read_bytes()


def _manifest_checks(manifest):
    if manifest.get('schema_version') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('unsupported bundle manifest')
    if set(manifest.get('models', {})) != set(MODELS):
        raise ValueError('bundle requires exactly six preserved models')
    for name, row in manifest['files'].items():
        safe_member(name)
        if name == 'bundle-manifest.json' or not re.fullmatch('[0-9a-f]{64}', row.get('sha256', '')):
            raise ValueError('invalid hash/inventory entry')
        if not isinstance(row.get('bytes'), int) or row['bytes'] < 0:
            raise ValueError('invalid manifest byte size')
    for model, value in manifest['models'].items():
        if manifest['files'].get('models/' + model, {}).get('sha256') != value:
            raise ValueError('model hash/inventory mismatch')


def verify_archive(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('duplicate archive members')
        for info in archive.infolist():
            safe_member(info.filename)
            if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('directory/symlink archive member rejected')
        if 'bundle-manifest.json' not in names:
            raise ValueError('missing bundle manifest')
        manifest = json.loads(archive.read('bundle-manifest.json'))
        _manifest_checks(manifest)
        if set(names) != set(manifest['files']) | {'bundle-manifest.json'}:
            raise ValueError('unlisted/missing archive members')
        for name, row in manifest['files'].items():
            data = archive.read(name)
            if len(data) != row['bytes'] or digest(data) != row['sha256']:
                raise ValueError('hash mismatch: ' + name)
    return manifest


def verify_directory(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'bundle-manifest.json').read_text(encoding='utf8'))
    _manifest_checks(manifest)
    for name, row in manifest['files'].items():
        path = directory / safe_member(name)
        if path.is_symlink() or not path.resolve().is_relative_to(directory) or not path.is_file():
            raise ValueError('missing/unsafe payload: ' + name)
        data = path.read_bytes()
        if len(data) != row['bytes'] or digest(data) != row['sha256']:
            raise ValueError('hash mismatch: ' + name)
    return {'status': 'verified', 'files': len(manifest['files']), 'models': len(manifest['models']),
            'directory': str(directory), 'manifest_sha256': digest((directory / 'bundle-manifest.json').read_bytes())}


def extract_bundle(archive_path, destination):
    manifest = verify_archive(archive_path)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError('destination must be new: ' + str(destination))
    destination.mkdir(parents=True)
    absolute = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for name in sorted(archive.namelist()):
            path = absolute / safe_member(name)
            if not path.resolve().is_relative_to(absolute):
                raise ValueError('extraction escapes destination')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(archive.read(name))
    verify_directory(destination)
    return manifest


def board_check():
    """Read-only imports/version checks; no model load or NPU activity."""
    report = {'python': platform.python_version(), 'system': platform.system(), 'machine': platform.machine(),
              'checks': [], 'status': 'failed', 'npu_exercised': False, 'offline_install_complete': False}
    report['checks'].append({'name': 'platform', 'ok': platform.system() == 'Linux' and
                             platform.machine() in ('aarch64', 'arm64') and sys.version_info[:2] == (3, 10)})
    for module, expected, distribution in (('numpy', '1.21.2', 'numpy'), ('cv2', '4.5.4', 'opencv-python'),
                                           ('rknnlite.api', '2.5.0', 'rknn-toolkit-lite2'),
                                           ('psutil', None, 'psutil'), ('ruamel.yaml', None, 'ruamel.yaml')):
        item = {'name': module, 'expected': expected}
        try:
            imported = importlib.import_module(module)
            if module == 'rknnlite.api' and not hasattr(imported, 'RKNNLite'):
                raise ImportError('RKNNLite API missing')
            value = getattr(imported, '__version__', None)
            if value is None:
                value = importlib.metadata.version(distribution)
            item.update(version=str(value), ok=expected is None or str(value) == expected)
        except (ImportError, OSError, importlib.metadata.PackageNotFoundError) as error:
            item.update(ok=False, error=type(error).__name__ + ': ' + str(error))
        report['checks'].append(item)
    report['status'] = 'ready-for-npu-validation' if all(item['ok'] for item in report['checks']) else 'failed'
    report['limits'] = 'Imports do not verify NPU inference, camera, display, codecs, long runs or a fresh-board offline install.'
    return report


def _readme():
    return '''# RK3568 offline runtime bundle

This bundle contains the existing six helmet models, runtime/PC scripts, sample
image, recorded model provenance, config and byte hashes. It does not train a
model. `person` means a bare head; `hat` means a head wearing a helmet.

The target is a PREPARED Linux aarch64 Python3.10 RK3568 board. Python, NumPy1.21.2,
OpenCV4.5.4, compatible NPU driver/vendor libraries and Lite2 dependencies are
required. This archive is not a complete fresh-board offline installer. See
`dependency-status.json` and `requirements-board.txt`. An included Lite2 wheel
can be installed with `python3 -m pip install --no-index --no-deps deps/*.whl`
ONLY after the required dependency/ABI checks pass; existing working boards need
no replacement installation. NumPy/OpenCV/system libraries are not bundled.

Copy the archive to a new directory below /userdata/helmet-validation. Verify
its SHA256 against the separately delivered .sha256 file, then verify and extract
using the builder's `extract ZIP NEW_DIR` command on the host, or Python's unzip
after archive verification. Extracted verification uses only the standard library:

```sh
cd /userdata/helmet-validation/NEW_BUNDLE
python3 verify_bundle.py verify .
python3 verify_bundle.py board-check
python3 -m compileall -q src
cd src
python3 helmet_realtime.py --help
python3 helmet_realtime.py --model ../models/helmet_416x416.rknn --source ../input --conf .25 --nms .45 --no-display --duration 10 --session-json ../results/replay-session.json
```

Create `results/` before replay; all new outputs belong there. The replay command
is real NPU validation on a repeated sample image, not physical-camera/display
or 30-minute acceptance. Do not run NPU experiments concurrently. No network is
needed by this command on a prepared board, but only an actual isolated child
run can prove its network-denied execution. The coordinating owner records that
separate experiment; this bundle itself does not claim it happened.

`config.json` records provisional profiles and fixed diagnostic thresholds.
416x416 prioritizes response; new640x640 prioritizes small-target quality. Model
choice remains provisional; 85% mAP50 was not met on current diagnostic subsets.
Training independence is unknown. No automatic default or threshold is changed.

Host analysis requires a separately prepared environment (`requirements-host.txt`).
Linux x86 RKNN conversion tools are included as source only; conversion environment
and source ONNX are optional, not included by default. Existing conversion is
deployment conversion, not training. Raw datasets, credentials, keys, .runtime,
venvs and rights to redistribute raw images are excluded. The single sample's
original ownership and optional source model rights remain the provider's rights.
No links to excluded repository documents or datasets are implied to work here.

Integrity hashes detect accidental/tampered payload differences against the
manifest. They are not a cryptographic signature: retain the independent ZIP
SHA256 through a trusted transfer channel.
'''.encode('utf8')


def build_bundle(root, output, wheel_directory=None, include_onnx=False):
    root, output = Path(root).resolve(), Path(output)
    if output.exists():
        raise FileExistsError('refusing to overwrite bundle: ' + str(output))
    payload, sources = {}, {}
    def add(source, target):
        target = safe_member(target)
        payload[target] = source_bytes(root, source)
        sources[target] = source
    # Explicit filenames prevent a future private.py from entering the archive.
    for base, target, modules in (('helmet', 'src', RUNTIME_MODULES), ('helmet/pc', 'src/pc', PC_MODULES)):
        for name in modules:
            add(base + '/' + name, target + '/' + name)
    for model in MODELS:
        add(('models/' if model == MODELS[0] else 'helmet/models/') + model, 'models/' + model)
    add('models/1.jpg', 'input/1.jpg')
    for name in ('requirements-board.txt', 'requirements-host.txt'):
        add(name, name)
    for name in ('community-model-provenance.json', 'coordinate-and-mapping-contract.json'):
        if (root / 'datasets/provenance' / name).is_file():
            add('datasets/provenance/' + name, 'provenance/' + name)
    if include_onnx:
        add('models/source/helmet.onnx', 'source-model/helmet.onnx')
    # The standalone verifier is this file; it imports no project/third-party
    # module until the operator explicitly invokes board-check.
    payload['verify_bundle.py'] = Path(__file__).read_bytes()
    payload['README.md'] = _readme()
    payload['provenance/model-hashes.json'] = json.dumps({
        'models': {model: digest(payload['models/' + model]) for model in MODELS},
        'sample_sha256': digest(payload['input/1.jpg']),
        'sample_source': 'models/1.jpg preserved existing project sample; underlying rights not established',
        'redistribution': 'local authorized transfer only; source-model/raw-image rights not supplied by this bundle'},
        indent=2).encode('utf8')
    payload['config.json'] = json.dumps({'class_order': ['person', 'hat'],
        'model_semantics': {'person': 'bare head (no helmet)', 'hat': 'head wearing helmet'},
        'diagnostic_thresholds': {'conf_min': .001, 'op_conf': .25, 'nms_iou': .45, 'matching_iou': .5},
        'provisional_profiles': {'response': 'helmet_416x416.rknn', 'small_target_quality': 'helmet_640x640.rknn'},
        'default_changed': False, 'accuracy_target_pass': None, 'training_independence': 'unknown'},
        indent=2).encode('utf8')
    dependencies = [dict(row) for row in BOARD_DEPENDENCIES]
    wheel_record = None
    if wheel_directory:
        wheel_directory = Path(wheel_directory).resolve()
        if not wheel_directory.is_relative_to(root):
            raise ValueError('wheel directory outside repository')
        provenance = wheel_directory / 'wheel-provenance.json'
        wheel_record = json.loads(source_bytes(root, provenance.relative_to(root).as_posix()))
        name = safe_member(wheel_record['filename'])
        if '/' in name or not name.endswith('.whl') or 'cp310-cp310' not in name or 'aarch64' not in name:
            raise ValueError('wheel is not cp310 Linux aarch64')
        if not wheel_record['source_url'].startswith('https://raw.githubusercontent.com/airockchip/rknn-toolkit2/'):
            raise ValueError('wheel must have official source provenance')
        data = source_bytes(root, (wheel_directory / name).relative_to(root).as_posix())
        if digest(data) != wheel_record['sha256'] or len(data) != wheel_record['bytes']:
            raise ValueError('wheel hash mismatch')
        payload['deps/' + name] = data
        payload['deps/wheel-provenance.json'] = provenance.read_bytes()
        dependencies[3]['bundled'] = True
    payload['dependency-status.json'] = json.dumps({'offline_fresh_board_install_complete': False,
        'prepared_board_runtime_bundle': True, 'dependencies': dependencies,
        'missing_dependency_artifacts': ['Python3.10 aarch64 OS interpreter', 'NumPy1.21.2 Buildroot package',
            'OpenCV4.5.4 Buildroot package and system libraries', 'psutil/ruamel.yaml packages if absent',
            'RK3568 kernel/NPU driver/vendor runtime stack'],
        'lite_wheel': wheel_record}, indent=2).encode('utf8')
    manifest = {'schema_version': 1, 'scope': 'existing-model runtime deployment; no training',
                'build_date': '2026-11-14', 'files': {}, 'models': {}, 'source_files': sources,
                'offline_fresh_board_install_complete': False}
    for name, data in sorted(payload.items()):
        manifest['files'][name] = {'sha256': digest(data), 'bytes': len(data)}
    manifest['models'] = {model: manifest['files']['models/' + model]['sha256'] for model in MODELS}
    payload['bundle-manifest.json'] = json.dumps(manifest, sort_keys=True, indent=2).encode('utf8')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 14, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
    verify_archive(output)
    output.with_suffix(output.suffix + '.sha256').write_text(digest(output.read_bytes()) + '  ' + output.name + '\n', encoding='ascii')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build')
    build.add_argument('--root', default=str(Path(__file__).resolve().parents[2]))
    build.add_argument('--out', required=True)
    build.add_argument('--wheel-dir')
    build.add_argument('--include-onnx', action='store_true')
    verify = commands.add_parser('verify')
    verify.add_argument('path')
    extract = commands.add_parser('extract')
    extract.add_argument('archive'); extract.add_argument('destination')
    commands.add_parser('board-check')
    args = parser.parse_args()
    try:
        if args.command == 'build':
            manifest = build_bundle(args.root, args.out, args.wheel_dir, args.include_onnx)
            result = {'status': 'verified', 'archive': args.out, 'files': len(manifest['files']),
                      'models': 6, 'sha256': digest(Path(args.out).read_bytes()), 'offline_fresh_board_install_complete': False}
        elif args.command == 'verify':
            path = Path(args.path)
            result = verify_directory(path) if path.is_dir() else {'status': 'verified',
                      'files': len(verify_archive(path)['files']), 'archive': str(path), 'sha256': digest(path.read_bytes())}
        elif args.command == 'extract':
            extract_bundle(args.archive, args.destination)
            result = verify_directory(args.destination)
        else:
            result = board_check()
        print(json.dumps(result, indent=2))
        if result['status'] == 'failed':
            return 1
        return 0
    except (ValueError, OSError, zipfile.BadZipFile, KeyError) as error:
        print('bundle error: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
