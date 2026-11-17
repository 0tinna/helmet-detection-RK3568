#!/usr/bin/env python3
"""Verify a complete v6 release root against its packaged manifest."""

import argparse
import hashlib
import json
from pathlib import Path
import sys


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    expected = manifest['files']
    ignored_names = {'verify_installed_release.py'}
    ignored_parts = {'__pycache__', 'app-state', 'results', 'evidence'}
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob('*')
        if path.is_file()
        and path.name != 'manifest.json'
        and path.name not in ignored_names
        and not path.name.endswith('.pyc')
        and not ignored_parts.intersection(path.relative_to(root).parts)
    }
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        raise RuntimeError(f'payload set mismatch; missing={missing}; extra={extra}')
    mismatches = []
    for relative, row in expected.items():
        path = root / relative
        value = digest(path)
        if value != row['sha256'] or path.stat().st_size != row['bytes']:
            mismatches.append(relative)
    if mismatches:
        raise RuntimeError('payload hash mismatch: ' + ', '.join(mismatches))
    sys.path.insert(0, str(root / 'src'))
    from app import resident

    code_identity = resident.code_identity(root / 'src/app')
    if code_identity != manifest['code_identity']:
        raise RuntimeError('code identity mismatch')
    result = {
        'passed': True,
        'root': str(root),
        'manifest_version': manifest['version'],
        'release_tag': manifest['release_tag'],
        'payload_files': len(expected),
        'code_identity': code_identity,
        'files': {
            relative: digest(root / relative)
            for relative in (
                'src/app/resident.py',
                'src/app/launch.py',
                'src/app/service.py',
                'src/app/wifi.py',
                'src/app/qml/Main.qml',
            )
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
