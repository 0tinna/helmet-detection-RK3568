import importlib.util
from pathlib import Path
import unittest


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / 'docs/tools/build_native_app_bundle_v6.py'


def load_builder():
    spec = importlib.util.spec_from_file_location('build_native_app_bundle_v6', MODULE_PATH)
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


if __name__ == '__main__':
    unittest.main()
