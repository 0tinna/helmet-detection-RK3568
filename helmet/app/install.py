#!/usr/bin/env python3
"""Register user application icons after checked deployment under /userdata."""
import argparse
import os
from pathlib import Path
import shutil
import socket
import shlex
from datetime import datetime, timezone


def vendor_entries(root, vendor_root):
    """Extend the installed vendor icon list, keeping assets/backups in userdata."""
    vendor_root = Path(vendor_root).resolve()
    if vendor_root != Path('/opt/ui'):
        raise ValueError('only the detected /opt/ui vendor launcher is supported')
    host = socket.gethostname()
    if not host.startswith('ATK-DLRK') or '/' in host:
        raise ValueError('unsupported vendor host configuration')
    config = vendor_root / 'src' / host / 'apk3.cfg'
    if not config.is_file():
        raise ValueError('vendor application list was not found')
    old = config.read_text(encoding='utf-8')
    assets = root / 'vendor-entry'
    assets.mkdir(exist_ok=True)
    backup = assets / ('apk3-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.cfg')
    backup.write_text(old, encoding='utf-8')
    additions = []
    for key, name, tab, icon in (('helmet_app', '安全帽检测', 'detection', 'helmet'),
                                 ('helmet_wifi', '无线网络', 'wifi', 'wifi')):
        executable = assets / key
        executable.write_text('#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(root / 'src/app/launch.py'))
                              + ' --root ' + shlex.quote(str(root)) + ' --tab ' + tab + ' "$@"\n', encoding='utf-8')
        executable.chmod(0o755)
        for target, source in ((vendor_root / 'src/apps' / key, executable),
                                (vendor_root / 'src/appicons' / (key + '.svg'), root / 'src/app/icons' / (icon + '.svg'))):
            if target.exists() or target.is_symlink():
                if not target.is_symlink() or target.resolve() != source.resolve():
                    raise ValueError('vendor entry already belongs to another installation')
            else:
                target.symlink_to(source)
        if not any(line.split()[-1:] == [key] for line in old.splitlines()):
            additions.append('appicons/' + key + '.svg ' + name + ' ' + key)
    if additions:
        new_text = old.rstrip('\n') + '\n' + '\n'.join(additions) + '\n'
        staged = assets / 'apk3.updated.cfg'
        staged.write_text(new_text, encoding='utf-8')
        # The user requested native icons. Only this launcher configuration is
        # changed; the executable/assets and rollback copy stay in /userdata.
        shutil.copyfile(staged, config)
    print('Vendor icons registered in ' + str(config))
    print('Configuration backup: ' + str(backup))
    print('The vendor home screen reloads the icon list at its next start; no automatic restart was performed.')


def install(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--vendor-icons', action='store_true', help='register icons in detected ATK /opt/ui launcher')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if not str(root).startswith('/userdata/') or any(c in str(root) for c in '\n\r"\\'):
        parser.error('installation must be in a clean /userdata path')
    app = root / 'src' / 'app'
    required_files = (
        'alert_events.py',
        'alert_hub.py',
        'alert_mail.py',
        'alert_viewer.py',
        'launch.py',
        'mail.example.json',
        'mail_probe.py',
        'resident.py',
        'service.py',
        'wifi.py',
        'qml/Main.qml',
        'icons/helmet.svg',
        'icons/wifi.svg',
    )
    for required in required_files:
        if not (app / required).is_file():
            parser.error('missing deployed application file: ' + required)
    desktop = Path.home() / '.local' / 'share' / 'applications'
    desktop.mkdir(parents=True, exist_ok=True)
    for key, name, tab, icon in (('helmet-detection', '安全帽检测', 'detection', 'helmet'),
                                  ('helmet-wifi', 'Wi-Fi 设置', 'wifi', 'wifi')):
        entry = desktop / (key + '.desktop')
        entry.write_text('[Desktop Entry]\nType=Application\nVersion=1.0\nName=' + name
                         + '\nExec=/usr/bin/python3 "' + str(app / 'launch.py') + '" --root "'
                         + str(root) + '" --tab ' + tab + '\nIcon=' + str(app / 'icons' / (icon + '.svg'))
                         + '\nTerminal=false\nCategories=Utility;\n', encoding='utf-8')
        os.chmod(entry, 0o644)
        print(str(entry))
    print('Native launcher: python3 ' + str(app / 'launch.py') + ' --root ' + str(root))
    if args.vendor_icons:
        vendor_entries(root, '/opt/ui')
    else:
        print('For the ATK icon desktop, use --vendor-icons; ordinary desktop files alone do not register vendor tiles.')


if __name__ == '__main__':
    install()
