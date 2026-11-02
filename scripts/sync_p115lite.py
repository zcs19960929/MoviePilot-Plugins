#!/usr/bin/env python3
"""Verified, scoped mirror of 115Lite. Python 3.11+; default is a read-only dry run."""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

UPSTREAM = "madrays/MoviePilot-Plugins"
PLUGIN = "plugins.v3/p115lite/"
FRONTEND = "frontend/p115lite/"
RECORD = "upstream/p115lite.json"
PINS = {"p115client": "0.0.9.6.5.1", "python-concurrenttools": "0.1.8"}
WHEELS = {"full_strm_sync", "txt_tree_storage", "share_strm_scan"}
ROOT = Path(__file__).resolve().parents[1]


class SyncError(Exception):
    """Unsafe or incompatible synchronization; no mirror writes are permitted."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def safe_path(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise SyncError(f"Unsafe archive path: {name}")
    return path.as_posix()


def read_zip(data):
    result = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(x.file_size for x in archive.infolist()) > 100 * 1024 * 1024:
            raise SyncError("Uncompressed archive exceeds size limit")
        for item in archive.infolist():
            name = safe_path(item.filename)
            if item.is_dir():
                continue
            if stat.S_ISLNK(item.external_attr >> 16) or name in result:
                raise SyncError(f"Symlink or duplicate archive path: {name}")
            result[name] = archive.read(item)
    return result


def pins(text):
    found = {}
    for line in text.splitlines():
        requirement = line.split("#", 1)[0].split(";", 1)[0].strip()
        name = re.split(r"[=<>!~\[ ]", requirement, maxsplit=1)[0].lower().replace("_", "-")
        if name in PINS:
            if requirement != f"{name}=={PINS[name]}" or name in found:
                raise SyncError(f"Incompatible dependency: {name}; expected =={PINS[name]}")
            found[name] = PINS[name]
    if found != PINS:
        raise SyncError("Missing locked dependency: " + ", ".join(PINS.keys() - found.keys()))


@dataclass
class Snapshot:
    sha: str
    version: str
    files: dict[str, bytes]
    entry: dict
    archive: bytes
    license: bytes
    notice: bytes


@dataclass
class Plan:
    version: str
    files: dict[str, bytes]
    remove: list[str]
    changed: bool


def check_identity(files, entry, version):
    tree = ast.parse(files[PLUGIN + "__init__.py"].decode())
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "P115Lite"), None)
    if cls is None:
        raise SyncError("P115Lite class is missing")
    attrs = {n.targets[0].id: ast.literal_eval(n.value) for n in cls.body
             if isinstance(n, ast.Assign) and len(n.targets) == 1
             and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Constant)}
    version_tree = ast.parse(files[PLUGIN + "version.py"].decode())
    declared = next((ast.literal_eval(n.value) for n in version_tree.body
                     if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in n.targets)), None)
    if (attrs.get("plugin_name") != entry.get("name") or attrs.get("plugin_version") != version
            or entry.get("version") != version or declared != version
            or attrs.get("plugin_config_prefix") != "p115lite_"):
        raise SyncError("P115Lite class, version.py and marketplace identity disagree")
    if entry.get("system_version") != ">=3.0.0" or not entry.get("release"):
        raise SyncError("Upstream marketplace must declare a V3 release")


def checked_destination(root, name):
    relative = Path(safe_path(name))
    if any((root / parent).is_symlink() for parent in (relative, *relative.parents) if parent != Path(".")):
        raise SyncError(f"Symlink in mirror destination: {name}")


def checked_local(root, record):
    for name, expected in record["files"].items():
        if not safe_path(name).startswith((PLUGIN, FRONTEND)):
            raise SyncError("Provenance contains unowned path: " + name)
        checked_destination(root, name)
        path = root / safe_path(name)
        if not path.is_file() or digest(path.read_bytes()) != expected:
            raise SyncError(f"Preserve local mirror edit or missing file: {name}")


def prepare(root: Path, snapshot: Snapshot) -> Plan:
    """Validate everything in memory before proposing any filesystem mutation."""
    if not re.fullmatch(r"[0-9a-f]{40}", snapshot.sha) or not re.fullmatch(r"\d+\.\d+\.\d+", snapshot.version):
        raise SyncError("Invalid upstream commit/version")
    for name in snapshot.files:
        if not safe_path(name).startswith((PLUGIN, FRONTEND)):
            raise SyncError(f"Source outside allowed mirror paths: {name}")
    pins((root / "plugins.v3/metatubescraper/requirements.txt").read_text())
    pins(snapshot.files[PLUGIN + "requirements.txt"].decode())
    project = tomllib.loads(snapshot.files[PLUGIN + "pyproject.toml"].decode())["project"]
    if project.get("requires-python") != ">=3.14,<3.15":
        raise SyncError("Python support changed; expected >=3.14,<3.15")
    pins("\n".join(project.get("dependencies", [])))
    check_identity(snapshot.files, snapshot.entry, snapshot.version)
    runtime = read_zip(snapshot.archive)
    source = {n[len(PLUGIN):]: data for n, data in snapshot.files.items()
              if n.startswith(PLUGIN) and not n.startswith(PLUGIN + "tests/")}
    for name, data in source.items():
        if runtime.get(name) != data:
            raise SyncError(f"Source and release differ: {name}")
    wheel_names = set()
    for name, data in runtime.items():
        if name in source:
            continue
        if not name.startswith("wheels/") or not name.endswith(".whl"):
            raise SyncError(f"Unexpected runtime artifact: {name}")
        parts = PurePosixPath(name).name.split("-")
        package = parts[0]
        if package not in WHEELS or package in wheel_names or "-cp314-cp314-" not in name or "x86_64" not in name or "manylinux" not in name:
            raise SyncError(f"Unsupported/duplicate wheel: {name}")
        contents = read_zip(data)
        metadata = [value for path, value in contents.items() if path.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or f"Name: {package}" not in metadata[0].decode():
            raise SyncError(f"Wheel metadata mismatch: {name}")
        wheel_names.add(package)
    if wheel_names != WHEELS:
        raise SyncError("Missing compatible Rust wheels")
    if not snapshot.license.strip() or not snapshot.notice.strip():
        raise SyncError("Upstream license/NOTICE missing")
    files = dict(snapshot.files)
    files.update({PLUGIN + n: b for n, b in runtime.items() if n.startswith("wheels/")})
    for prefix in (PLUGIN, FRONTEND):
        files[prefix + "LICENSE"] = snapshot.license
        files[prefix + "NOTICE"] = snapshot.notice + b"\nMirrored and packaged by zcs19960929/MoviePilot-Plugins.\nNo endorsement by the original authors is implied.\n"
    manifest = json.loads((root / "package.v3.json").read_text())
    entry = dict(snapshot.entry)
    record = {"repository": UPSTREAM, "commit": snapshot.sha, "version": snapshot.version,
              "tag": "P115Lite_v" + snapshot.version,
              "upstream_archive_sha256": digest(snapshot.archive),
              "platform": "CPython 3.14 / Linux glibc x86_64",
              "runtime_paths": sorted(runtime), "entry": entry,
              "files": {n: digest(b) for n, b in sorted(files.items())}}
    previous_path = root / RECORD
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else None
    if previous:
        checked_local(root, previous)
        if (previous["files"] == record["files"] and previous["entry"] == entry
                and previous["upstream_archive_sha256"] == record["upstream_archive_sha256"]
                and manifest.get("P115Lite") == entry):
            return Plan(snapshot.version, {}, [], False)
        if previous["version"] == snapshot.version:
            raise SyncError("Published version changed; do not overwrite the same version")
    elif "P115Lite" in manifest:
        raise SyncError("Existing unmanaged P115Lite entry must be reviewed before adopting")
    # Refuse collisions with untracked/user-managed source and symlinked parents.
    for name in files.keys() | {RECORD, "package.v3.json"}:
        target = root / name
        checked_destination(root, name)
        if target.exists() and name not in (previous or {}).get("files", {}) and name not in {RECORD, "package.v3.json"}:
            raise SyncError(f"Preserve unmanaged local file: {name}")
    manifest["P115Lite"] = entry
    files["package.v3.json"] = encoded(manifest)
    files[RECORD] = encoded(record)
    return Plan(snapshot.version, files, sorted(set((previous or {}).get("files", {})) - files.keys()), True)


def apply(root: Path, plan: Plan):
    if not plan.changed:
        return
    for name in list(plan.files) + plan.remove:
        checked_destination(root, name)
    for name, data in plan.files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            output.write(data)
            temp = Path(output.name)
        os.replace(temp, path)
    for name in plan.remove:
        (root / name).unlink()


def build_archive(root: Path):
    record = json.loads((root / RECORD).read_text())
    checked_local(root, record)
    pins((root / "plugins.v3/metatubescraper/requirements.txt").read_text())
    pins((root / PLUGIN / "requirements.txt").read_text())
    if json.loads((root / "package.v3.json").read_text()).get("P115Lite") != record["entry"]:
        raise SyncError("Local marketplace differs from verified upstream entry")
    files = {}
    for name in record["runtime_paths"]:
        safe_path(name)
        if name.startswith("tests/") or PLUGIN + name not in record["files"]:
            raise SyncError("Unowned or test runtime path: " + name)
        files[name] = (root / PLUGIN / name).read_bytes()
    for name in ("LICENSE", "NOTICE"):
        files[name] = (root / PLUGIN / name).read_bytes()
    files["UPSTREAM.json"] = encoded({k: v for k, v in record.items() if k not in {"files", "entry", "runtime_paths"}})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
    return output.getvalue()


def download(url, limit=50 * 1024 * 1024):
    headers = {"User-Agent": "MoviePilot-P115Lite-Mirror", "Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise SyncError("Download exceeds size limit")
    return data


def fetch_snapshot(ref):
    api = "https://api.github.com/repos/" + UPSTREAM
    commit = json.loads(download(api + "/commits/" + urllib.parse.quote(ref, safe="")))
    sha = commit["sha"]
    data = download(f"https://codeload.github.com/{UPSTREAM}/tar.gz/{sha}")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        if sum(x.size for x in archive.getmembers()) > 150 * 1024 * 1024:
            raise SyncError("Source archive exceeds size limit")
        for item in archive.getmembers():
            safe_path(item.name)
            name = item.name.partition("/")[2]
            if not (name.startswith((PLUGIN, FRONTEND)) or name in {"LICENSE", "NOTICE", "package.v3.json"}):
                continue
            if item.isdir():
                continue
            if not item.isfile() or name in files:
                raise SyncError("Unsafe source archive member: " + name)
            files[name] = archive.extractfile(item).read()
    entry = json.loads(files.pop("package.v3.json"))["P115Lite"]
    version = entry["version"]
    release = json.loads(download(api + "/releases/tags/P115Lite_v" + version))
    assets = [a for a in release["assets"] if a["name"] == f"p115lite_v{version}.zip"]
    if len(assets) != 1:
        raise SyncError("Expected exactly one upstream plugin ZIP")
    asset = assets[0]
    content = download(asset["browser_download_url"])
    if asset.get("digest") != "sha256:" + digest(content):
        raise SyncError("Upstream Release SHA256 missing or mismatched")
    license_data, notice = files.pop("LICENSE"), files.pop("NOTICE")
    return Snapshot(sha, version, files, entry, content, license_data, notice)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="main", help="Upstream branch/tag/commit, resolved to a fixed SHA")
    parser.add_argument("--apply", action="store_true", help="Explicitly update owned source paths")
    parser.add_argument("--verify", action="store_true", help="Verify local mirror without network")
    parser.add_argument("--build-dir", type=Path, help="Write deterministic plugin ZIP and SHA256")
    args = parser.parse_args(argv)
    try:
        if args.apply and args.verify:
            raise SyncError("--apply and --verify are mutually exclusive")
        if not args.verify:
            snapshot = fetch_snapshot(args.ref)
            plan = prepare(ROOT, snapshot)
            if args.apply:
                apply(ROOT, plan)
            report = {"version": plan.version, "commit": snapshot.sha, "changed": plan.changed, "applied": args.apply}
        else:
            record = json.loads((ROOT / RECORD).read_text())
            build_archive(ROOT)
            report = {"version": record["version"], "verified": True}
        if args.build_dir:
            if not args.verify and not args.apply:
                raise SyncError("Building requires --verify or --apply")
            args.build_dir.mkdir(parents=True, exist_ok=True)
            archive = build_archive(ROOT)
            name = f"p115lite_v{report['version']}.zip"
            (args.build_dir / name).write_bytes(archive)
            (args.build_dir / (name + ".sha256")).write_text(digest(archive) + "  " + name + "\n")
            report.update(archive=name, sha256=digest(archive))
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (SyncError, OSError, ValueError, KeyError, zipfile.BadZipFile, tarfile.TarError) as error:
        print("Synchronization rejected: " + str(error), file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
