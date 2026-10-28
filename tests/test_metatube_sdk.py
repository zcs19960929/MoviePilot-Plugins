"""Pinned SDK and real HTTP routing tests; run in the documented isolated venv."""
import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_metatube_auth import load_plugin
import test_metatube_direct as fixtures


@unittest.skipUnless(importlib.util.find_spec("p115client"), "pinned SDK is not installed in this interpreter")
class PinnedSDKTests(unittest.TestCase):
    def test_real_sdk_cookie_list_info_rename_link_and_subtitle_download(self):
        import inspect
        original = Path.expanduser
        with tempfile.TemporaryDirectory() as directory:
            # Keep the SDK's import-time cache in this isolated test directory.
            def expand(path):
                return Path(directory) / "sdk-cache" if str(path) == "~/.p115client.cache.d" else original(path)
            with patch.object(Path, "expanduser", expand):
                from p115client import P115Client
                m = fixtures.components()
                file = {"file_id": "1", "category_id": "0", "file_name": "original.ass",
                        "file_size": 10, "file_category": "1", "pick_code": "a" * 17}
                calls = []
                risk = [False]
                def request(sdk, url, method="GET", **kwargs):
                    calls.append((url, method, kwargs))
                    if url.endswith("/files/get_info"):
                        return {"state": True, "data": [{"fid": "1", "cid": "0", "n": file["file_name"], "pc": "a" * 17, "s": 10}]}
                    if url.endswith("/files/batch_rename"):
                        file["file_name"] = kwargs["data"]["files_new_name[1]"]
                        return {"state": True}
                    if url.endswith("/files"):
                        return {"state": True, "count": 1, "path": [{"cid": "0"}], "data": [
                            {"fid": "1", "cid": "0", "n": file["file_name"], "pc": "a" * 17, "s": 10}]}
                    if "/ufile/download" in url:
                        if risk[0]:
                            return {"state": False, "errno": 911}
                        return {"state": True, "data": {"url": "https://cdn.115.com/subtitle?t=4102444800"},
                                "headers": {"User-Agent": kwargs.get("headers", {}).get("user-agent", "")}}
                    raise AssertionError("unexpected SDK endpoint: " + url)
                with patch.object(P115Client, "request", request):
                    client = m.Cloud115("UID=12345_A1; CID=fixture; SEID=fixture", interval=0)
                    self.assertEqual(client.listdir("0")[0].id, "1")
                    self.assertEqual(client.info("1").name, "original.ass")
                    client.rename("1", "DSOD-060.zh.ass")
                    self.assertEqual(client.info("1").name, "DSOD-060.zh.ass")
                    link = client.download_link(client.info("1"), "Emby")
                    self.assertEqual(link.url, "https://cdn.115.com/subtitle?t=4102444800")
                    response = Mock(status_code=200)
                    response.__enter__ = Mock(return_value=response)
                    response.__exit__ = Mock(return_value=False)
                    response.iter_content.return_value = [b"subtitle bytes"]
                    with patch("requests.get", return_value=response):
                        self.assertEqual(client.download_bytes(client.info("1")), b"subtitle bytes")
                    self.assertTrue(all(url.startswith("https://") for url, _, _ in calls))
                    self.assertFalse(any("login" in url or "delete" in url or "copy" in url for url, _, _ in calls))
                    risk[0] = True
                    with self.assertRaises(m.RiskError):
                        client.download_link(client.info("1"), "Emby")


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI is provided by the host / isolated test venv")
class PlaybackHTTPTests(unittest.TestCase):
    def test_get_head_unknown_id_and_disabled_scraping(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        fixture = fixtures.DirectAcceptanceTests()
        fixture.addCleanup = self.addCleanup
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = fixture.make(directory)
            service.run(["0"])
            media_id = (Path(directory) / "library/DSOD-060/DSOD-060.strm").read_text().strip().split("media_id=")[1]
            plugin = load_plugin("v3", Mock())
            plugin.init_plugin({"storage": "115_direct", "enabled": False})
            plugin._direct_service = service
            route = next(api for api in plugin.get_api() if api["path"] == "/redirect_url")
            app = FastAPI()
            app.add_api_route("/redirect_url", route["endpoint"], methods=route["methods"])
            with TestClient(app) as http:
                for method in ("GET", "HEAD"):
                    response = http.request(method, "/redirect_url", params={"media_id": media_id},
                                            headers={"User-Agent": "Emby"}, follow_redirects=False)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers["location"], "https://cdn.115.com/video?t=4102444800")
                    if method == "HEAD":
                        self.assertEqual(response.content, b"")
                self.assertEqual(http.get("/redirect_url", params={"media_id": "0" * 64}).status_code, 404)
                self.assertEqual(http.get("/redirect_url", params={"media_id": "bad"}).status_code, 404)
                self.assertEqual(cloud.links, [("1", "Emby")])


if __name__ == "__main__":
    unittest.main()
