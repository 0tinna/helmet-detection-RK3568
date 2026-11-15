"""Meaningful offline-package boundary and tamper regressions; no NPU needed."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import stat
import warnings
import zipfile

from helmet.pc.build_offline_bundle import (build_bundle, extract_bundle, safe_member,
                                             verify_archive, verify_directory, RUNTIME_MODULES, PC_MODULES)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        for name in ('helmet/helmet_realtime.py', 'helmet/yolo_post.py', 'helmet/pc/eval_map.py',
                     'models/helmet-640-640.rknn', 'models/1.jpg'):
            self.put(name, b'fixture')
        for name in RUNTIME_MODULES:
            self.put('helmet/' + name, b'# runtime fixture\n')
        for name in PC_MODULES:
            self.put('helmet/pc/' + name, b'# tool fixture\n')
        for size in ('416x416', '512x288', '512x512', '640x384', '640x640'):
            self.put('helmet/models/helmet_' + size + '.rknn', size.encode())
        for name in ('requirements-board.txt', 'requirements-host.txt'):
            self.put(name, b'# fixture requirements\n')
        self.archive = Path(self.temp.name) / 'bundle.zip'

    def put(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_roundtrip_has_complete_inventory_and_no_credentials(self):
        for name in ('.ssh/id_rsa', '.runtime/python/private.key', '.env',
                     'datasets/raw/private.jpg', 'helmet/private.pem', 'helmet/pc/token.txt', 'helmet/private.py'):
            self.put(name, b'SECRET_DO_NOT_PACKAGE')
        build_bundle(self.root, self.archive)
        manifest = verify_archive(self.archive)
        self.assertEqual(6, len(manifest['models']))
        with zipfile.ZipFile(self.archive) as archive:
            self.assertFalse(any('SECRET_DO_NOT_PACKAGE' in archive.read(name).decode('utf8', 'ignore')
                                 for name in archive.namelist()))
            self.assertIn('README.md', archive.namelist())
            self.assertIn('verify_bundle.py', archive.namelist())
        destination = Path(self.temp.name) / 'unpacked'
        extract_bundle(self.archive, destination)
        self.assertEqual('verified', verify_directory(destination)['status'])

    def test_missing_model_fails_before_archive_is_written(self):
        (self.root / 'helmet/models/helmet_640x640.rknn').unlink()
        with self.assertRaisesRegex(ValueError, 'missing required'):
            build_bundle(self.root, self.archive)
        self.assertFalse(self.archive.exists())

    def test_safe_paths_reject_traversal_absolute_and_windows_names(self):
        for name in ('../escape', '/root/key', 'C:/private', 'src/../escape', 'src\\escape',
                     'src//duplicate', './file', 'src/key:stream', 'src/CON.py'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_member(name)
        self.assertEqual('src/pc/eval_map.py', safe_member('src/pc/eval_map.py'))

    def test_tampered_payload_is_rejected_without_extraction(self):
        build_bundle(self.root, self.archive)
        bad = Path(self.temp.name) / 'tampered.zip'
        with zipfile.ZipFile(self.archive) as old, zipfile.ZipFile(bad, 'w') as new:
            for name in old.namelist():
                new.writestr(name, b'tampered' if name == 'src/helmet_realtime.py' else old.read(name))
        target = Path(self.temp.name) / 'bad-out'
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            extract_bundle(bad, target)
        self.assertFalse(target.exists())

    def test_unlisted_and_duplicate_archive_members_are_rejected(self):
        build_bundle(self.root, self.archive)
        for duplicate in (False, True):
            bad = Path(self.temp.name) / ('duplicate.zip' if duplicate else 'extra.zip')
            with zipfile.ZipFile(self.archive) as old, zipfile.ZipFile(bad, 'w') as new:
                for name in old.namelist():
                    new.writestr(name, old.read(name))
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    new.writestr('src/helmet_realtime.py' if duplicate else 'unlisted-secret.txt', b'extra')
            with self.assertRaises(ValueError):
                verify_archive(bad)

    def test_directory_tamper_and_existing_destination_are_rejected(self):
        build_bundle(self.root, self.archive)
        target = Path(self.temp.name) / 'out'
        extract_bundle(self.archive, target)
        (target / 'input/1.jpg').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            verify_directory(target)
        with self.assertRaises(FileExistsError):
            extract_bundle(self.archive, target)

    def test_source_symlink_cannot_escape_repository(self):
        source = self.root / 'helmet/helmet_realtime.py'
        outside = Path(self.temp.name) / 'secret.py'
        outside.write_text('secret', encoding='utf8')
        source.unlink()
        try:
            source.symlink_to(outside)
        except OSError:
            self.skipTest('platform does not permit creating symlinks')
        with self.assertRaisesRegex(ValueError, 'outside|symlink'):
            build_bundle(self.root, self.archive)

    def test_zip_symlink_is_rejected_on_all_platforms(self):
        build_bundle(self.root, self.archive)
        bad = Path(self.temp.name) / 'symlink.zip'
        with zipfile.ZipFile(self.archive) as old, zipfile.ZipFile(bad, 'w') as new:
            for name in old.namelist():
                info = zipfile.ZipInfo(name)
                info.external_attr = ((stat.S_IFLNK if name == 'input/1.jpg' else stat.S_IFREG) | 0o644) << 16
                new.writestr(info, old.read(name))
        with self.assertRaisesRegex(ValueError, 'symlink'):
            verify_archive(bad)

    def test_required_runtime_module_missing_is_fatal(self):
        (self.root / 'helmet/output_sinks.py').unlink()
        with self.assertRaisesRegex(ValueError, 'missing required'):
            build_bundle(self.root, self.archive)
        self.assertFalse(self.archive.exists())

    def test_wrong_official_wheel_hash_is_rejected(self):
        name = 'rknn_toolkit_lite2-2.5.0-cp310-cp310-manylinux2014_aarch64.whl'
        self.put('dist/deps/' + name, b'wrong-wheel')
        record = {'filename': name, 'source_url':
                  'https://raw.githubusercontent.com/airockchip/rknn-toolkit2/frozen/packages/' + name,
                  'sha256': '0' * 64, 'bytes': 11}
        self.put('dist/deps/wheel-provenance.json', json.dumps(record).encode())
        with self.assertRaisesRegex(ValueError, 'wheel hash mismatch'):
            build_bundle(self.root, self.archive, self.root / 'dist/deps')
        self.assertFalse(self.archive.exists())

    def test_rebuild_is_byte_deterministic_and_refuses_overwrite(self):
        build_bundle(self.root, self.archive)
        other = Path(self.temp.name) / 'other.zip'
        build_bundle(self.root, other)
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).digest(),
                         hashlib.sha256(other.read_bytes()).digest())
        with self.assertRaises(FileExistsError):
            build_bundle(self.root, self.archive)


if __name__ == '__main__':
    unittest.main()
