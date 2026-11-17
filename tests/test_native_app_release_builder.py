import importlib.util
import io
from pathlib import Path
import unittest
import zipfile


REPO = Path(__file__).resolve().parents[1]
MODULE_PATHS = {
    'v6-r1': REPO / 'docs/tools/build_native_app_bundle_v6.py',
    'v6-r2': REPO / 'docs/tools/build_native_app_bundle_v6_r2.py',
}


def load_builder(version='v6-r1'):
    module_path = MODULE_PATHS[version]
    spec = importlib.util.spec_from_file_location(
        'build_native_app_bundle_' + version.replace('-', '_'), module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeAppReleaseBuilderTests(unittest.TestCase):
    def test_vendor_wrapper_uses_absolute_posix_paths(self):
        builder = load_builder()
        wrapper = builder.vendor_wrapper('detection').decode('utf-8')
        expected_root = builder.FINAL_ROOT.as_posix()
        self.assertIn(expected_root, wrapper)
        self.assertNotIn('\\userdata', wrapper)
        self.assertIn(expected_root + '/src/app/launch.py', wrapper)

    def test_v6_r2_wrapper_targets_the_new_release_root(self):
        builder = load_builder('v6-r2')
        wrapper = builder.vendor_wrapper('detection').decode('utf-8')
        expected_root = '/userdata/helmet-validation/20261117-native-app-v6-r2'
        self.assertEqual(builder.VERSION, '20261117-v6-r2')
        self.assertIn(expected_root, wrapper)
        self.assertIn(expected_root + '/src/app/launch.py', wrapper)

    def test_v6_r2_archives_executable_entries_with_unix_modes(self):
        builder = load_builder('v6-r2')
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            builder.zip_write(archive, 'vendor-entry/helmet_app', b'#!/bin/sh\n', 0o755)
            builder.zip_write(archive, 'README.txt', b'readme\n', 0o644)
        with zipfile.ZipFile(stream) as archive:
            wrapper = archive.getinfo('vendor-entry/helmet_app')
            readme = archive.getinfo('README.txt')
        self.assertEqual(wrapper.create_system, 3)
        self.assertEqual((wrapper.external_attr >> 16) & 0o777, 0o755)
        self.assertEqual(readme.create_system, 3)
        self.assertEqual((readme.external_attr >> 16) & 0o777, 0o644)


if __name__ == '__main__':
    unittest.main()
