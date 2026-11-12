"""Number recognition for watermarked release names (run with unittest discover)."""
import unittest
from unittest.mock import Mock

from test_metatube_auth import VERSIONS, load_plugin


def extract(plugin, name, numeric_ids=False):
    return plugin._MetaTubeScraper__extract_number(name, numeric_ids)


class ExtractNumberTests(unittest.TestCase):
    CASES = (
        ("www.98T.la@DV-1149_restored.mp4", "DV-1149"),
        ("www.98T.la@DV-1110.(53dv01110).[Alice.Japan]希志あいの_水印.mp4", "DV-1110"),
        ("www.98T.la@[LTX]DV-1363_restored（有水印）.mp4", "DV-1363"),
        ("DV-1682.mp4", "DV-1682"),
        ("www.98T.la@IPX-939.mp4", "IPX-939"),
        ("www.98T.la@ssis-448_restored.mp4", "SSIS-448"),
        ("FC2-PPV-1234567.mp4", "FC2-PPV-1234567"),
    )

    def test_watermarked_names_keep_their_number(self):
        for version in VERSIONS:
            plugin = load_plugin(version, Mock())
            for name, expected in self.CASES:
                with self.subTest(version=version, name=name):
                    self.assertEqual(extract(plugin, name), expected)

    def test_quality_tokens_stay_filtered(self):
        for version in VERSIONS:
            plugin = load_plugin(version, Mock())
            for name in ("movie.DV-1080P.mkv", "sample.HD-720P.mp4", "[BD-1080P].mp4"):
                with self.subTest(version=version, name=name):
                    self.assertIsNone(extract(plugin, name))


if __name__ == "__main__":
    unittest.main()
