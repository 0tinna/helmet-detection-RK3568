#!/usr/bin/env python3
"""Build the frozen v6-r2 native application bundle without replacing old releases."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import stat
import zipfile


VERSION = '20261117-v6-r2'
FINAL_ROOT = PurePosixPath('/userdata/helmet-validation/20261117-native-app-v6-r2')
FIXED_TIME = (2026, 11, 17, 0, 0, 0)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def zip_write(archive, name, data, mode=0o644):
    info = zipfile.ZipInfo(name, FIXED_TIME)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | mode) << 16
    archive.writestr(info, data)


def vendor_wrapper(tab):
    if tab not in ('detection', 'wifi'):
        raise ValueError('unsupported vendor tab')
    return (
        '#!/bin/sh\n'
        'exec /usr/bin/python3 '
        + shlex.quote(FINAL_ROOT.as_posix() + '/src/app/launch.py')
        + ' --root '
        + shlex.quote(FINAL_ROOT.as_posix())
        + ' --tab '
        + tab
        + ' "$@"\n'
    ).encode('utf-8')


def build(root, output, manifest_path):
    if output.exists():
        raise FileExistsError(f'refusing to replace existing release: {output}')
    if manifest_path.exists():
        raise FileExistsError(f'refusing to replace existing manifest: {manifest_path}')

    payload = {}
    for path in (root / 'helmet').glob('*.py'):
        payload['src/' + path.name] = path.read_bytes()
    for path in (root / 'helmet/app').rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts:
            relative = path.relative_to(root / 'helmet/app').as_posix()
            payload['src/app/' + relative] = path.read_bytes()
    for path in (root / 'helmet/models').glob('*.rknn'):
        payload['models/' + path.name] = path.read_bytes()
    payload['models/helmet-640-640.rknn'] = (
        root / 'models/helmet-640-640.rknn'
    ).read_bytes()
    payload['input/1.jpg'] = (root / 'models/1.jpg').read_bytes()
    for path in (root / 'docs/app').glob('*.md'):
        payload['docs/' + path.name] = path.read_bytes()

    for key, tab in (('helmet_app', 'detection'), ('helmet_wifi', 'wifi')):
        payload[f'vendor-entry/{key}'] = vendor_wrapper(tab)

    payload['README.txt'] = (
        'RK3568 native Qt helmet detection application.\n'
        'Existing compatible Qt5/qmlscene, Wayland, Python3, OpenCV, NumPy, '
        'RKNNLite and camera drivers are required.\n'
        'Extract the complete bundle to '
        + str(FINAL_ROOT)
        + ' and verify manifest.json before first use.\n'
        'Launch: python3 src/app/launch.py --root '
        + str(FINAL_ROOT)
        + '\n'
        'LAN viewer after the resident service starts: http://BOARD_IP:8091/\n'
        'Local detection uses the board model and does not need internet or a '
        'browser. Physical camera, human display continuity, phone touch and '
        'WiFi authentication remain separately validated gates.\n'
    ).encode('utf-8')

    code_files = ('resident.py', 'launch.py', 'service.py', 'wifi.py', 'qml/Main.qml')
    identity = hashlib.sha256()
    for relative in code_files:
        identity.update(relative.encode('utf-8'))
        identity.update(b'\0')
        identity.update(hashlib.sha256(payload['src/app/' + relative]).digest())

    files = {
        name: {'sha256': digest(data), 'bytes': len(data)}
        for name, data in sorted(payload.items())
    }
    internal_manifest = {
        'kind': 'native-board-app',
        'version': VERSION,
        'release_tag': 'native-app-v6-r2',
        'requires_preinstalled_os_runtime': True,
        'requires_browser': False,
        'offline_daily_detection': True,
        'lan_preview_port': 8091,
        'lan_controls': False,
        'boot_background_manual_detection': True,
        'physical_camera_tested_this_version': False,
        'human_touch_verified_this_version': False,
        'wifi_connect_tested_this_version': False,
        'code_identity': identity.hexdigest(),
        'files': files,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(payload.items()):
            zip_write(
                archive,
                name,
                data,
                0o755 if name.startswith('vendor-entry/') or name.endswith('.sh') else 0o644,
            )
        zip_write(
            archive,
            'manifest.json',
            (json.dumps(internal_manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
        )

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        expected = set(files) | {'manifest.json'}
        if names != expected:
            raise RuntimeError('bundle member set does not match its manifest')
        for name, expected_value in files.items():
            data = archive.read(name)
            if digest(data) != expected_value['sha256'] or len(data) != expected_value['bytes']:
                raise RuntimeError('bundle hash mismatch: ' + name)
        packaged_manifest = json.loads(archive.read('manifest.json'))
        if packaged_manifest != internal_manifest:
            raise RuntimeError('packaged manifest differs from generated manifest')

    archive_bytes = output.read_bytes()
    report = {
        'kind': 'native-app-release-manifest',
        'version': VERSION,
        'tag': 'native-app-v6-r2',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'path': output.relative_to(root).as_posix(),
        'sha256': digest(archive_bytes),
        'bytes': len(archive_bytes),
        'entries': len(payload) + 1,
        'payload_files': len(payload),
        'deployment_root': str(FINAL_ROOT),
        'code_identity': identity.hexdigest(),
        'manifest': internal_manifest,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        '--out',
        type=Path,
        default=None,
    )
    parser.add_argument('--manifest', type=Path, default=None)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.out or root / f'dist/helmet-native-app-{VERSION}.zip'
    manifest = args.manifest or (
        root
        / 'docs/experiments/2026-11-17/native-app-v6-r2/bundle-manifest-v6-r2.json'
    )
    report = build(root, output, manifest)
    print(json.dumps({
        key: report[key]
        for key in ('path', 'sha256', 'bytes', 'entries', 'code_identity')
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
