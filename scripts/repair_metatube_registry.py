#!/usr/bin/env python3
"""Repair explicitly verified legacy STRM capabilities; dry run unless --apply.

Run inside MoviePilot's Python environment. Credentials stay in process memory;
read the saved plugin config through its authenticated API or a private JSON file.
No cloud rename/move/delete, metadata scraping or media download is performed.
"""
import argparse
import importlib
import json
import os
import re
import sqlite3
import sys
import types
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def runtime(directory):
    package = types.ModuleType('metatube_repair_runtime')
    package.__path__ = [str(directory)]
    sys.modules[package.__name__] = package
    return importlib.import_module(package.__name__ + '.repair')


def saved_config(args):
    if args.config_file:
        return json.loads(args.config_file.read_text())
    key = os.environ.get('API_TOKEN', '')
    if not key and args.env_file.is_file():
        for line in args.env_file.read_text().splitlines():
            if line.startswith('API_TOKEN='):
                key = line.split('=', 1)[1].strip().strip('\"\x27')
    if not key:
        raise ValueError('MoviePilot API_TOKEN 未配置；请使用宿主环境或私有配置文件')
    request = urllib.request.Request(args.api_url.rstrip('/') + '/plugin/' + args.plugin_id,
                                     headers={'X-API-KEY': key})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            config = json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError('读取已保存配置失败：HTTP ' + str(exc.code)) from None
    if isinstance(config, dict) and isinstance(config.get('data'), dict):
        config = config['data']
    if not isinstance(config, dict):
        raise ValueError('MoviePilot 返回了无效配置')
    return config


def backup(directory, config, recovery, preview):
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(directory, 0o700)
    src = sqlite3.connect((recovery.data_dir/'direct.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)
    dest = sqlite3.connect(directory/'registry.sqlite3')
    try:
        src.backup(dest)
    finally:
        src.close(); dest.close()
    os.chmod(directory/'registry.sqlite3', 0o600)
    with open(directory/'config.json', 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
    for item in preview['ready']:
        target = directory/'strm'/item['path'].relative_to(recovery.root)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(target, 'xb', opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
            file.write(item['path'].read_bytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--config-file', type=Path, help='NAS 本机私有的已保存插件配置 JSON')
    source.add_argument('--api-url', default='http://127.0.0.1:3001/api/v1')
    parser.add_argument('--env-file', type=Path, default=Path('/config/app.env'))
    parser.add_argument('--plugin-id', default='MetaTubeScraper')
    parser.add_argument('--plugin-dir', type=Path, default=Path(__file__).resolve().parents[1]/'plugins.v3/metatubescraper')
    parser.add_argument('--data-dir', type=Path, required=True, help='含 direct.sqlite3 的 115_direct 目录')
    parser.add_argument('--output-root', type=Path, required=True, help='明确授权恢复的本地绝对目录')
    parser.add_argument('--backup-dir', type=Path, help='--apply 时创建，必须尚不存在')
    parser.add_argument('--apply', action='store_true', help='写入经重新核验的登记，默认只读检查')
    args = parser.parse_args(argv)
    if not re.fullmatch('[A-Za-z][A-Za-z0-9_]*', args.plugin_id):
        parser.error('插件实例标识无效')
    try:
        config = saved_config(args)
        storage = config.get('storage')
        if isinstance(storage, dict): storage = storage.get('value')
        if storage != '115_direct': raise ValueError('修复仅支持 115 Cookie 直连模式')
        if args.apply and config.get('enabled'):
            raise ValueError('写入前请先暂停插件整理任务（关闭启用并保存），完成后恢复原设置')
        m = runtime(args.plugin_dir)
        roots = config.get('scan_dirs') or ''
        if isinstance(roots, str): roots = [p.strip() for p in re.split('[,;\n\r]+', roots) if p.strip()]
        roots = [p.get('value', '') if isinstance(p, dict) else p for p in roots]
        cloud = m.Cloud115(config.get('cookie115'), max(1, float(config.get('cloud_interval', 5))))
        recovery = m.Recovery(cloud, args.data_dir, args.output_root, config.get('playback_base_url', ''),
                              roots, plugin_id=args.plugin_id, recursive=config.get('scan_mode') == 'recursive',
                              progress=lambda msg: print(msg, flush=True))
        preview = recovery.preview()
        print(json.dumps({'mode': 'apply' if args.apply else 'check', 'total': preview['total'],
                          'ready': len(preview['ready']), 'already': preview['already'],
                          'skipped': preview['skipped']}, ensure_ascii=False), flush=True)
        if args.apply:
            if preview['ready']:
                target = args.backup_dir or args.data_dir/'backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                backup(target, config, recovery, preview)
                print('修复前备份：' + str(target), flush=True)
            print(json.dumps(recovery.apply(preview), ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        # SDK errors are already sanitized in Cloud115. Never print raw network
        # exception text (may contain a Cookie or signed playback URL).
        safe = isinstance(exc, ValueError) or ('m' in locals() and isinstance(exc, m.CloudError))
        print('恢复停止：' + (str(exc) if safe else type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
