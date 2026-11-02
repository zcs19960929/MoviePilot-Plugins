"""Importer behavior, using real archives and temporary repositories without network."""
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def importer():
    path = ROOT / "scripts/sync_p115lite.py"
    if not path.exists():
        raise AssertionError("verified 115Lite importer is not implemented")
    spec = importlib.util.spec_from_file_location("p115lite_sync", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fixture(module, version="3.0.3", sha="a" * 40):
    requirements = b"p115client==0.0.9.6.5.1\npython-concurrenttools==0.1.8\n"
    files = {
        "plugins.v3/p115lite/__init__.py": (
            "class P115Lite:\n plugin_name='115Lite'\n plugin_version=" + repr(version)
            + "\n plugin_config_prefix='p115lite_'\n"
        ).encode(),
        "plugins.v3/p115lite/version.py": ("VERSION=" + repr(version) + "\n").encode(),
        "plugins.v3/p115lite/requirements.txt": requirements,
        "plugins.v3/p115lite/pyproject.toml": b'[project]\nrequires-python=">=3.14,<3.15"\ndependencies=["p115client==0.0.9.6.5.1","python-concurrenttools==0.1.8"]\n',
        "plugins.v3/p115lite/dist/assets/remoteEntry.js": b"fixture frontend",
        "plugins.v3/p115lite/tests/_stub_host/app/__init__.py": b"never in release",
        "frontend/p115lite/src/App.vue": b"<template>fixture</template>",
        "frontend/p115lite/package-lock.json": b"{}",
    }
    runtime = {k.removeprefix("plugins.v3/p115lite/"): v for k, v in files.items()
               if k.startswith("plugins.v3/p115lite/") and "/tests/" not in k}
    for name, wheel_version in (("full_strm_sync", "0.1.6"), ("txt_tree_storage", "0.2.1"), ("share_strm_scan", "0.2.2")):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as wheel:
            wheel.writestr(f"{name}-{wheel_version}.dist-info/METADATA", f"Name: {name}\nVersion: {wheel_version}\n")
        runtime[f"wheels/{name}-{wheel_version}-cp314-cp314-manylinux_2_17_x86_64.whl"] = data.getvalue()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in runtime.items():
            archive.writestr(name, content)
    return module.Snapshot(sha=sha, version=version, files=files,
                           entry={"name": "115Lite", "version": version, "author": "madrays", "release": True, "system_version": ">=3.0.0"},
                           archive=output.getvalue(), license=b"upstream license", notice=b"upstream authors")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.m = importer()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "plugins.v3/metatubescraper").mkdir(parents=True)
        (self.root / "plugins.v3/metatubescraper/requirements.txt").write_text(
            'p115client==0.0.9.6.5.1; python_version >= "3.12"\npython-concurrenttools==0.1.8; python_version >= "3.12"\n')
        self.manifest = {"MetaTubeScraper": {"version": "1.1.5", "history": {"v1.1.5": "keep"}}}
        (self.root / "package.v3.json").write_text(json.dumps(self.manifest))
        (self.root / "package.v2.json").write_bytes(b"preserve V2 exactly\n")
        self.snapshot = fixture(self.m)

    def inventory(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_dry_run_does_not_write_even_the_provenance_or_manifest(self):
        before = self.inventory()
        plan = self.m.prepare(self.root, self.snapshot)
        self.assertTrue(plan.changed)
        self.assertEqual(self.inventory(), before)

    def test_apply_preserves_metatube_v2_and_other_manifest_entries(self):
        before = self.inventory()
        self.m.apply(self.root, self.m.prepare(self.root, self.snapshot))
        self.assertEqual(json.loads((self.root / "package.v3.json").read_text())["MetaTubeScraper"], self.manifest["MetaTubeScraper"])
        for path in ("plugins.v3/metatubescraper/requirements.txt", "package.v2.json"):
            self.assertEqual((self.root / path).read_bytes(), before[path])
        self.assertEqual((self.root / "frontend/p115lite/src/App.vue").read_bytes(), self.snapshot.files["frontend/p115lite/src/App.vue"])
        self.assertTrue((self.root / "plugins.v3/p115lite/NOTICE").exists())
        self.assertTrue((self.root / "upstream/p115lite.json").exists())

    def test_no_change_and_unrelated_upstream_commit_do_not_change_provenance(self):
        self.m.apply(self.root, self.m.prepare(self.root, self.snapshot))
        before = self.inventory()
        self.snapshot.sha = "b" * 40
        plan = self.m.prepare(self.root, self.snapshot)
        self.assertFalse(plan.changed)
        self.m.apply(self.root, plan)
        self.assertEqual(self.inventory(), before)

    def test_core_dependency_drift_rejects_before_any_write(self):
        self.snapshot.files["plugins.v3/p115lite/requirements.txt"] = b"p115client==0.0.9.7.2\npython-concurrenttools==0.1.8\n"
        before = self.inventory()
        with self.assertRaisesRegex(self.m.SyncError, "p115client"):
            self.m.prepare(self.root, self.snapshot)
        self.assertEqual(self.inventory(), before)

    def test_python_requirement_change_is_not_silently_accepted(self):
        self.snapshot.files["plugins.v3/p115lite/pyproject.toml"] = b'[project]\nrequires-python=">=3.15"\n'
        with self.assertRaisesRegex(self.m.SyncError, "Python"):
            self.m.prepare(self.root, self.snapshot)

    def test_missing_wheel_and_mismatched_release_source_are_rejected(self):
        for omitted in ("wheels/", "dist/"):
            with self.subTest(omitted=omitted):
                snapshot = fixture(self.m)
                data = io.BytesIO()
                with zipfile.ZipFile(io.BytesIO(snapshot.archive)) as old, zipfile.ZipFile(data, "w") as new:
                    for name in old.namelist():
                        if not name.startswith(omitted):
                            new.writestr(name, old.read(name))
                snapshot.archive = data.getvalue()
                with self.assertRaises(self.m.SyncError):
                    self.m.prepare(self.root, snapshot)

    def test_archive_traversal_is_rejected(self):
        data = io.BytesIO(self.snapshot.archive)
        with zipfile.ZipFile(data, "a") as archive:
            archive.writestr("../escape.py", b"bad")
        self.snapshot.archive = data.getvalue()
        with self.assertRaises(self.m.SyncError):
            self.m.prepare(self.root, self.snapshot)

    def test_local_mirror_edits_are_not_overwritten(self):
        self.m.apply(self.root, self.m.prepare(self.root, self.snapshot))
        edited = self.root / "plugins.v3/p115lite/__init__.py"
        edited.write_bytes(b"local edit")
        with self.assertRaisesRegex(self.m.SyncError, "local|本地"):
            self.m.prepare(self.root, fixture(self.m, "3.0.4", "b" * 40))
        self.assertEqual(edited.read_bytes(), b"local edit")

    def test_same_version_changed_archive_cannot_replace_published_version(self):
        self.m.apply(self.root, self.m.prepare(self.root, self.snapshot))
        changed = fixture(self.m, sha="b" * 40)
        changed.notice = b"changed author notice"
        with self.assertRaisesRegex(self.m.SyncError, "version|版本"):
            self.m.prepare(self.root, changed)

    def test_build_is_reproducible_and_excludes_test_host(self):
        self.m.apply(self.root, self.m.prepare(self.root, self.snapshot))
        first = self.m.build_archive(self.root)
        self.assertEqual(first, self.m.build_archive(self.root))
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            names = archive.namelist()
            self.assertIn("__init__.py", names)
            self.assertIn("dist/assets/remoteEntry.js", names)
            self.assertIn("LICENSE", names)
            self.assertIn("NOTICE", names)
            self.assertEqual(len([n for n in names if n.endswith(".whl")]), 3)
            self.assertFalse(any("_stub_host" in n or n.startswith("tests/") for n in names))
            self.assertIn("madrays", archive.read("UPSTREAM.json").decode())

    def test_verify_rejects_a_new_metatube_sdk_lock_before_building(self):
        self.m.apply(self.root, self.m.prepare(self.root, self.snapshot))
        (self.root / "plugins.v3/metatubescraper/requirements.txt").write_text("p115client==0.0.9.7.2\n")
        with self.assertRaisesRegex(self.m.SyncError, "p115client"):
            self.m.build_archive(self.root)

    def test_archive_build_rejects_provenance_traversal(self):
        self.m.apply(self.root, self.m.prepare(self.root, self.snapshot))
        record = json.loads((self.root / "upstream/p115lite.json").read_text())
        record["runtime_paths"].append("../../package.v2.json")
        (self.root / "upstream/p115lite.json").write_text(json.dumps(record))
        with self.assertRaises(self.m.SyncError):
            self.m.build_archive(self.root)


if __name__ == "__main__":
    unittest.main()
