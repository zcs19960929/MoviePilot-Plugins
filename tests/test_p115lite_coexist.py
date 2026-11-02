"""Real shared SDK and complete plugin imports in isolated, offline child processes."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CoexistTests(unittest.TestCase):
    def test_both_import_orders_and_sdk_patch_lifecycle(self):
        for order in ("metatube-first", "115lite-first"):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as tmp:
                env = dict(os.environ)
                env["P115_TEST_SDK_CACHE"] = str(Path(tmp) / "sdk-cache")
                env["P115_TEST_TMPDIR"] = str(Path(tmp) / "temp")
                env["P115_TEST_HOST"] = "1"
                env["PYTHONPATH"] = os.pathsep.join(str(ROOT / p) for p in (
                    "tests/support", "plugins.v3/p115lite/tests/_stub_host", "plugins.v3/p115lite", "plugins.v3"))
                result = subprocess.run([sys.executable, str(ROOT / "tests/p115lite_coexist_probe.py"), order, tmp],
                                        env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout[-1500:] + result.stderr[-3000:])
                self.assertIn('"sdk_and_credentials_isolated": true', result.stdout)


if __name__ == "__main__":
    unittest.main()
