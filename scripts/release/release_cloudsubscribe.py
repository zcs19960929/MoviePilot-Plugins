#!/usr/bin/env python3
"""Release helpers for the CloudSubscribe MoviePilot plugin (self-hosted fork).

MoviePilot 只有在下列条件全部满足时才会在插件详情页提供版本选择，因此这里的
每一条校验都对应宿主规则：

* 标签名为 ``CloudSubscribe_v<version>``；
* 发布附件为标签小写加 ``.zip``；
* ``package.v3.json`` 中该插件版本与 ``plugin_version`` 一致且 ``"release": true``；
* 压缩包根目录直接是插件文件，且 ``plugin_version`` 与标签一致；
* 插件依赖 Vue 前端，压缩包必须包含 ``dist/assets/remoteEntry.js``。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import zipfile

PLUGIN_ID = "CloudSubscribe"
PLUGIN_DIR = pathlib.Path("plugins.v3") / "cloudsubscribe"
MANIFEST = pathlib.Path("package.v3.json")
FRONTEND_ENTRY = "dist/assets/remoteEntry.js"


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
    version = (args.version or read_version()).strip()
    problems = []

    if not MANIFEST.is_file():
        problems.append(f"缺少 {MANIFEST}")
    else:
        meta = json.loads(MANIFEST.read_text(encoding="utf-8")).get(PLUGIN_ID, {})
        if not meta:
            problems.append(f"{MANIFEST} 缺少 {PLUGIN_ID} 条目")
        if meta.get("version") != version:
            problems.append(
                f"{MANIFEST} 的 version={meta.get('version')} 与 plugin_version={version} 不一致"
            )
        if meta.get("release") is not True:
            problems.append(f'{MANIFEST} 缺少 "release": true')
        if meta.get("v3") is not True:
            problems.append(f'{MANIFEST} 缺少 "v3": true，V3 市场不会展示该条目')
        history = meta.get("history") or {}
        if f"v{version}" not in history:
            problems.append(f"history 缺少 v{version} 说明")

    if not (PLUGIN_DIR / FRONTEND_ENTRY).is_file():
        problems.append(f"缺少前端产物 {PLUGIN_DIR / FRONTEND_ENTRY}，配置页会白屏")

    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        raise SystemExit(1)
    if not args.version:
        print(f"版本校验通过：{version}")


def cmd_verify_asset(args: argparse.Namespace) -> None:
    version = args.version.strip()
    path = pathlib.Path(args.archive)
    if not path.is_file():
        _fail(f"发布包不存在：{path}")
    with zipfile.ZipFile(path) as archive:
        broken = archive.testzip()
        if broken:
            _fail(f"压缩包损坏：{broken}")
        names = archive.namelist()
        if "__init__.py" not in names:
            _fail("压缩包根目录缺少 __init__.py（打包时必须进入插件目录内部）")
        if FRONTEND_ENTRY not in names:
            _fail(f"压缩包缺少 {FRONTEND_ENTRY}")
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
    if declared.group(1) != version:
        _fail(f"压缩包内 plugin_version={declared.group(1)} 与标签版本 {version} 不一致")
    print(f"发布包自检通过：{path.name}，{len(names)} 个文件，版本 {version}")


def cmd_notes(args: argparse.Namespace) -> None:
    version = args.version.strip()
    meta = json.loads(MANIFEST.read_text(encoding="utf-8")).get(PLUGIN_ID, {})
    section = (meta.get("history") or {}).get(f"v{version}", "变更见提交记录")

    lines = [
        "MoviePilot 插件「网盘订阅助手」自用 fork，适用于 MoviePilot V3。",
        "",
        "本仓库版本基于 [odomu/MoviePilot-Plugins](https://github.com/odomu/MoviePilot-Plugins) "
        "的 CloudSubscribe v1.7.11 构建，主要修复整理落点的一级分类路径。",
        "",
        "## 安装方式",
        "",
        "插件市场添加仓库 `https://github.com/zcs19960929/MoviePilot-Plugins`，"
        f"在插件详情页选择版本 `v{version}`；对应标签 `{tag_for(version)}`，"
        f"附件 `{asset_for(version)}`。",
        "",
        "> 与上游插件 ID 相同，安装前请先卸载上游版本并移除 odomu 插件市场源，"
        "避免两张同名卡片互相覆盖。",
        "",
        "## 更新内容",
        "",
        f"- {section}",
        "",
        "## 发布包",
        "",
        f"- 标签：`{tag_for(version)}`",
        f"- 附件：`{asset_for(version)}`（插件源码 + `dist` 前端产物）",
        "- 依赖见包内 `requirements.txt`（`p115client`、`torf`、`curl_cffi`、`numpy`、`Pillow`）",
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
