#!/usr/bin/env python3
"""Build the v7-r1 native application candidate without replacing old releases."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import stat
import zipfile


VERSION = '20261124-v7-r1'
FINAL_ROOT = PurePosixPath('/userdata/helmet-validation/20261124-native-app-v7-r1')
FIXED_TIME = (2026, 11, 24, 0, 0, 0)
CODE_FILES = ('resident.py', 'launch.py', 'service.py', 'wifi.py', 'qml/Main.qml')
ALERT_FILES = (
    'alert_events.py',
    'alert_mail.py',
    'alert_viewer.py',
    'alert_hub.py',
    'mail.example.json',
    'service.py',
    'launch.py',
    'resident.py',
)
APP_ROOT_FILES = {
    '__init__.py', 'alert_events.py', 'alert_hub.py', 'alert_mail.py',
    'alert_viewer.py', 'autostart.sh', 'boot.py', 'deploy.ps1', 'install.py',
    'lan.py', 'launch.py', 'mail.example.json', 'resident.py', 'service.py',
    'wifi.py',
}
SECRET_NAMES = {'mail.json', 'public-access.key', 'token', 'ready.json'}
SKIP_DIRECTORIES = {'__pycache__', '.pytest_cache', 'app-state', 'results'}


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


def _safe_app_file(path, app):
    if path.is_symlink() or not path.is_file():
        return False
    relative = path.relative_to(app)
    if any(part in SKIP_DIRECTORIES for part in relative.parts):
        return False
    if path.name in SECRET_NAMES:
        return False
    if len(relative.parts) == 1:
        return path.name in APP_ROOT_FILES
    return relative.parts[0] in {'icons', 'qml'}


def collect_payload(root):
    root = Path(root)
    payload = {}
    for path in (root / 'helmet').glob('*.py'):
        payload['src/' + path.name] = path.read_bytes()
    app = root / 'helmet/app'
    for path in app.rglob('*'):
        if _safe_app_file(path, app):
            payload['src/app/' + path.relative_to(app).as_posix()] = path.read_bytes()
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
        'RKNNLite, camera drivers and network configuration are required.\n'
        'Extract the complete bundle to ' + FINAL_ROOT.as_posix()
        + ' and verify manifest.json before first use.\n'
        'Launch: python3 src/app/launch.py --root '
        + FINAL_ROOT.as_posix() + '\n'
        'Existing LAN preview after the resident service starts: http://BOARD_IP:8091/\n'
        'Authenticated event archive: http://BOARD_IP:8092/ after router forwarding.\n'
        'Never place mail.json or public-access.key in a release archive.\n'
        'Physical camera, human display, SMTP, router NAT and external phone '
        'checks remain separate acceptance gates.\n'
    ).encode('utf-8')
    return payload


def _identity(payload, names):
    identity = hashlib.sha256()
    for relative in names:
        identity.update(relative.encode('ascii') + b'\0')
        identity.update(hashlib.sha256(payload['src/app/' + relative]).digest())
    return identity.hexdigest()


def code_identity_from_payload(payload):
    return _identity(payload, CODE_FILES)


def alert_identity_from_payload(payload):
    return _identity(payload, ALERT_FILES)


def make_manifest(payload):
    files = {
        name: {'sha256': digest(data), 'bytes': len(data)}
        for name, data in sorted(payload.items())
    }
    return {
        'kind': 'native-board-app',
        'version': VERSION,
        'release_tag': 'native-app-v7-r1',
        'deployment_root': FINAL_ROOT.as_posix(),
        'requires_preinstalled_os_runtime': True,
        'requires_browser': False,
        'offline_daily_detection': True,
        'lan_preview_port': 8091,
        'public_event_port': 8092,
        'alert_event_feature': True,
        'public_event_viewer_requires_access_invite': True,
        'public_live_preview_exposed': False,
        'event_retention_days': 7,
        'event_max_bytes': 268435456,
        'event_max_records': 5000,
        'event_free_space_floor_bytes': 134217728,
        'smtp_credentials_included': False,
        'public_access_key_included': False,
        'public_nat_verified': False,
        'smtp_delivery_tested_this_version': False,
        'physical_camera_alert_scenarios_tested': False,
        'human_touch_verified_this_version': False,
        'code_identity': code_identity_from_payload(payload),
        'alert_feature_identity': alert_identity_from_payload(payload),
        'files': files,
    }


def build(root, output, manifest_path):
    root = Path(root)
    output, manifest_path = Path(output), Path(manifest_path)
    if output.exists():
        raise FileExistsError(f'refusing to replace existing release: {output}')
    if manifest_path.exists():
        raise FileExistsError(f'refusing to replace existing manifest: {manifest_path}')
    payload = collect_payload(root)
    internal_manifest = make_manifest(payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        manifest_bytes = (
            json.dumps(internal_manifest, ensure_ascii=False, indent=2)
            + '\n').encode('utf-8')
        entries = dict(payload)
        entries['manifest.json'] = manifest_bytes
        for name, data in sorted(entries.items()):
            zip_write(
                archive,
                name,
                data,
                0o755 if name.startswith('vendor-entry/') or name.endswith('.sh')
                else 0o644,
            )
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        expected = set(payload) | {'manifest.json'}
        if names != expected:
            raise RuntimeError('bundle member set does not match its manifest')
        for name, expected_value in internal_manifest['files'].items():
            data = archive.read(name)
            if (digest(data) != expected_value['sha256']
                    or len(data) != expected_value['bytes']):
                raise RuntimeError('bundle hash mismatch: ' + name)
        packaged_manifest = json.loads(archive.read('manifest.json'))
        if packaged_manifest != internal_manifest:
            raise RuntimeError('packaged manifest differs from generated manifest')
    archive_bytes = output.read_bytes()
    try:
        archive_path = output.relative_to(root).as_posix()
    except ValueError:
        archive_path = output.as_posix()
    report = {
        'kind': 'native-app-release-manifest',
        'version': VERSION,
        'tag': 'native-app-v7-r1',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'path': archive_path,
        'sha256': digest(archive_bytes),
        'bytes': len(archive_bytes),
        'entries': len(payload) + 1,
        'payload_files': len(payload),
        'deployment_root': FINAL_ROOT.as_posix(),
        'code_identity': internal_manifest['code_identity'],
        'alert_feature_identity': internal_manifest['alert_feature_identity'],
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
    parser.add_argument('--out', type=Path)
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.out or root / f'dist/helmet-native-app-{VERSION}.zip'
    manifest = args.manifest or (
        root / 'docs/experiments/2026-11-24/native-app-v7-r1/'
        'bundle-manifest-v7-r1.json'
    )
    report = build(root, output, manifest)
    print(json.dumps({
        key: report[key]
        for key in ('path', 'sha256', 'bytes', 'entries',
                    'code_identity', 'alert_feature_identity')
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
