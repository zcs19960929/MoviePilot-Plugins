#!/usr/bin/env python3
"""Release helpers for the MetaTubeScraper MoviePilot plugin.

MoviePilot only offers GitHub Release versions when all of the following hold,
so every check here mirrors the host rules:

* tag name is ``MetaTubeScraper_v<version>``;
* the release asset is the lowercased tag plus ``.zip``;
* ``package.v2.json`` / ``package.v3.json`` carry the same version and
  ``"release": true``;
* the zip root holds the plugin files and its ``plugin_version`` matches.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import zipfile

PLUGIN_ID = "MetaTubeScraper"
PLUGIN_DIR = pathlib.Path("plugins.v3") / "metatubescraper"


def _fail(message: str) -> None:
    print(f"::error::{message}")
    raise SystemExit(1)


def read_version() -> str:
    text = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^    plugin_version = "([^"]+)"', text, re.M)
    if not match:
        _fail(f"无法从 {PLUGIN_DIR / '__init__.py'} 读取 plugin_version")
    return match.group(1)


def tag_for(version: str) -> str:
    return f"{PLUGIN_ID}_v{version}"


def asset_for(version: str) -> str:
    return f"{tag_for(version).lower()}.zip"


def cmd_check(args: argparse.Namespace) -> None:
    version = args.version or read_version()
    problems = []
    for name in ("package.v2.json", "package.v3.json"):
        path = pathlib.Path(name)
        if not path.is_file():
            problems.append(f"缺少 {name}")
            continue
        meta = json.loads(path.read_text(encoding="utf-8")).get(PLUGIN_ID, {})
        if meta.get("version") != version:
            problems.append(
                f"{name} 的 version={meta.get('version')} 与 plugin_version={version} 不一致"
            )
        if meta.get("release") is not True:
            problems.append(f'{name} 缺少 "release": true，MoviePilot 不会提供版本选择')
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        raise SystemExit(1)
    if not args.version:
        print(f"版本校验通过：{version}")


def cmd_verify_asset(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.archive)
    if not path.is_file():
        _fail(f"发布包不存在：{path}")
    with zipfile.ZipFile(path) as archive:
        broken = archive.testzip()
        if broken:
            _fail(f"压缩包损坏：{broken}")
        names = archive.namelist()
        if "__init__.py" not in names:
            _fail("压缩包根目录缺少 __init__.py")
        illegal = [name for name in names if name.startswith("/") or ".." in name.split("/")]
        if illegal:
            _fail(f"压缩包包含非法路径：{illegal}")
        declared = re.search(
            r'^    plugin_version = "([^"]+)"',
            archive.read("__init__.py").decode("utf-8"),
            re.M,
        )
    if not declared:
        _fail("压缩包内的 __init__.py 缺少 plugin_version")
    if declared.group(1) != args.version:
        _fail(f"压缩包内 plugin_version={declared.group(1)} 与标签版本 {args.version} 不一致")
    print(f"发布包自检通过：{path.name}，{len(names)} 个文件，版本 {args.version}")


def cmd_notes(args: argparse.Namespace) -> None:
    changelog = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(
        rf"^## {re.escape(args.version)}\s*$(.*?)(?=^## |\Z)",
        changelog,
        re.M | re.S,
    )
    section = match.group(1).strip() if match else ""
    meta = json.loads(
        pathlib.Path("package.v3.json").read_text(encoding="utf-8")
    ).get(PLUGIN_ID, {})
    if not section:
        section = f"- {meta.get('history', {}).get('v' + args.version, '变更见提交记录')}"

    lines = [
        "MoviePilot 插件「MetaTube 番号刮削」发布包，适用于 MoviePilot V3（容器版，Python ≥ 3.12）。",
        "",
        "## 安装方式",
        "",
        "插件市场添加仓库 `https://github.com/zcs19960929/MoviePilot-Plugins`，"
        f"在插件详情页选择版本 `v{args.version}`；对应标签 `{tag_for(args.version)}`，"
        f"附件 `{asset_for(args.version)}`。",
        "",
        "## 更新内容",
        "",
        section,
        "",
        "## 发布包",
        "",
        f"- 标签：`{tag_for(args.version)}`",
        f"- 附件：`{asset_for(args.version)}`（插件目录源码，不含 `__pycache__` 与缓存）",
        "- 依赖见包内 `requirements.txt`（`p115client`、`python-concurrenttools`，Python ≥ 3.12）",
    ]
    text = "\n".join(lines) + "\n"
    pathlib.Path(args.output).write_text(text, encoding="utf-8")
    print(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="校验清单版本与 release 声明")
    check.add_argument("version", nargs="?", default="")
    check.set_defaults(func=cmd_check)

    verify = subparsers.add_parser("verify-asset", help="校验发布包结构与版本")
    verify.add_argument("archive")
    verify.add_argument("version")
    verify.set_defaults(func=cmd_verify_asset)

    notes = subparsers.add_parser("notes", help="生成 Release 说明")
    notes.add_argument("version")
    notes.add_argument("output")
    notes.set_defaults(func=cmd_notes)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
