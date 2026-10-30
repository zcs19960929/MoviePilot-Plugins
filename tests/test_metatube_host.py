import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_metatube_auth import load_plugin
import test_metatube_direct as fixtures


class HostTests(unittest.TestCase):
    def test_configuration_reload_before_client_creation_cancels_the_original_job(self):
        for version in ('v2','v3'):
            plugin=load_plugin(version,Mock())
            config=dict(enabled=True,storage='115_direct',cookie115='account-A',metatube_url='http://backend',
                        scan_dirs='id:0',local_strm_root='/tmp/output',playback_base_url='https://mp.example')
            plugin.init_plugin(config)
            records=[];plugin.save_data=lambda key,value:records.append(value)
            def reload_after_start(message):
                if '刮削开始' in message:plugin.init_plugin(dict(config,cookie115='account-B'))
            plugin.info=reload_after_start
            plugin._MetaTubeScraper__scrape_all()
            self.assertEqual(records[-1]['fail'],0)
            self.assertTrue(records[-1]['stopped'])

    def test_saving_ui_or_schedule_settings_does_not_cancel_an_active_scan(self):
        for version in ('v2', 'v3'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                fixture = fixtures.DirectAcceptanceTests()
                fixture.addCleanup = self.addCleanup
                m, cloud, metadata, service = fixture.make(directory, version)
                plugin = load_plugin(version, Mock())
                _, config = plugin.get_form()
                config.update(enabled=True, storage='115_direct', cookie115='fixture',
                              metatube_url='http://backend', scan_dirs='id:0',
                              local_strm_root=str(Path(directory)/'library'), playback_base_url='https://mp.example')
                plugin.init_plugin(config)
                cloud.stop_event = service.stop_event = plugin._stop_event
                plugin._cloud115, plugin._direct_service = cloud, service
                entered, release = threading.Event(), threading.Event()
                original_listdir = cloud.listdir
                def waiting_listdir(cid):
                    entered.set()
                    if not release.wait(3): raise RuntimeError('test did not release scan')
                    if cloud.stop_event.is_set(): raise m.Stopped('任务已停止')
                    return original_listdir(cid)
                cloud.listdir = waiting_listdir
                records = []
                plugin.save_data = lambda key, value: records.append(value)
                worker = threading.Thread(target=plugin._MetaTubeScraper__scrape_all)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    plugin.init_plugin(dict(config, run_msg='运行中', cookie115_msg='Cookie 可用',
                                            storage={'value':'115_direct'}, cron_hour='4',
                                            schedule_mode='interval', schedule_interval=90))
                    self.assertFalse(cloud.stop_event.is_set())
                finally:
                    release.set(); worker.join(4)
                self.assertFalse(worker.is_alive())
                self.assertEqual(records[-1]['ok'], 1)
                self.assertEqual(records[-1]['fail'], 0)
                self.assertTrue((Path(directory)/'library/DSOD-060/movie.nfo').is_file())
                self.assertEqual(plugin.get_service()[0]['kwargs'], {'minutes':90})

    def test_real_configuration_change_stops_scan_without_reporting_scrape_failure(self):
        for version in ('v2', 'v3'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                fixture = fixtures.DirectAcceptanceTests(); fixture.addCleanup = self.addCleanup
                m, cloud, metadata, service = fixture.make(directory, version)
                plugin = load_plugin(version, Mock())
                config=dict(enabled=True, storage='115_direct', cookie115='account-A', metatube_url='http://backend',
                            scan_dirs='id:0', local_strm_root=str(Path(directory)/'library'), playback_base_url='https://mp.example')
                plugin.init_plugin(config)
                cloud.stop_event = service.stop_event = plugin._stop_event
                plugin._cloud115, plugin._direct_service = cloud, service
                entered, release = threading.Event(), threading.Event()
                def waiting_listdir(cid):
                    entered.set()
                    if not release.wait(3): raise RuntimeError('test did not release scan')
                    if cloud.stop_event.is_set(): raise m.Stopped('任务已停止')
                    return []
                cloud.listdir = waiting_listdir
                records=[]; plugin.save_data=lambda key,value:records.append(value)
                worker=threading.Thread(target=plugin._MetaTubeScraper__scrape_all);worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    plugin.init_plugin(dict(config,cookie115='account-B'))
                    self.assertTrue(cloud.stop_event.is_set())
                finally:
                    release.set();worker.join(4)
                self.assertEqual(records[-1]['fail'],0)
                self.assertTrue(records[-1]['stopped'])
                self.assertFalse((Path(directory)/'library/DSOD-060/movie.nfo').exists())
                plugin.get_data=lambda key:records[-1]
                page=plugin.get_page()
                self.assertIn('已停止',str(page))
                self.assertEqual(page[-1]['props']['type'],'warning')

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
        self.assertEqual(reply['display_path'],'/')
        self.assertEqual(reply['data'][0]['display_path'],'/电影')
        self.assertEqual(reply["parent"], "id:0")
        self.assertEqual(routes["/115/check"]["endpoint"]()["code"], 0)

    def test_browse_adds_readable_nested_paths_without_changing_stable_ids(self):
        for version in ('v2','v3'):
            plugin=load_plugin(version,Mock());plugin.init_plugin({'storage':'115_direct'})
            plugin._cloud115=types.SimpleNamespace(
                resolve_folder=lambda path:'7',
                info=lambda cid:types.SimpleNamespace(name='中文合集',parent_id='6'),
                listdir=lambda cid:[types.SimpleNamespace(name='演员 甲',id='8',is_dir=True)],
                directory_path=lambda cid:'/电影/中文合集',
            )
            route=next(api for api in plugin.get_api() if api['path']=='/115/browse')
            reply=route['endpoint'](path='id:7')
            self.assertEqual(reply['path'],'id:7')
            self.assertEqual(reply['parent'],'id:6')
            self.assertEqual(reply['display_path'],'/电影/中文合集')
            self.assertEqual(reply['data'][0]['path'],'id:8')
            self.assertEqual(reply['data'][0]['display_path'],'/电影/中文合集/演员 甲')

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
            plugin.init_plugin({"enabled": True, "storage": "115_direct", "metatube_url": "http://backend", "cookie115": "fixture", "scan_dirs": "id:0", "local_strm_root": str(Path(directory) / "library"), "playback_base_url": "https://mp.example", "trigger_strm": True, "write_remote": True, "organize": True})
            plugin._direct_service = service
            plugin.save_data = Mock()
            events = []
            with patch.dict(plugin._MetaTubeScraper__trigger_strm.__func__.__globals__,
                            eventmanager=types.SimpleNamespace(send_event=lambda *args: events.append(args)),
                            EventType=types.SimpleNamespace(PluginAction="plugin-action")):
                plugin._MetaTubeScraper__scrape_all()
            self.assertEqual(plugin.save_data.call_args.args[1]["ok"], 1)
            self.assertTrue((Path(directory) / "library/DSOD-060/DSOD-060.strm").is_file())
            self.assertEqual(events, [])

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
