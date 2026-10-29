"""Jellyfin-visible NFO fields, tested through both complete plugin classes."""
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock

from test_metatube_auth import load_plugin


class NfoTests(unittest.TestCase):
    def nfo(self, version="v3", **info):
        plugin = load_plugin(version, Mock())
        plugin.init_plugin({})
        return ET.fromstring(plugin._MetaTubeScraper__build_nfo(info, "SNOS-377"))

    def test_number_prefix_and_source_original_title_are_kept(self):
        cases = [
            ("影片名称", "SNOS-377 影片名称"),
            ("SNOS-377 影片名称", "SNOS-377 影片名称"),
            ("snos-377 影片名称", "SNOS-377 影片名称"),
            ("[SNOS-377] 影片名称", "SNOS-377 影片名称"),
            ("影片名称 [SNOS-377]", "SNOS-377 影片名称"),
            ("SNOS-377", "SNOS-377"),
            ("SNOS-3770 另一番号", "SNOS-377 SNOS-3770 另一番号"),
            ("", "SNOS-377"),
        ]
        for version in ("v2", "v3"):
            for source, title in cases:
                with self.subTest(version=version, source=source):
                    root = self.nfo(version, title=source)
                    self.assertEqual(root.findtext("title"), title)
                    self.assertEqual(root.findtext("originaltitle"), source or "SNOS-377")
                    self.assertEqual(root.findtext("num"), "SNOS-377")

    def test_facts_are_visible_in_plot_and_original_summary_is_preserved(self):
        summary = "原简介 & 特殊 <字符>\n第二行"
        for version in ("v2", "v3"):
            root = self.nfo(version, title="影片", summary=summary, release_date="2026-09-18T00:00:00Z",
                            runtime=120, director="导演", maker="厂商", label="发行商", series="系列",
                            genres=["分类"], actors=["演员"], score=8.5, provider="JAV321", id="snos00377")
            plot = root.findtext("plot")
            for expected in ("番号：SNOS-377", "上映日期：2026-09-18", "片长：120 分钟", "导演：导演",
                             "厂商：厂商", "发行商：发行商", "系列：系列"):
                self.assertIn(expected, plot)
            self.assertTrue(plot.endswith("\n\n" + summary))
            self.assertEqual(root.findtext("premiered"), "2026-09-18")
            self.assertEqual(root.findtext("releasedate"), "2026-09-18")
            self.assertEqual(root.findtext("year"), "2026")
            self.assertEqual(root.findtext("runtime"), "120")
            self.assertEqual(root.findtext("actor/name"), "演员")
            self.assertEqual(root.findtext("genre"), "分类")
            self.assertEqual(root.findtext("uniqueid"), "JAV321:snos00377")

    def test_rating_uses_a_value_element_that_jellyfin_parses(self):
        for version in ("v2", "v3"):
            for score in (0, "8.5", 10):
                root = self.nfo(version, title="影片", score=score)
                rating = root.find("ratings/rating")
                self.assertIsNotNone(rating)
                self.assertEqual(rating.get("max"), "10")
                self.assertEqual(float(rating.findtext("value")), float(score))
                self.assertNotIn("value", rating.attrib)

    def test_missing_and_invalid_metadata_is_not_invented(self):
        for version in ("v2", "v3"):
            for date in (None, "", "0001-01-01", "2026-02-30", "not a date"):
                root = self.nfo(version, title="影片", release_date=date, runtime=-1, score="nan")
                self.assertEqual(root.findtext("plot"), "番号：SNOS-377")
                for tag in ("premiered", "releasedate", "year", "runtime", "ratings"):
                    self.assertIsNone(root.find(tag))


if __name__ == "__main__":
    unittest.main()
