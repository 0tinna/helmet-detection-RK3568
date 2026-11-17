#!/usr/bin/env python3
"""Migrate only the project-owned vendor entries after the release root is verified."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path('/userdata/helmet-validation/20261117-native-app-v6-r1')
EVIDENCE = ROOT / 'evidence/entry-migration'
BACKUP = EVIDENCE / 'backup'
CFG = Path('/opt/ui/src/ATK-DLRK3568/apk3.cfg')
DESKTOPS = {
    'helmet-detection.desktop': Path('/root/.local/share/applications/helmet-detection.desktop'),
    'helmet-wifi.desktop': Path('/root/.local/share/applications/helmet-wifi.desktop'),
}
APP_LINKS = {
    'helmet_app': Path('/opt/ui/src/apps/helmet_app'),
    'helmet_wifi': Path('/opt/ui/src/apps/helmet_wifi'),
}
INIT_LINK = Path('/etc/init.d/S99zhelmet-app')
SERVICE_KEYS = ('helmet_app', 'helmet_wifi')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def link_target(path):
    return os.readlink(path) if path.is_symlink() else None


def write_desktop(path, name, tab, icon):
    app = ROOT / 'src/app'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[Desktop Entry]\n'
        'Type=Application\n'
        'Version=1.0\n'
        f'Name={name}\n'
        f'Exec=/usr/bin/python3 "{app / "launch.py"}" --root "{ROOT}" --tab {tab}\n'
        f'Icon={app / "icons" / (icon + ".svg")}\n'
        'Terminal=false\n'
        'Categories=Utility;\n',
        encoding='utf-8',
    )
    os.chmod(path, 0o644)


def replace_symlink(path, target):
    temporary = path.with_name(path.name + '.v6-tmp')
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    os.symlink(target, temporary)
    os.replace(temporary, path)


def main():
    EVIDENCE.mkdir(mode=0o700, parents=True, exist_ok=False)
    BACKUP.mkdir(mode=0o700)
    before = {
        'app_links': {name: link_target(path) for name, path in APP_LINKS.items()},
        'init_link': link_target(INIT_LINK),
        'cfg_sha256': sha(CFG),
        'systemui_pid': subprocess.run(
            ['pidof', 'systemui'], capture_output=True, text=True, check=False
        ).stdout.strip(),
        'desktops': {
            name: {'sha256': sha(path), 'text': path.read_text(encoding='utf-8')}
            for name, path in DESKTOPS.items()
        },
    }
    shutil.copy2(CFG, BACKUP / 'apk3.cfg')
    for name, path in DESKTOPS.items():
        shutil.copy2(path, BACKUP / name)
    (BACKUP / 'links-before.json').write_text(
        json.dumps(
            {
                'app_links': before['app_links'],
                'init_link': before['init_link'],
            },
            indent=2,
        )
        + '\n',
        encoding='utf-8',
    )

    cfg = CFG.read_text(encoding='utf-8')
    if any(not any(line.split()[-1:] == [key] for line in cfg.splitlines()) for key in SERVICE_KEYS):
        raise RuntimeError('existing vendor configuration no longer contains both project entries')

    for name, path in APP_LINKS.items():
        wrapper = ROOT / 'vendor-entry' / name
        os.chmod(wrapper, 0o700)
        replace_symlink(path, wrapper)

    write_desktop(DESKTOPS['helmet-detection.desktop'], '安全帽检测', 'detection', 'helmet')
    write_desktop(DESKTOPS['helmet-wifi.desktop'], 'Wi-Fi 设置', 'wifi', 'wifi')
    replace_symlink(INIT_LINK, ROOT / 'src/app/autostart.sh')

    after = {
        'app_links': {name: link_target(path) for name, path in APP_LINKS.items()},
        'init_link': link_target(INIT_LINK),
        'cfg_sha256': sha(CFG),
        'systemui_pid': subprocess.run(
            ['pidof', 'systemui'], capture_output=True, text=True, check=False
        ).stdout.strip(),
        'desktops': {
            name: {'sha256': sha(path), 'text': path.read_text(encoding='utf-8')}
            for name, path in DESKTOPS.items()
        },
        'wrapper_modes': {
            name: oct((ROOT / 'vendor-entry' / name).stat().st_mode & 0o777)
            for name in APP_LINKS
        },
    }
    expected_links = {
        name: str(ROOT / 'vendor-entry' / name) for name in APP_LINKS
    }
    passed = (
        before['cfg_sha256'] == after['cfg_sha256']
        and before['systemui_pid'] == after['systemui_pid']
        and after['app_links'] == expected_links
        and after['init_link'] == str(ROOT / 'src/app/autostart.sh')
        and all(mode == '0o700' for mode in after['wrapper_modes'].values())
        and '--root' in after['desktops']['helmet-detection.desktop']['text']
        and str(ROOT) in after['desktops']['helmet-detection.desktop']['text']
        and str(ROOT) in after['desktops']['helmet-wifi.desktop']['text']
    )
    result = {
        'passed': passed,
        'scope': 'only project-owned vendor links, two desktop entries and init link; cfg unchanged; systemui not restarted',
        'root': str(ROOT),
        'before': before,
        'after': after,
        'backup': str(BACKUP),
    }
    (EVIDENCE / 'migration.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except BaseException as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        raise
