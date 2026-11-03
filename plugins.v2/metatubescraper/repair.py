"""Explicit legacy STRM recovery. Preview never writes; cloud operations are read-only."""
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .cloud115 import Cloud115, Entry, CloudError, AuthenticationError, RiskError, Stopped
from .direct import Registry, VIDEO_EXTS, digest, UnknownMedia


class Recovery:
    def __init__(self, client, data_dir, output_root, playback_base_url, scan_roots,
                 plugin_id="MetaTubeScraper", recursive=False, max_depth=3, progress=None):
        self.client, self.data_dir = client, Path(data_dir)
        root = Path(output_root)
        if not root.is_absolute() or root.is_symlink():
            raise CloudError("恢复目录必须是无符号链接的绝对路径")
        self.root = root.resolve()
        self.base = urlsplit(playback_base_url.rstrip('/'))
        if self.base.scheme not in ('http', 'https') or not self.base.netloc or self.base.username or self.base.query or self.base.fragment:
            raise CloudError("MoviePilot 播放入口地址无效")
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', plugin_id):
            raise CloudError("插件实例标识无效")
        self.route = self.base.path.rstrip('/') + '/api/v1/plugin/' + plugin_id + '/redirect_url'
        self.roots, self.depth = list(scan_roots), max_depth if recursive else 1
        if not self.roots:
            raise CloudError("请提供已确认的 115 扫描目录")
        self.progress = progress or (lambda message: None)

    def _local(self, path):
        if (self.root.is_symlink() or path.is_symlink() or path.parent.is_symlink()
                or path.parent.parent != self.root or path.parent.resolve().parent != self.root):
            raise CloudError("保留符号链接或超出恢复范围的文件")
        if not path.is_file() or path.stat().st_size > 8192:
            raise CloudError("STRM 不是受支持的普通文件")
        raw = path.read_bytes()
        try:
            url = urlsplit(raw.decode('utf-8').strip())
            query = parse_qs(url.query, keep_blank_values=True)
        except (ValueError, UnicodeError):
            raise CloudError("STRM 地址格式无效") from None
        if (url.scheme != self.base.scheme or url.netloc != self.base.netloc or url.path != self.route
                or url.fragment or set(query) != {'media_id'} or len(query['media_id']) != 1
                or not re.fullmatch(r'[a-f0-9]{64}', query['media_id'][0])):
            raise CloudError("保留非当前 MetaTube 实例的播放地址")
        number = path.parent.name
        if not re.fullmatch(r'[A-Z0-9][A-Z0-9-]*', number) or not re.fullmatch(re.escape(number) + r'(?:-CD[1-9]\d*)?', path.stem):
            raise CloudError("影片目录、番号或视频分段不一致")
        nfo = path.parent / 'movie.nfo'
        if nfo.exists():
            if nfo.is_symlink() or nfo.stat().st_size > 4 * 1024 * 1024:
                raise CloudError("本地 NFO 文件不适合核验")
            try:
                stated = ET.fromstring(nfo.read_bytes()).findtext('num')
            except ET.ParseError:
                raise CloudError("本地 NFO 无法解析") from None
            if stated and stated.strip() != number:
                raise CloudError("NFO 番号与影片目录不一致")
        return raw, query['media_id'][0]

    def _inventory(self, priority):
        queue = deque((self.client.resolve_folder(root), 0) for root in self.roots)
        visited, videos = set(), defaultdict(list)
        while queue:
            cid, depth = queue.popleft()
            if cid in visited:
                continue
            visited.add(cid)
            entries = self.client.listdir(cid)
            self.progress('已只读核验 %s 个网盘目录' % len(visited))
            for entry in entries:
                if not entry.is_dir and Path(entry.name).suffix.lower() in VIDEO_EXTS:
                    videos[Path(entry.name).stem.casefold()].append(entry)
            children = [e for e in entries if e.is_dir and depth < self.depth]
            children.sort(key=lambda e: e.id not in priority)
            queue.extend((e.id, depth + 1) for e in children)
        return videos

    def preview(self):
        registry = Registry(self.data_dir, readonly=True)
        result = {'ready': [], 'already': 0, 'skipped': [], 'total': 0, 'database_identity': registry.identity}
        try:
            # Journaled parents are inspected first. Full scoped enumeration is
            # still required to rule out same-name versions in other folders.
            priority = set()
            for row in registry.db.execute('SELECT plan FROM jobs WHERE account=?', (self.client.account,)):
                plan = json.loads(row[0])
                priority.update(str(f['entry']['parent_id']) for f in plan['files'] if f['kind'] == 'video')
            candidates = []
            for path in sorted(self.root.glob('*/*.strm')):
                result['total'] += 1
                try:
                    raw, token = self._local(path)
                    candidates.append((path, raw, token))
                except (CloudError, OSError) as exc:
                    result['skipped'].append({'file': str(path), 'reason': str(exc) if isinstance(exc, CloudError) else '本地文件不可读取'})
            if not candidates:
                return result
            videos = self._inventory(priority)
            for path, raw, token in candidates:
                try:
                    matches = videos.get(path.stem.casefold(), [])
                    if len(matches) != 1:
                        raise CloudError('网盘视频无法唯一匹配（找到 %s 个同名版本）' % len(matches))
                    listed = matches[0]
                    current = self.client.info(listed.id)
                    if current != listed or current.is_dir or not current.pickcode:
                        raise CloudError('网盘文件状态变化或缺少播放标识')
                    try:
                        old = registry.media(self.client.account, token)
                    except UnknownMedia:
                        old = None
                    if old:
                        if old.id != current.id:
                            raise CloudError('旧播放标识已指向其他文件')
                        result['already'] += 1
                        continue
                    snapshot = registry.snapshot(self.client.account, current.id, token)
                    for row in snapshot[0] + snapshot[1]:
                        if row[0] == token and (row[1], row[2]) != (self.client.account, current.id):
                            raise CloudError('旧播放标识已属于其他账户或文件')
                    result['ready'].append({'path': path, 'digest': digest(raw), 'token': token,
                                            'entry': current, 'account': self.client.account, 'snapshot': snapshot})
                except (AuthenticationError, RiskError, Stopped):
                    raise
                except (CloudError, OSError) as exc:
                    result['skipped'].append({'file': str(path), 'reason': str(exc) if isinstance(exc, CloudError) else '网盘文件不可读取'})
            return result
        finally:
            registry.close()

    def apply(self, preview):
        result = {'restored': 0, 'already': preview['already'], 'skipped': list(preview['skipped'])}
        if not preview['ready']:
            return result
        registry = Registry(self.data_dir, create=False, expected_identity=preview['database_identity'])
        try:
            videos = self._inventory(set())
            for item in preview['ready']:
                path = item['path']
                try:
                    raw, token = self._local(path)
                    if digest(raw) != item['digest'] or token != item['token'] or self.client.account != item['account']:
                        raise CloudError('本地文件或账户在检查后变化，请重新检查')
                    matches = videos.get(path.stem.casefold(), [])
                    if len(matches) != 1 or matches[0] != item['entry']:
                        raise CloudError('网盘匹配在检查后变化或出现多个版本，请重新检查')
                    if self.client.info(item['entry'].id) != item['entry']:
                        raise CloudError('网盘文件在检查后变化，请重新检查')
                    # No file is modified. The affected registry rows are checked
                    # again under BEGIN IMMEDIATE by restore_media.
                    changed = registry.restore_media(item['account'], item['entry'], token, expected=item['snapshot'])
                    result['restored' if changed else 'already'] += 1
                    self.progress('已恢复 %s 个旧播放标识' % result['restored'])
                except (AuthenticationError, RiskError, Stopped):
                    raise
                except (CloudError, OSError) as exc:
                    result['skipped'].append({'file': str(path), 'reason': str(exc) if isinstance(exc, CloudError) else '本地文件不可读取'})
            return result
        finally:
            registry.close()
