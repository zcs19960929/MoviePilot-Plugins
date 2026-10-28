import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_metatube_auth import load_plugin
import test_metatube_direct as fixtures


class HostTests(unittest.TestCase):
    def test_account_reload_serializes_with_client_creation_and_stops_old_context(self):
        plugin = load_plugin("v3", Mock())
        plugin.init_plugin({"cookie115": "account-A", "storage": "115_direct"})
        entered, release, reloaded = threading.Event(), threading.Event(), threading.Event()
        clients = []
        def factory(cookie, interval, stop_event):
            client = types.SimpleNamespace(account=cookie, stop_event=stop_event)
            clients.append(client)
            if cookie == "account-A":
                entered.set()
                release.wait(3)
            return client
        def reload_config():
            plugin.init_plugin({"cookie115": "account-B", "storage": "115_direct"})
            reloaded.set()
        getter = plugin._MetaTubeScraper__get_cloud115
        with patch.dict(getter.__func__.__globals__, {"Cloud115": factory}):
            creator = threading.Thread(target=getter)
            creator.start()
            self.assertTrue(entered.wait(2))
            changer = threading.Thread(target=reload_config)
            changer.start()
            try:
                self.assertFalse(reloaded.wait(.1), "reload must wait for in-flight client construction")
            finally:
                release.set()
                creator.join(3)
                changer.join(3)
            self.assertTrue(clients[0].stop_event.is_set())
            self.assertEqual(getter().account, "account-B")
    def test_new_install_default_and_old_mode_are_preserved(self):
        for version in ("v2", "v3"):
            with self.subTest(version=version):
                plugin = load_plugin(version, Mock())
                form, defaults = plugin.get_form()
                self.assertEqual(defaults["storage"], "115_direct")
                self.assertEqual(defaults["cloud_interval"], 5)
                plugin.init_plugin({"enabled": True, "storage": "local", "scan_dirs": "/movies", "metatube_url": "http://backend"})
                self.assertEqual(plugin._storage, "local")
                self.assertTrue(plugin._config_ok)
                fields = []
                def visit(items):
                    for item in items:
                        if item.get("props", {}).get("model") == "cookie115":
                            fields.append(item["props"])
                        visit(item.get("content", []))
                visit(form)
                self.assertEqual(fields[0]["type"], "password")

    def test_direct_mode_configuration_and_routes(self):
        for version in ("v2", "v3"):
            with self.subTest(version=version):
                plugin = load_plugin(version, Mock())
                config = {"enabled": True, "storage": "115_direct", "metatube_url": "http://backend", "scan_dirs": "id:0"}
                plugin.init_plugin(config)
                self.assertFalse(plugin._config_ok)
                plugin.init_plugin(dict(config, cookie115="fixture", local_strm_root="/tmp/output", playback_base_url="http://mp"))
                self.assertTrue(plugin._config_ok)
                routes = {api["path"]: api for api in plugin.get_api()}
                for path in ("/115/check", "/115/browse"):
                    self.assertEqual(routes[path]["auth"], "bear")
                    self.assertFalse(routes[path].get("allow_anonymous", False))
                play = routes["/redirect_url"]
                self.assertTrue(play["allow_anonymous"])
                self.assertEqual(set(play["methods"]), {"GET", "HEAD"})

    def test_browse_uses_own_client_and_keeps_folder_ids(self):
        plugin = load_plugin("v3", Mock())
        plugin.init_plugin({"storage": "115_direct"})
        plugin._cloud115 = types.SimpleNamespace(
            account="123", resolve_folder=lambda value: "0", check=lambda: "123",
            listdir=lambda cid: [types.SimpleNamespace(id="7", parent_id="0", name="电影", is_dir=True)],
        )
        routes = {api["path"]: api for api in plugin.get_api()}
        reply = routes["/115/browse"]["endpoint"](path="/")
        self.assertEqual(reply["data"][0]["path"], "id:7")
        self.assertEqual(reply["parent"], "id:0")
        self.assertEqual(routes["/115/check"]["endpoint"]()["code"], 0)

    def test_direct_initialization_error_is_counted_and_saved(self):
        plugin = load_plugin("v3", Mock())
        plugin.init_plugin({"storage": "115_direct"})
        error = plugin._MetaTubeScraper__get_cloud115.__func__.__globals__["CloudError"]
        plugin._direct_service = types.SimpleNamespace(run=Mock(side_effect=error("SDK unavailable")))
        plugin.save_data = Mock()
        plugin._MetaTubeScraper__scrape_all()
        self.assertEqual(plugin.save_data.call_args.args[1]["fail"], 1)
        self.assertIn("SDK unavailable", plugin.save_data.call_args.args[1]["errors"][0])

    def test_actual_host_run_calls_direct_pipeline_without_115lite(self):
        fixture = fixtures.DirectAcceptanceTests()
        fixture.addCleanup = self.addCleanup
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, service = fixture.make(directory)
            plugin = load_plugin("v3", Mock())
            plugin.init_plugin({"enabled": True, "storage": "115_direct", "metatube_url": "http://backend", "cookie115": "fixture", "scan_dirs": "id:0", "local_strm_root": str(Path(directory) / "library"), "playback_base_url": "https://mp.example"})
            plugin._direct_service = service
            plugin.save_data = Mock()
            plugin._MetaTubeScraper__scrape_all()
            self.assertEqual(plugin.save_data.call_args.args[1]["ok"], 1)
            self.assertTrue((Path(directory) / "library/DSOD-060/DSOD-060.strm").is_file())

    def test_host_factory_uses_real_backend_callbacks_and_independent_auth(self):
        fixture = fixtures.DirectAcceptanceTests()
        fixture.addCleanup = self.addCleanup
        with tempfile.TemporaryDirectory() as directory:
            m, cloud, metadata, unused = fixture.make(directory)
            calls = []
            def get(url, **kwargs):
                calls.append((url, kwargs))
                reply = types.SimpleNamespace(status_code=200)
                if "/images/" in url:
                    reply.headers = {"content-type": "image/jpeg"}
                    reply.content = b"image bytes"
                    reply.close = Mock()
                elif url.endswith("/search"):
                    reply.json = lambda: {"data": [{"provider": "FANZA", "id": "film"}]}
                else:
                    reply.json = lambda: {"data": {"provider": "FANZA", "id": "film", "title": "Backend title"}}
                return reply
            plugin = load_plugin("v3", get)
            plugin.init_plugin({"enabled": True, "storage": "115_direct", "cookie115": "cloud-cookie-fixture",
                                "metatube_url": "https://metadata.example", "metatube_token": "backend-test",
                                "scan_dirs": "id:0", "local_strm_root": str(Path(directory) / "library"),
                                "playback_base_url": "https://mp.example"})
            plugin._cloud115 = cloud
            plugin.get_data_path = lambda: Path(directory) / "host-state"
            plugin.save_data = Mock()
            self.addCleanup(lambda: plugin._direct_service.close() if plugin._direct_service else None)
            plugin._MetaTubeScraper__scrape_all()
            self.assertEqual(plugin.save_data.call_args.args[1]["ok"], 1)
            nfo = (Path(directory) / "library/DSOD-060/movie.nfo").read_text()
            self.assertIn("Backend title", nfo)
            self.assertNotIn("backend-test", nfo)
            self.assertNotIn("cloud-cookie-fixture", nfo)
            self.assertTrue(calls)
            for url, kwargs in calls:
                self.assertTrue(url.startswith("https://metadata.example/"))
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer backend-test")
                self.assertNotIn("Cookie", kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
