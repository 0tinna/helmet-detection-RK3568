"""Check the experiment validator rejects incomplete benchmark output."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HELMET_DIR = Path(__file__).resolve().parents[1]
MODEL_NAMES = (
    'helmet-640-640.rknn', 'helmet_416x416.rknn', 'helmet_512x288.rknn',
    'helmet_512x512.rknn', 'helmet_640x384.rknn', 'helmet_640x640.rknn',
)


class ComparisonRegressions(unittest.TestCase):
    def fixture(self):
        return [dict(model=name, infer_ms=1.0, post_ms=2.0, e2e_ms=3.0,
                     fps=333.3, loops=50) for name in MODEL_NAMES]

    def run_validator(self, entries):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'comparison.json'
            path.write_text(json.dumps(entries))
            return subprocess.run(
                [sys.executable, str(HELMET_DIR / 'validate_comparison.py'), str(path)],
                capture_output=True, text=True, timeout=5,
            )

    def test_complete_comparison_succeeds(self):
        result = self.run_validator(self.fixture())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_model_is_rejected(self):
        result = self.run_validator(self.fixture()[:-1])
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_invalid_latency_is_rejected(self):
        entries = self.fixture()
        entries[0]['e2e_ms'] = -1.0
        result = self.run_validator(entries)
        self.assertEqual(result.returncode, 1, result.stderr)


if __name__ == '__main__':
    unittest.main()
