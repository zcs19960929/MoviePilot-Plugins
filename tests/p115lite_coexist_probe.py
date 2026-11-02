"""Called by the coexistence test in a fresh process; all outbound connections fail."""
import importlib
import importlib.metadata
import json
import socket
import sys
from pathlib import Path
from unittest.mock import Mock, patch


def run(order, directory):
    import app.sdk.config
    for name in ("CONFIG_PATH", "ROOT_PATH", "PLUGIN_DATA_PATH"):
        value = Path(directory) / name
        value.mkdir(parents=True, exist_ok=True)
        setattr(app.sdk.config.settings, name, value)
    assert importlib.metadata.version("p115client") == "0.0.9.6.5.1"
    assert importlib.metadata.version("python-concurrenttools") == "0.1.8"
    names = ["metatubescraper", "p115lite"] if order == "metatube-first" else ["p115lite", "metatubescraper"]
    loaded = {name: importlib.import_module(name) for name in names}
    meta = loaded["metatubescraper"].MetaTubeScraper()
    lite = loaded["p115lite"].P115Lite()
    from metatubescraper.cloud115 import Cloud115
    from p115lite.core.p115_client import create_client
    from p115lite.core.config import configer
    from p115lite.patch.app_ver import AppVerPatcher
    from p115lite.utils.user_agent import UserAgentUtils
    import p115client.client as sdk_module
    from p115client import P115Client
    configer.error_info_upload = False
    meta.init_plugin({"enabled": False, "storage": "115_direct", "metatube_url": "http://fixture.invalid", "metatube_token": "fixture-token"})
    cookie_a = "UID=12345_A1_1700000000; CID=fixture-A; SEID=fixture-A"
    cookie_b = "UID=67890_A1_1700000000; CID=fixture-B; SEID=fixture-B"
    calls = []

    def request(client, url, method="GET", **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/files"):
            return {"state": True, "count": 1, "path": [{"cid": "0"}], "data": [
                {"fid": "1", "cid": "0", "n": "DSOD-060.mp4", "pc": "a" * 17, "s": 10}]}
        if "/ufile/download" in url:
            return {"state": True, "data": {"url": "https://cdn.115.com/fixture?t=4102444800"}, "headers": {}}
        raise AssertionError("unexpected SDK endpoint: " + url)

    with patch.object(P115Client, "request", request):
        cloud = Cloud115(cookie_a, interval=0)
        other = create_client(cookie_b, default_timeout={"connect": 1, "read": 1})
        original_cookies = str(cloud.sdk.cookies_str)
        original_version = sdk_module._app_version
        for phase in ("before", "patched", "stopped"):
            if phase == "patched":
                with patch.object(UserAgentUtils, "get_real_app_ver", return_value="38.0.2"):
                    AppVerPatcher.enable()
                assert AppVerPatcher._active
            elif phase == "stopped":
                lite.stop_service()
                assert not AppVerPatcher._active
                assert sdk_module._app_version == original_version
            files = cloud.listdir("0")
            assert files[0].name == "DSOD-060.mp4"
            link = cloud.download_link(files[0], "Jellyfin-fixture")
            assert link.url == "https://cdn.115.com/fixture?t=4102444800"
            assert cloud.account == "12345"
            assert str(cloud.sdk.cookies_str) == original_cookies
            assert "67890_A1" in str(other.cookies_str)
            assert "12345_A1" not in str(other.cookies_str)
            response = Mock(status_code=200)
            response.json.return_value = {"data": []}
            with patch("requests.get", return_value=response) as get:
                assert meta._MetaTubeScraper__mt_get("/v1/movies/search", {"q": "DSOD-060"}) == {"data": []}
                assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer fixture-token"
        assert all(kwargs["headers"].get("user-agent") == "Jellyfin-fixture" for url, kwargs in calls if "/ufile/download" in url)
    print(json.dumps({"order": order, "sdk_and_credentials_isolated": True, "network": "blocked"}))


if __name__ == "__main__":
    with patch.object(socket.socket, "connect", side_effect=AssertionError("outbound network forbidden")):
        run(sys.argv[1], sys.argv[2])
