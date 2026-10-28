"""Offline acceptance tests: no MoviePilot, 115Lite, Cookie or network required."""
import dataclasses
import importlib.util
import json
import re
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def components(version="v3"):
    directory = ROOT / f"plugins.{version}" / "metatubescraper"
    path = directory / "direct.py"
    if not path.exists():
        raise AssertionError(f"{version}: independent 115 processing is missing")
    name = f"direct_acceptance_{version}"
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    spec = importlib.util.spec_from_file_location(name + ".direct", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: package, spec.name: module}):
        spec.loader.exec_module(module)
    return module


class FakeCloud:
    account = "12345"

    def __init__(self, module, entries):
        self.module = module
        self.entries = {e.id: e for e in entries}
        self.renames = []
        self.downloads = []
        self.links = []
        self.fail_rename = None
        self.raise_after_rename = None

    def resolve_folder(self, value):
        return str(value).removeprefix("id:")

    def listdir(self, cid):
        return [e for e in self.entries.values() if e.parent_id == str(cid)]

    def info(self, fid):
        return self.entries[str(fid)]

    def rename(self, fid, name):
        if fid == self.fail_rename:
            raise RuntimeError("simulated failed rename")
        self.renames.append((fid, name))
        self.entries[fid] = dataclasses.replace(self.entries[fid], name=name)
        if fid == self.raise_after_rename:
            self.raise_after_rename = None
            raise RuntimeError("response lost after successful rename")

    def download_bytes(self, entry):
        self.downloads.append(entry.id)
        return ("subtitle:" + entry.id).encode()

    def download_link(self, entry, user_agent="", background=False):
        self.links.append((entry.id, user_agent))
        return self.module.DirectLink("https://cdn.115.com/video?t=4102444800", {}, 4102444800)


