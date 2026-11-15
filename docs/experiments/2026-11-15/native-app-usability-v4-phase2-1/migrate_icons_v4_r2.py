#!/usr/bin/env python3
"""Minimal audited migration for the two v4-r2 App icon entries."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid


OLD_ROOT = Path('/userdata/helmet-validation/20261115-native-app-v2')
NEW_ROOT = Path('/userdata/helmet-validation/20261115-native-app-v4-r2-local-2')
CONFIG = Path('/opt/ui/src/ATK-DLRK3568/apk3.cfg')
EXPECTED_CONFIG_SHA = 'f64a8e2f751ff777b98a36a13a0ad062e0812f2bd6ebd7914a512b772f507b11'
DESKTOP_ROOT = Path('/root/.local/share/applications')
LINKS = (
    (
        Path('/opt/ui/src/apps/helmet_app'),
        OLD_ROOT / 'vendor-entry/helmet_app',
        NEW_ROOT / 'vendor-entry/helmet_app',
    ),
    (
        Path('/opt/ui/src/apps/helmet_wifi'),
        OLD_ROOT / 'vendor-entry/helmet_wifi',
        NEW_ROOT / 'vendor-entry/helmet_wifi',
    ),
    (
        Path('/opt/ui/src/appicons/helmet_app.svg'),
        OLD_ROOT / 'src/app/icons/helmet.svg',
        NEW_ROOT / 'src/app/icons/helmet.svg',
    ),
    (
        Path('/opt/ui/src/appicons/helmet_wifi.svg'),
        OLD_ROOT / 'src/app/icons/wifi.svg',
        NEW_ROOT / 'src/app/icons/wifi.svg',
    ),
)
DESKTOPS = (
    DESKTOP_ROOT / 'helmet-detection.desktop',
    DESKTOP_ROOT / 'helmet-wifi.desktop',
)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def atomic_write(path, data, mode):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(
        prefix='.' + path.name + '.', dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_symlink(path, target):
    path = Path(path)
    temporary = path.with_name('.helmet-r2-' + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def preflight():
    require(os.geteuid() == 0, 'migration must run as root')
    require(NEW_ROOT.is_dir(), 'v4-r2 root is missing')
    require(CONFIG.is_file() and not CONFIG.is_symlink(), 'vendor config is missing or unsafe')
    require(sha256_file(CONFIG) == EXPECTED_CONFIG_SHA, 'vendor config SHA differs from the frozen baseline')
    npu = Path('/sys/kernel/debug/rknpu/load').read_text().strip()
    require(re.fullmatch(r'NPU load:\s+0%', npu) is not None, 'NPU must be idle before migration')
    processes = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            command = [
                item.decode('utf-8', errors='replace')
                for item in (proc / 'cmdline').read_bytes().split(b'\0')
                if item
            ]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        joined = ' '.join(command)
        if any(
            token in joined
            for token in (
                str(NEW_ROOT / 'src/app/resident.py'),
                str(NEW_ROOT / 'src/app/service.py'),
                str(NEW_ROOT / 'src/app/launch.py'),
                str(NEW_ROOT / 'app-state/session-'),
            )
        ):
            processes.append({'pid': int(proc.name), 'command': command})
    require(not processes, 'v4-r2 App processes are still live')

    links = {}
    for path, old, new in LINKS:
        require(path.is_symlink(), 'owned link is missing: ' + str(path))
        require(os.readlink(path) == str(old), 'owned link has an unexpected target: ' + str(path))
        require(new.is_file() and not new.is_symlink(), 'new target is missing: ' + str(new))
        if new.suffix == '':
            require(os.access(new, os.X_OK), 'new wrapper is not executable: ' + str(new))
            require(b'\r' not in new.read_bytes(), 'new wrapper contains CR bytes: ' + str(new))
        links[str(path)] = {
            'old_target': str(old),
            'new_target': str(new),
            'new_sha256': sha256_file(new),
        }

    desktops = {}
    for path in DESKTOPS:
        require(path.is_file() and not path.is_symlink(), 'desktop entry is missing: ' + str(path))
        data = path.read_bytes()
        text = data.decode('utf-8')
        require(str(OLD_ROOT) in text, 'desktop does not reference the frozen v2 root: ' + str(path))
        desktops[str(path)] = {
            'bytes': len(data),
            'sha256': sha256_bytes(data),
            'mode': stat.S_IMODE(path.stat().st_mode),
        }
    return {
        'kind': 'native-app-v4-r2-icon-migration',
        'applied': False,
        'npu_load': npu,
        'config': {
            'path': str(CONFIG),
            'sha256_before': sha256_file(CONFIG),
            'bytes': CONFIG.stat().st_size,
        },
        'links': links,
        'desktops': desktops,
        'systemui_restarted': False,
        'human_icon_tap': False,
    }


def migrate(apply):
    report = preflight()
    if not apply:
        return report

    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    backup = NEW_ROOT / 'vendor-entry' / ('icon-migration-r2-' + stamp + '-' + uuid.uuid4().hex[:8])
    require(not backup.exists(), 'backup directory already exists')
    backup.mkdir(mode=0o700)
    report['backup_directory'] = str(backup)

    config_data = CONFIG.read_bytes()
    atomic_write(backup / 'apk3.before.cfg', config_data, 0o600)
    before = {
        'kind': 'native-app-v4-r2-icon-before',
        'config': {
            'path': str(CONFIG),
            'sha256': sha256_bytes(config_data),
            'bytes': len(config_data),
        },
        'links': {},
        'desktops': {},
    }
    for path, old, new in LINKS:
        before['links'][str(path)] = {
            'target': os.readlink(path),
            'device': path.lstat().st_dev,
            'inode': path.lstat().st_ino,
        }
    for path in DESKTOPS:
        data = path.read_bytes()
        before['desktops'][str(path)] = {
            'bytes': len(data),
            'sha256': sha256_bytes(data),
            'mode': stat.S_IMODE(path.stat().st_mode),
        }
        atomic_write(backup / path.name, data, 0o600)
    atomic_write(
        backup / 'before.json',
        json.dumps(before, ensure_ascii=False, indent=2).encode('utf-8') + b'\n',
        0o600,
    )

    desktop_after = {}
    for path in DESKTOPS:
        original = path.read_bytes()
        updated = original.replace(str(OLD_ROOT).encode('utf-8'), str(NEW_ROOT).encode('utf-8'))
        require(str(OLD_ROOT).encode('utf-8') not in updated, 'desktop still references v2 after replacement')
        atomic_write(path, updated, stat.S_IMODE(path.stat().st_mode))
        desktop_after[str(path)] = {
            'bytes': len(updated),
            'sha256': sha256_bytes(updated),
        }
    for path, old, new in LINKS:
        atomic_symlink(path, new)

    require(sha256_file(CONFIG) == EXPECTED_CONFIG_SHA, 'vendor config changed during migration')
    require(os.readlink(CONFIG.parent.parent / 'apps' / 'helmet_app') == str(NEW_ROOT / 'vendor-entry/helmet_app'), 'helmet_app link mismatch')
    require(os.readlink(CONFIG.parent.parent / 'apps' / 'helmet_wifi') == str(NEW_ROOT / 'vendor-entry/helmet_wifi'), 'helmet_wifi link mismatch')
    require(os.readlink(CONFIG.parent.parent / 'appicons' / 'helmet_app.svg') == str(NEW_ROOT / 'src/app/icons/helmet.svg'), 'helmet icon link mismatch')
    require(os.readlink(CONFIG.parent.parent / 'appicons' / 'helmet_wifi.svg') == str(NEW_ROOT / 'src/app/icons/wifi.svg'), 'wifi icon link mismatch')
    for path in DESKTOPS:
        text = path.read_text(encoding='utf-8')
        require(str(OLD_ROOT) not in text and str(NEW_ROOT) in text, 'desktop final content mismatch')

    report.update(
        applied=True,
        config_sha256_after=sha256_file(CONFIG),
        links_after={str(path): os.readlink(path) for path, _old, _new in LINKS},
        desktops_after=desktop_after,
        systemui_restarted=False,
        human_icon_tap=False,
    )
    atomic_write(
        backup / 'result.json',
        json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8') + b'\n',
        0o600,
    )
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(migrate(args.apply), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
