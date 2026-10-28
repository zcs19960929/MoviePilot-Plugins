"""Run with: python3 -m unittest discover -s tests -v."""

import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ("v2", "v3")
PROVIDERS = ("FANZA", "JavBus", "JAV321", "MGS", "DUGA", "FC2PPVDB", "fc2hub")
TOKEN = "test-metatube-token"


def load_plugin(version, get):
    """Load the complete plugin with only host and HTTP dependencies replaced."""
    modules = {name: types.ModuleType(name) for name in (
        "app", "app.plugins", "app.sdk", "app.sdk.logging", "requests",
    )}
    modules["app.plugins"]._PluginBase = object
    modules["app.sdk.logging"].logger = logging.getLogger(__name__)
    modules["requests"].get = get
    path = ROOT / f"plugins.{version}" / "metatubescraper" / "__init__.py"
    spec = importlib.util.spec_from_file_location(f"metatubescraper_{version}", path)
    module = importlib.util.module_from_spec(spec)
    modules[spec.name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module.MetaTubeScraper()


def response(status, payload):
    return types.SimpleNamespace(status_code=status, json=lambda: payload)


def configure(plugin, token=TOKEN):
    plugin.init_plugin({
        "metatube_url": "http://metatube.invalid:8080/",
        "metatube_token": token,
        "providers": ",".join(PROVIDERS),
    })


class MetaTubeAuthenticationTests(unittest.TestCase):
    def test_selftest_authenticates_every_provider_with_saved_token(self):
        def authenticated_search(url, **kwargs):
            if kwargs.get("headers", {}).get("Authorization") != f"Bearer {TOKEN}":
                return response(401, {"error": {"message": "Unauthorized"}})
            return response(200, {"data": [{"title": "DSOD-060"}]})

        for version in VERSIONS:
            with self.subTest(version=version):
                get = Mock(side_effect=authenticated_search)
                plugin = load_plugin(version, get)
                configure(plugin, f"  {TOKEN}\n")
                result = plugin._MetaTubeScraper__api_selftest("DSOD-060")
                expected = " | ".join(f"{p}: 命中「DSOD-060」" for p in PROVIDERS)
                self.assertEqual(result, {"code": 0, "msg": f"DSOD-060 → {expected}"})
                self.assertEqual(get.call_count, len(PROVIDERS))
                for call, provider in zip(get.call_args_list, PROVIDERS):
                    self.assertEqual(call.args[0], "http://metatube.invalid:8080/v1/movies/search")
                    self.assertEqual(call.kwargs["params"], {
                        "q": "DSOD-060", "provider": provider, "fallback": "true",
                    })

    def test_selftest_and_normal_requests_use_consistent_headers(self):
        for version in VERSIONS:
            for token in (TOKEN, ""):
                with self.subTest(version=version, token_set=bool(token)):
                    reply = response(200, {"data": []})
                    get = Mock(return_value=reply)
                    plugin = load_plugin(version, get)
                    configure(plugin, token)
                    self.assertEqual(plugin._MetaTubeScraper__mt_get(
                        "/v1/movies/search", {"q": "DSOD-060"}), {"data": []})
                    self.assertIs(plugin._MetaTubeScraper__mt_get(
                        "/v1/images/primary/FANZA/example", stream=True), reply)
                    plugin._MetaTubeScraper__api_selftest("DSOD-060")
                    expected = {"User-Agent": "MoviePilot-MetaTubeScraper/1.1"}
                    if token:
                        expected["Authorization"] = f"Bearer {token}"
                    for call in get.call_args_list:
                        self.assertEqual(call.kwargs.get("headers", {}), expected)

    def test_selftest_preserves_http_results_and_error_details(self):
        cases = (
            (200, {"data": [{"title": "Example"}]}, "命中「Example」"),
            (200, {"data": []}, "空结果"),
            (401, {"error": {"message": "Unauthorized"}}, "HTTP 401 Unauthorized"),
            (404, {"error": {"message": "Not Found"}}, "HTTP 404 Not Found"),
            (500, {"error": {"message": "missing __next_f"}}, "HTTP 500 missing __next_f"),
        )
        for version in VERSIONS:
            for status, payload, message in cases:
                with self.subTest(version=version, status=status, message=message):
                    plugin = load_plugin(version, Mock(return_value=response(status, payload)))
                    configure(plugin)
                    plugin._providers = "FANZA"
                    result = plugin._MetaTubeScraper__api_selftest("DSOD-060")
                    self.assertEqual(result, {"code": 0, "msg": f"DSOD-060 → FANZA: {message}"})


if __name__ == "__main__":
    unittest.main()