class DirectAcceptanceTests(unittest.TestCase):
    def make(self, directory, version="v3", entries=None):
        m = components(version)
        entries = entries or [
            m.Entry("1", "0", "DSOD-060 original.mp4", pickcode="a" * 17),
            m.Entry("2", "0", "DSOD-060 original.zh.ass", pickcode="b" * 17),
        ]
        cloud = FakeCloud(m, entries)
        metadata = Mock(return_value={"provider": "FANZA", "id": "film", "title": "Example"})
        service = m.DirectService(
            client=cloud, data_dir=Path(directory) / "state",
            output_root=Path(directory) / "library", playback_base_url="https://mp.example",
            metadata=metadata, artwork=lambda info, kind: b"image:" + kind.encode(),
            build_nfo=lambda info, number: ("<movie><title>" + info["title"] + "</title></movie>").encode(),
            extract_number=lambda text: (re.search(r"[A-Z]{2,6}-\d{2,5}", text.upper()) or [None])[0],
        )
        self.addCleanup(service.close)
        return m, cloud, metadata, service

    def test_independent_pipeline_and_idempotence_both_versions(self):
        for version in ("v2", "v3"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                m, cloud, metadata, service = self.make(directory, version)
                stats = service.run(["0"])
                self.assertEqual(stats["ok"], 1)
                output = Path(directory) / "library" / "DSOD-060"
                self.assertEqual(cloud.entries["1"].name, "DSOD-060.mp4")
                self.assertEqual(cloud.entries["2"].name, "DSOD-060.zh.ass")
                self.assertEqual((output / "DSOD-060.zh.ass").read_bytes(), b"subtitle:2")
                strm = (output / "DSOD-060.strm").read_text()
                self.assertTrue(strm.startswith("https://mp.example/api/v1/plugin/MetaTubeScraper/redirect_url?media_id="))
                self.assertNotIn("P115Lite", strm)
                self.assertNotIn("pickcode", strm)
                self.assertIn(b"poster.jpg", (output / "movie.nfo").read_bytes())
                for name in ("poster.jpg", "fanart.jpg"):
                    self.assertTrue((output / name).read_bytes())
                calls = list(cloud.renames)
                self.assertEqual(service.run(["0"])["skip"], 1)
                self.assertEqual(cloud.renames, calls)
                (output / "DSOD-060.strm").unlink()
                (output / "DSOD-060.zh.ass").unlink()
                self.assertEqual(service.run(["0"])["ok"], 1)
                self.assertEqual(cloud.renames, calls)
                self.assertEqual((output / "DSOD-060.strm").read_text(), strm)

    def test_metadata_failure_never_renames(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            metadata.return_value = None
            self.assertEqual(service.run(["0"])["notfound"], 1)
            self.assertEqual(cloud.renames, [])

    def test_subtitle_arriving_after_completed_run_is_added(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            service.run(["0"])
            cloud.entries["3"] = m.Entry("3", "0", "DSOD-060.en.srt", pickcode="c" * 17)
            self.assertEqual(service.run(["0"])["ok"], 1)
            self.assertEqual((Path(directory) / "library/DSOD-060/DSOD-060.en.srt").read_bytes(), b"subtitle:3")

    def test_force_refreshes_metadata_but_does_not_repeat_cloud_renames(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            service.run(["0"])
            rename_calls = list(cloud.renames)
            metadata.return_value = {"provider": "FANZA", "id": "film", "title": "Corrected title"}
            cloud.entries["3"] = m.Entry("3", "0", "DSOD-060.en.srt", pickcode="c" * 17)
            service.force = True
            self.assertEqual(service.run(["0"])["ok"], 1)
            nfo = Path(directory) / "library/DSOD-060/movie.nfo"
            self.assertEqual(ET.fromstring(nfo.read_bytes()).findtext("title"), "Corrected title")
            self.assertEqual(cloud.renames, rename_calls)

    def test_output_directory_replacement_during_staging_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            outside = Path(directory) / "outside"
            outside.mkdir()
            folder = Path(directory) / "library/DSOD-060"
            def artwork(info, kind):
                if not folder.is_symlink():
                    folder.rmdir()
                    folder.symlink_to(outside, target_is_directory=True)
                return b"image"
            service.artwork = artwork
            self.assertEqual(service.run(["0"])["fail"], 1)
            self.assertEqual(cloud.renames, [])
            self.assertEqual(list(outside.iterdir()), [])

    def test_subtitle_failure_prevents_cloud_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            cloud.download_bytes = Mock(side_effect=RuntimeError("download failed"))
            self.assertEqual(service.run(["0"])["fail"], 1)
            self.assertEqual(cloud.renames, [])

    def test_partial_rename_resumes_after_restart_even_when_response_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            cloud.raise_after_rename = "1"
            self.assertEqual(service.run(["0"])["fail"], 1)
            service.close()
            service.registry.reopen()
            self.assertEqual(service.run(["0"])["ok"], 1)
            self.assertEqual([fid for fid, name in cloud.renames].count("1"), 1)
            self.assertEqual(cloud.entries["2"].name, "DSOD-060.zh.ass")

    def test_multipart_and_language_and_vobsub_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            cloud.entries = {e.id: e for e in [
                m.Entry("1", "0", "DSOD-060 original-CD1.mkv", pickcode="a" * 17),
                m.Entry("2", "0", "DSOD-060 original-CD2.mkv", pickcode="b" * 17),
                m.Entry("3", "0", "DSOD-060 original-CD1.zh-Hans.forced.ass", pickcode="c" * 17),
                m.Entry("4", "0", "DSOD-060 original-CD2.zh.idx", pickcode="d" * 17),
                m.Entry("5", "0", "DSOD-060 original-CD2.zh.sub", pickcode="e" * 17),
            ]}
            self.assertEqual(service.run(["0"])["ok"], 1)
            output = Path(directory) / "library" / "DSOD-060"
            for name in ("DSOD-060-CD1.strm", "DSOD-060-CD2.strm", "DSOD-060-CD1.zh-Hans.forced.ass", "DSOD-060-CD2.zh.idx", "DSOD-060-CD2.zh.sub"):
                self.assertTrue((output / name).is_file(), name)

    def test_conflicting_editions_are_not_renamed(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            cloud.entries["3"] = m.Entry("3", "0", "DSOD-060 another.mp4", pickcode="c" * 17)
            self.assertEqual(service.run(["0"])["fail"], 1)
            self.assertEqual(cloud.renames, [])

    def test_existing_local_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            output = Path(directory) / "library" / "DSOD-060"
            output.mkdir(parents=True)
            (output / "movie.nfo").write_text("user content")
            self.assertEqual(service.run(["0"])["fail"], 1)
            self.assertEqual((output / "movie.nfo").read_text(), "user content")
            self.assertEqual(cloud.renames, [])

    def test_selected_root_directory_name_can_identify_loose_video(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            cloud.entries = {e.id: e for e in [
                m.Entry("10", "0", "DSOD-060", is_dir=True),
                m.Entry("1", "10", "movie.mp4", pickcode="a" * 17),
                m.Entry("2", "10", "movie.zh.ass", pickcode="b" * 17),
            ]}
            self.assertEqual(service.run(["id:10"])["ok"], 1)

    def test_ambiguous_subtitle_with_multiple_videos_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            cloud.entries = {e.id: e for e in [
                m.Entry("1", "0", "DSOD-060-CD1.mkv", pickcode="a" * 17),
                m.Entry("2", "0", "DSOD-060-CD2.mkv", pickcode="b" * 17),
                m.Entry("3", "0", "Chinese.ass", pickcode="c" * 17),
            ]}
            self.assertEqual(service.run(["0"])["fail"], 1)
            self.assertEqual(cloud.renames, [])

    def test_expired_cache_and_concurrent_same_ua(self):
        import concurrent.futures
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            service.run(["0"])
            media_id = (Path(directory) / "library/DSOD-060/DSOD-060.strm").read_text().strip().split("media_id=")[1]
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                urls = list(pool.map(lambda _: service.playback_url(media_id, "Emby"), range(6)))
            self.assertEqual(len(set(urls)), 1)
            self.assertEqual(len(cloud.links), 1)
            service._cache[(cloud.account, "1", "Emby")] = (urls[0], 0)
            service.playback_url(media_id, "Emby")
            self.assertEqual(len(cloud.links), 2)

    def test_manually_modified_owned_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            service.run(["0"])
            nfo = Path(directory) / "library/DSOD-060/movie.nfo"
            nfo.write_text("manual edit")
            self.assertEqual(service.run(["0"])["fail"], 1)
            self.assertEqual(nfo.read_text(), "manual edit")

    def test_unknown_and_other_account_playback_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            service.run(["0"])
            strm = (Path(directory) / "library/DSOD-060/DSOD-060.strm").read_text()
            media_id = strm.strip().split("media_id=")[1]
            self.assertEqual(len(media_id), 64)
            self.assertTrue(service.playback_url(media_id, "Emby").startswith("https://cdn.115.com/"))
            service.playback_url(media_id, "Emby")
            self.assertEqual(cloud.links, [("1", "Emby")])
            service.playback_url(media_id, "OtherPlayer")
            self.assertEqual(cloud.links[-1], ("1", "OtherPlayer"))
            with self.assertRaises(KeyError):
                service.playback_url("unknown", "Emby")
            cloud.account = "another-account"
            with self.assertRaises(KeyError):
                service.playback_url(media_id, "Emby")

    def test_cookie_required_direct_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            service.run(["0"])
            media_id = (Path(directory) / "library/DSOD-060/DSOD-060.strm").read_text().strip().split("media_id=")[1]
            cloud.download_link = lambda *a, **k: m.DirectLink("https://cdn.115.com/v?f=3", {"Cookie": "secret"}, 4102444800)
            with self.assertRaises(m.CloudError):
                service.playback_url(media_id, "Emby")

    def test_valid_http_cdn_redirect_is_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            service.run(["0"])
            media_id = (Path(directory) / "library/DSOD-060/DSOD-060.strm").read_text().strip().split("media_id=")[1]
            cloud.download_link = lambda *a, **k: m.DirectLink("http://cdn.115.com/video?t=4102444800", {}, 4102444800)
            self.assertTrue(service.playback_url(media_id, "Emby").startswith("http://cdn.115.com/"))

    def test_stop_and_cloud_risk_stop_the_task(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            service.stop_event.set()
            self.assertEqual(service.run(["0"])["ok"], 0)
            self.assertEqual(cloud.renames, [])
            service.stop_event.clear()
            cloud.listdir = Mock(side_effect=m.RiskError("rate limited"))
            with self.assertRaises(m.RiskError):
                service.run(["0"])

    def test_risk_preserves_counts_from_already_finished_movies(self):
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = self.make(directory)
            cloud.entries["3"] = m.Entry("3", "0", "MIDE-999.mp4", pickcode="c" * 17)
            original = service.metadata
            def get_metadata(number):
                if number == "MIDE-999":
                    raise m.RiskError("verification required")
                return original(number)
            service.metadata = get_metadata
            with self.assertRaises(m.RiskError) as caught:
                service.run(["0"])
            self.assertEqual(caught.exception.stats["ok"], 1)


class AdapterTests(unittest.TestCase):
    def test_info_uses_full_entry_with_parent_and_directory_identity(self):
        m = components()
        sdk = types.SimpleNamespace(fs_file=Mock(side_effect=[
            {"state": True, "data": [{"cid": "7", "pid": "0", "n": "folder"}]},
            {"state": True, "data": [{"fid": "8", "cid": "7", "n": "film.mp4", "pc": "a" * 17}]},
        ]))
        client = m.Cloud115("UID=12345_A1; CID=fixture; SEID=fixture", sdk=sdk, interval=0)
        folder = client.info("7")
        self.assertTrue(folder.is_dir)
        self.assertEqual(folder.parent_id, "0")
        file = client.info("8")
        self.assertFalse(file.is_dir)
        self.assertEqual(file.parent_id, "7")

    def test_pagination_and_field_normalization(self):
        m = components()
        sdk = types.SimpleNamespace(fs_files=Mock(side_effect=[
            {"state": True, "count": 3, "path": [{"cid": "0"}], "data": [{"cid": "10", "pid": "0", "n": "folder"}, {"fid": "1", "cid": "0", "n": "film.mp4", "pc": "a" * 17, "s": "15"}]},
            {"state": True, "count": 3, "path": [{"cid": "0"}], "data": [{"fid": "2", "cid": "0", "n": "film.ass", "pc": "b" * 17, "s": "5"}]},
        ]))
        client = m.Cloud115("UID=12345_A1; CID=fixture; SEID=fixture", sdk=sdk, interval=0)
        entries = client.listdir("0")
        self.assertEqual([e.id for e in entries], ["10", "1", "2"])
        self.assertTrue(entries[0].is_dir)
        self.assertEqual(entries[1].size, 15)
        self.assertEqual(sdk.fs_files.call_args_list[1].args[0]["offset"], 2)

    def test_invalid_credentials_and_business_risk_are_bounded_errors(self):
        m = components()
        with self.assertRaises(m.AuthenticationError):
            m.Cloud115("", sdk=object(), interval=0)
        sdk = types.SimpleNamespace(fs_files=Mock(return_value={"state": False, "errno": 911, "error": "verification required"}))
        client = m.Cloud115("UID=12345_A1; CID=fixture; SEID=fixture", sdk=sdk, interval=0)
        with self.assertRaises(m.RiskError):
            client.listdir("0")

    def test_sdk_exception_and_frequency_response_stop_as_risk(self):
        m = components()
        class P115AuthenticationError(OSError):
            pass
        sdk = types.SimpleNamespace(download_url=Mock(side_effect=P115AuthenticationError(5, {"state": False, "errno": 911})),
                                    fs_files=Mock(return_value={"state": False, "errno": 590075}))
        client = m.Cloud115("UID=12345_A1; CID=fixture; SEID=fixture", sdk=sdk, interval=0)
        with self.assertRaises(m.RiskError):
            client.download_link(m.Entry("1", "0", "film.mp4", pickcode="a" * 17))
        with self.assertRaises(m.RiskError):
            client.listdir("0")

    def test_sdk_error_does_not_echo_credentials(self):
        m = components()
        sdk = types.SimpleNamespace(fs_files=Mock(side_effect=RuntimeError("Cookie=do-not-echo")))
        client = m.Cloud115("UID=12345_A1; CID=fixture; SEID=fixture", sdk=sdk, interval=0)
        with self.assertRaises(m.CloudError) as caught:
            client.listdir("0")
        self.assertNotIn("do-not-echo", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
