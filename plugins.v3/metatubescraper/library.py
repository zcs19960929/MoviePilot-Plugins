"""Movie-level 115 library, stable versions and classified local publication."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .cloud115 import AuthenticationError, CloudError, Entry, NotFoundError, RiskError, Stopped
from .direct import PlaybackService, Registry, SUBTITLE_EXTS, VIDEO_EXTS, _part, checked_path, digest
from .scan_progress import CheckpointError, ScanProgress
from .classification import ClassificationPolicy, directory_name
from .migration import MigrationManager
from .naming import NamingManager, REVISION as NAMING_REVISION


class ScanReadError(CloudError):
    """Directory failures must retain the current checkpoint, not skip a movie."""


class DirectoryGone(CloudError):
    """A queued directory no longer exists; it can be dropped, not paused on."""

    def __init__(self, message, cid=None):
        super().__init__(message)
        self.cid = cid


def category(number):
    prefix = number.rsplit('-', 1)[0]
    return '数字番号' if prefix.isdigit() else prefix


def movie_relative(number, group=None):
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9-]*', number):
        raise CloudError('番号不适合作为输出目录')
    return str(Path(directory_name(group) if group is not None else category(number))/number)


def movie_job(root, number):
    return 'movie:' + hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:24] + ':' + number


def version_job(root, number, version):
    return movie_job(root, number) + ':V' + str(int(version)).zfill(4)


def variant_stem(number, version, part=''):
    return number + ' - V' + str(int(version)).zfill(4) + part


class LibraryRegistry(Registry):
    def reopen(self):
        super().reopen()
        if self.readonly:
            return
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS library_movies (
                account TEXT, root TEXT, number TEXT, relative_dir TEXT,
                metadata TEXT, cloud_root TEXT, cloud_folder TEXT,
                PRIMARY KEY(account,root,number));
            CREATE TABLE IF NOT EXISTS library_versions (
                account TEXT, root TEXT, number TEXT, version INTEGER,
                files TEXT, complete INTEGER DEFAULT 0,
                PRIMARY KEY(account,root,number,version));
            CREATE TABLE IF NOT EXISTS library_files (
                account TEXT, root TEXT, fid TEXT, number TEXT, version INTEGER,
                PRIMARY KEY(account,root,fid));
        ''')
        self.db.commit()

    def movie(self, account, root, number):
        row = self.db.execute('SELECT * FROM library_movies WHERE account=? AND root=? AND number=?',
                              (account, str(root), number)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value['metadata'] = json.loads(value['metadata']) if value['metadata'] else None
        return value

    def ensure_movie(self, account, root, number, relative_dir, metadata=None):
        with self._lock, self.db:
            self.check_identity()
            row = self.movie(account, root, number)
            if row and row['relative_dir'] != str(relative_dir):
                raise CloudError('已登记的影片输出位置与番号分类不一致')
            self.db.execute('''INSERT INTO library_movies(account,root,number,relative_dir,metadata)
                VALUES(?,?,?,?,?) ON CONFLICT(account,root,number) DO UPDATE SET
                metadata=COALESCE(excluded.metadata,library_movies.metadata)''',
                (account, str(root), number, str(relative_dir), json.dumps(metadata, ensure_ascii=False) if metadata else None))
        return self.movie(account, root, number)

    def versions(self, account, root, number):
        rows = self.db.execute('SELECT * FROM library_versions WHERE account=? AND root=? AND number=? ORDER BY version',
                               (account, str(root), number))
        return [dict(row, files=json.loads(row['files'])) for row in rows]

    def assign_versions(self, account, root, number, groups):
        assigned = {}
        with self._lock, self.db:
            self.check_identity()
            self.db.execute('BEGIN IMMEDIATE')
            next_version = self.db.execute('SELECT COALESCE(MAX(version),0)+1 FROM library_versions WHERE account=? AND root=? AND number=?',
                                           (account, str(root), number)).fetchone()[0]
            for group in groups:
                matches = set()
                for entry in group:
                    row = self.db.execute('SELECT number,version FROM library_files WHERE account=? AND root=? AND fid=?',
                                           (account, str(root), entry.id)).fetchone()
                    if row:
                        if row['number'] != number:
                            raise CloudError('视频 ID 已登记为其他番号：' + entry.id)
                        matches.add(row['version'])
                if len(matches) > 1:
                    raise CloudError('多个已登记版本发生分段交叉，请核对原文件名')
                version = next(iter(matches)) if matches else next_version
                if not matches:
                    next_version += 1
                    self.db.execute('INSERT INTO library_versions VALUES(?,?,?,?,?,0)',
                                    (account, str(root), number, version, '[]'))
                for entry in group:
                    self.db.execute('INSERT OR IGNORE INTO library_files VALUES(?,?,?,?,?)',
                                    (account, str(root), entry.id, number, version))
                    assigned[entry.id] = version
        return assigned

    def save_version(self, account, root, number, version, files, complete=False):
        with self._lock, self.db:
            self.check_identity()
            self.db.execute('BEGIN IMMEDIATE')
            previous = self.db.execute('SELECT files FROM library_versions WHERE account=? AND root=? AND number=? AND version=?',
                                       (account, str(root), number, version)).fetchone()
            merged = {item['entry']['id']: item for item in json.loads(previous['files'])} if previous else {}
            merged.update((item['entry']['id'], item) for item in files)
            files = list(merged.values())
            self.db.execute('INSERT INTO library_versions VALUES(?,?,?,?,?,?) ON CONFLICT(account,root,number,version) DO UPDATE SET files=excluded.files,complete=excluded.complete',
                            (account, str(root), number, version, json.dumps(files, ensure_ascii=False), int(complete)))
            for item in files:
                entry = item['entry']
                row = self.db.execute('SELECT number,version FROM library_files WHERE account=? AND root=? AND fid=?',
                                       (account, str(root), entry['id'])).fetchone()
                if row and (row['number'], row['version']) != (number, version):
                    raise CloudError('文件 ID 已归属其他影片版本：' + entry['id'])
                self.db.execute('INSERT OR IGNORE INTO library_files VALUES(?,?,?,?,?)',
                                (account, str(root), entry['id'], number, version))
                if item['kind'] == 'video':
                    self.db.execute('UPDATE media SET entry=? WHERE account=? AND fid=?',
                                    (json.dumps(entry, ensure_ascii=False), account, entry['id']))


def _source_key(entry, name=None):
    stem = re.sub(r'(?:[-_.\s])(?:CD|DISC|PART)[-_.\s]?\d+(?=[-_.\s]|$)', '', Path(name or entry.name).stem, flags=re.I)
    return entry.parent_id, stem.casefold()


def group_videos(number, videos):
    """Only join parts with an unambiguous original basename and parent."""
    groups = defaultdict(list)
    singles = []
    for entry in videos:
        part = _part(entry.name)
        if not part:
            singles.append([entry])
        else:
            groups[_source_key(entry)].append(entry)
    for group in groups.values():
        if len({_part(entry.name) for entry in group}) != len(group):
            raise CloudError('多套分段无法唯一关联，原文件名=' + repr([entry.name for entry in group]))
    result = singles + list(groups.values())
    return sorted(result, key=lambda group: (group[0].parent_id, min(entry.id for entry in group)))


class LibraryService(PlaybackService):
    def __init__(self, client, data_dir, output_root, playback_base_url, metadata,
                 artwork, build_nfo, extract_number, stop_event=None, force=False,
                 plugin_id='MetaTubeScraper', debug=None, info=None, archive_loose=False, error=None, numeric_ids=False,
                 library_layout='prefix', maker_groups=None, archive_root='', cleanup_empty_dirs=True):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', plugin_id):
            raise CloudError('插件实例标识无效')
        if not Path(output_root).is_absolute():
            raise CloudError('本地媒体库根目录必须使用绝对路径，例如 /media/link/japan')
        self.root = Path(output_root).resolve()
        self.base = playback_base_url.rstrip('/')
        parts = urlsplit(self.base)
        if parts.scheme not in ('http','https') or not parts.netloc or parts.username or parts.query or parts.fragment:
            raise CloudError('请填写播放器可访问的 MoviePilot HTTP(S) 地址')
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry = LibraryRegistry(data_dir, self.root)
        self.client, self.plugin_id = client, plugin_id
        self.metadata, self.artwork = metadata, artwork
        self.build_nfo, self.extract_number = build_nfo, extract_number
        self.stop_event = stop_event or threading.Event()
        self.force, self.archive_loose = force, archive_loose
        self.numeric_ids = numeric_ids
        self.policy = ClassificationPolicy(library_layout, maker_groups)
        self.archive_root = str(archive_root or '').strip()
        self.cleanup_empty_dirs = bool(cleanup_empty_dirs)
        self._archive_id = None
        self._relatives = {}
        self._origin_ancestors = {}
        self._registered_migration = False
        self._debug = debug or (lambda message: None)
        self._info = info or (lambda message: None)
        self._error = error or (lambda message: None)
        self._cache = {}
        self._play_lock = threading.RLock()
        self._metadata_cache, self._artwork_cache = {}, {}
        self._refreshed_subtitles = set()
        self.migrations = MigrationManager(self)
        self.naming = NamingManager(self)
        self._registered_categories = self.migrations.categories()

    def _stopped(self):
        if self.stop_event.is_set():
            raise Stopped('任务已停止')

    def _listdir(self, cid):
        try:
            return self.client.listdir(cid)
        except (AuthenticationError, RiskError, Stopped):
            raise
        except NotFoundError as exc:
            raise DirectoryGone('目录已不存在：' + str(exc), cid) from None
        except Exception as exc:
            reason = str(exc) if isinstance(exc, CloudError) else type(exc).__name__
            raise ScanReadError('目录读取失败，已保留扫描位置：'+reason) from None

    def _folder_info(self, cid):
        try:
            entry = self.client.info(cid)
            if not entry.is_dir:
                raise ScanReadError('目录身份已变化，已保留扫描位置')
            return entry
        except (AuthenticationError, RiskError, Stopped, ScanReadError):
            raise
        except NotFoundError as exc:
            raise DirectoryGone('目录已不存在：' + str(exc), cid) from None
        except Exception as exc:
            reason = str(exc) if isinstance(exc, CloudError) else type(exc).__name__
            raise ScanReadError('目录核验失败，已保留扫描位置：'+reason) from None

    def _folder(self, number):
        folder = self.root/self._relative(number)
        checked_path(self.root, folder/'movie.nfo')
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _checked_name(self, path):
        checked_path(self.root, path)

    def _version_owner(self, number, version):
        return version_job(self.root, number, version)

    def _relative(self, number):
        if number in self._relatives:
            return self._relatives[number]
        movie = self.registry.movie(self.client.account, self.root, number)
        return movie['relative_dir'] if movie else movie_relative(number, self.policy.category(number))

    def _ancestors(self, parent):
        if parent in self._origin_ancestors:
            return self._origin_ancestors[parent]
        chain, seen, current = [], set(), parent
        while current not in seen:
            seen.add(current)
            chain.append(current)
            if current == '0':
                break
            folder = self._scan_folders.get(current) or self._folder_info(current)
            current = folder.parent_id
        self._origin_ancestors[parent] = chain
        return chain

    def _origin_allowed(self, origin):
        return bool(origin and self._scan_root_ids.intersection(origin['ancestors']))

    def _origin_root(self, origin, number):
        # Rebinding a common ancestor deliberately changes the archive basis.
        for cid in reversed(origin['ancestors']):
            if cid in self._scan_root_ids:
                root = self._scan_archive_roots[cid]
                category_root = self._scan_state['category_roots'].get(root)
                if category_root and self._category_matches(category_root['name'], number):
                    root = category_root['parent_id']
                return root
        return None

    def _remember_origins(self, plans, source_root):
        for files in plans.values():
            for item in files:
                entry = Entry(**item['entry'])
                origin = self.migrations.origin(entry.id)
                if origin:
                    if not self._origin_allowed(origin):
                        raise CloudError('文件来源已移出授权扫描根，冻结迁移：'+entry.name)
                    continue
                parents = self._ancestors(entry.parent_id)
                if not self._scan_root_ids.intersection(parents):
                    raise CloudError('无法核验文件原始扫描范围：'+entry.name)
                self.migrations.record_origin(entry, source_root, parents)

    def _entry_allowed(self, entry, number, source_root):
        if self._within_scan(entry.parent_id, number, source_root):
            return True
        origin = self.migrations.origin(entry.id)
        if not self._origin_allowed(origin):
            return False
        if self.archive_loose and self._archive_id is None and self._origin_root(origin, number) != source_root:
            return False
        row = self.registry.db.execute('SELECT number FROM library_files WHERE account=? AND root=? AND fid=?',
                                       (self.client.account, str(self.root), entry.id)).fetchone()
        if not row or row['number'] != number:
            return False
        movie = self.registry.movie(self.client.account, self.root, number)
        plan = self.migrations.plan(number) or {}
        targets = set(plan.get('cloud_targets', []))
        if self._archive_id is not None:
            targets.add(self._archive_id)
        if movie and movie['cloud_root']:
            targets.add(movie['cloud_root'])
        return bool(targets.intersection(self._ancestors(entry.parent_id)))

    def _category_matches(self, name, number):
        return self.policy.matches(name, number, self._registered_categories)

    def _protected_cloud_roots(self):
        roots = {'0'} | self._scan_root_ids | set(self._scan_archive_roots.values())
        if self._archive_id is not None:
            roots.add(self._archive_id)
        return roots

    def _check_folder(self, folder, identity):
        checked_path(self.root, folder/'movie.nfo')
        current = folder.stat()
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise CloudError('本地影片目录身份发生变化')

    def _plans(self, number, videos, entries):
        account = self.client.account
        existing = self.registry.versions(account, self.root, number)
        known = {item['entry']['id']:(version['version'], item) for version in existing for item in version['files']}
        fresh = [entry for entry in videos if entry.id not in known]
        groups = group_videos(number, fresh)
        for group in groups:
            if not _part(group[0].name):
                continue
            candidates = {}
            key = _source_key(group[0])
            for fid, (version, item) in known.items():
                if item['kind'] != 'video':
                    continue
                source = Entry(**item['entry'])
                source = Entry(**dict(item['entry'], parent_id=item.get('source_parent', source.parent_id)))
                if _source_key(source, item.get('source_name')) == key:
                    candidates[version] = item
            if len(candidates) > 1:
                raise CloudError('新增分段无法唯一关联已登记版本')
            if candidates:
                group.append(Entry(**next(iter(candidates.values()))['entry']))
        # Known multipart files retain their registered version after moves/renames.
        grouped = defaultdict(list)
        for entry in videos:
            if entry.id in known:
                grouped[known[entry.id][0]].append(entry)
        groups += list(grouped.values())
        assigned = self.registry.assign_versions(account, self.root, number, groups)
        plans = defaultdict(list)
        targets = {item['target'].casefold(): fid for fid, (_, item) in known.items()}
        for entry in videos:
            version = assigned[entry.id]
            previous = known.get(entry.id)
            target = previous[1]['target'] if previous else variant_stem(number, version, _part(entry.name)) + Path(entry.name).suffix.lower()
            if target.casefold() in targets and targets[target.casefold()] != entry.id:
                raise CloudError('版本内分段目标重名：' + entry.name)
            targets[target.casefold()] = entry.id
            plans[version].append(dict(entry=asdict(entry), target=target, kind='video',
                                       source_name=previous[1].get('source_name', previous[1]['entry']['name']) if previous else entry.name,
                                       source_parent=previous[1].get('source_parent', previous[1]['entry']['parent_id']) if previous else entry.parent_id))
        for entry in entries:
            if entry.is_dir or Path(entry.name).suffix.lower() not in SUBTITLE_EXTS:
                continue
            previous = known.get(entry.id)
            if previous:
                version, item = previous
                plans[version].append(dict(item, entry=asdict(entry)))
                continue
            matches = {}
            parent_videos = [video for video in videos if video.parent_id == entry.parent_id or
                             (video.id in known and known[video.id][1].get('source_parent') == entry.parent_id)]
            for video in videos:
                if video not in parent_videos:
                    continue
                aliases = [Path(video.name).stem]
                if video.id in known:
                    aliases.append(Path(known[video.id][1]['entry']['name']).stem)
                    aliases.append(Path(known[video.id][1].get('source_name', video.name)).stem)
                aliases.append(number + _part(video.name))
                for alias in aliases:
                    stem = Path(entry.name).stem
                    tail = stem[len(alias):]
                    if stem.casefold().startswith(alias.casefold()) and (not tail or tail[0] in '._- '):
                        match = (len(alias), tail)
                        if video.id not in matches or match[0] > matches[video.id][0]:
                            matches[video.id] = match
            if matches:
                longest = max(value[0] for value in matches.values())
                matches = {fid:value for fid,value in matches.items() if value[0] == longest}
            elif (len(parent_videos) == 1
                  and len([video for video in entries if video.parent_id == entry.parent_id and Path(video.name).suffix.lower() in VIDEO_EXTS]) <= 1
                  and not self.extract_number(entry.name)):
                owner = parent_videos[0]
                matches = {owner.id:(0, Path(entry.name).stem)}
            elif self.extract_number(entry.name) not in (None, number):
                continue
            if not matches and not self.extract_number(entry.name):
                parent_videos = [video for video in entries if video.parent_id == entry.parent_id and Path(video.name).suffix.lower() in VIDEO_EXTS]
                if len(parent_videos) > 1:
                    raise CloudError('字幕无法唯一关联版本，原文件名=' + repr(entry.name))
                continue
            if len(matches) != 1:
                raise CloudError('字幕无法唯一关联版本，原文件名=' + repr(entry.name))
            fid, (_, tail) = next(iter(matches.items()))
            version = assigned[fid]
            video_target = next(item['target'] for item in plans[version] if item['entry']['id']==fid)
            suffix = tail.strip(' .-_')
            target = Path(video_target).stem + ('.'+suffix if suffix else '') + Path(entry.name).suffix.lower()
            plans[version].append(dict(entry=asdict(entry), target=target, kind='subtitle'))
        names = set()
        for files in plans.values():
            for item in files:
                key = item['target'].casefold()
                if key in names:
                    raise CloudError('规范命名后文件重名：' + item['target'])
                names.add(key)
            for item in files:
                if item['target'].endswith('.idx') and item['target'][:-4].casefold()+'.sub' not in names:
                    raise CloudError('IDX 字幕缺少配对的 SUB 文件')
        return dict(plans)

    def _publish(self, folder, identity, path, content, owner, stage):
        self._check_folder(folder, identity)
        self.registry.reserve(path, self.client.account, owner, content)
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        directory_fd = None
        try:
            directory_fd = os.open(str(folder), flags)
            opened = os.fstat(directory_fd)
            if (opened.st_dev, opened.st_ino) != (identity.st_dev, identity.st_ino):
                raise CloudError('本地影片目录身份发生变化')
            self._check_folder(folder, identity)
            self.registry.check_output(path, self.client.account, owner)
            if path.exists():
                os.replace(str(stage), path.name, dst_dir_fd=directory_fd)
            else:
                os.link(str(stage), path.name, dst_dir_fd=directory_fd, follow_symlinks=False)
            self.registry.published(path, content)
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            stage.unlink(missing_ok=True)

    def _process_movie(self, number, videos, entries, cloud_root):
        self._stopped()
        account = self.client.account
        movie = self.registry.movie(account, self.root, number)
        for version in self.registry.versions(account, self.root, number):
            for item in version['files']:
                origin = self.migrations.origin(item['entry']['id'])
                if origin and not self._origin_allowed(origin):
                    raise CloudError('影片含已移除来源的版本，冻结迁移：'+item['entry']['name'])
                if (origin and self.archive_loose and self._archive_id is None and
                        self._origin_root(origin, number) != cloud_root):
                    raise CloudError('同番号来自独立扫描根，请选择共同上级或指定统一网盘目标')
        if number not in self._metadata_cache:
            info = movie['metadata'] if movie else None
            self._metadata_cache[number] = self.metadata(number) if not info or self.force else info
        info = self._metadata_cache[number]
        if not info or not info.get('title'):
            return 'notfound'
        relative = movie_relative(number, self.policy.category(number, info))
        self._relatives[number] = relative
        previous_naming = self.naming.job(number) or {}
        normal_retry = previous_naming and not previous_naming.get('requires_archive',True) and any(
            item['phase'] != 'done' for item in previous_naming['cloud'])
        cloud_names = self.archive_loose or normal_retry or not (
            self._registered_migration or self.naming.needs(number) or previous_naming.get('cloud_deferred'))
        recovered = self.naming.recover(number, False)
        if recovered:
            videos = [self.client.info(e.id) for e in videos]
            entries = [self.client.info(e.id) for e in entries]
        videos = [self.naming.pending_entry(number,e) for e in videos]
        entries = [self.naming.pending_entry(number,e) for e in entries]
        plans = self._plans(number, videos, entries)
        self._remember_origins(plans, cloud_root)
        plans, renamed = self.naming.apply(number, plans, cloud_root, cloud_names)
        owners = {movie_job(self.root, number)} | {version_job(self.root, number, version['version'])
                                                for version in self.registry.versions(account, self.root, number)}
        relocated = self.migrations.relocate_local(number, relative, owners)
        self.registry.ensure_movie(account, self.root, number, relative, info)
        folder = self._folder(number)
        identity = folder.stat()
        shared_owner = movie_job(self.root, number)
        shared = {'movie.nfo': self._render_nfo(info, number)}
        for kind, name in (('poster','poster.jpg'), ('fanart','fanart.jpg')):
            old = self.registry.owned_bytes(folder/name, account, shared_owner)
            key = (number, kind)
            if key not in self._artwork_cache:
                self._artwork_cache[key] = old if old is not None and not self.force else self.artwork(info, kind)
            shared[name] = self._artwork_cache[key]
            if not shared[name]:
                raise CloudError('图片下载失败：' + name)
        data = []
        changed = relocated or recovered or renamed
        for version, files in plans.items():
            owner = version_job(self.root, number, version)
            self.registry.save_version(account, self.root, number, version, files)
            for item in files:
                entry = Entry(**item['entry'])
                if item['kind']=='video':
                    token = self.registry.media_token(account, entry)
                    name = Path(item['target']).stem+'.strm'
                    old = self.registry.owned_bytes(folder/name, account, owner)
                    # Preserve the stable playback URL of an existing owned STRM.
                    content = old if old is not None else (self.base+'/api/v1/plugin/'+self.plugin_id+'/redirect_url?media_id='+token+'\n').encode()
                else:
                    name = item['target']
                    old = self.registry.owned_bytes(folder/name, account, owner)
                    refresh = self.force and entry.id not in self._refreshed_subtitles
                    content = old if old is not None and not refresh else self.client.download_bytes(entry)
                    self._refreshed_subtitles.add(entry.id)
                if not content:
                    raise CloudError('字幕下载结果为空：' + name)
                data.append((folder/name, content, owner))
                changed = changed or old != content or (cloud_names and entry.name != item['target'])
        for name, content in shared.items():
            old = self.registry.owned_bytes(folder/name, account, shared_owner)
            changed = changed or old != content
            data.append((folder/name, content, shared_owner))
        for path, content, owner in data:
            self.registry.reserve(path, account, owner, content)
        with tempfile.TemporaryDirectory(dir=str(self.root), prefix='.metatube-') as temporary:
            staged = {}
            for index, (path, content, owner) in enumerate(data):
                self._stopped()
                self._check_folder(folder, identity)
                stage = Path(temporary)/str(index)
                with open(stage, 'xb') as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                staged[path] = stage
            cloud_renamed = self.naming.recover(number, cloud_names)
            changed = changed or cloud_renamed
            if cloud_renamed:
                for files in plans.values():
                    for item in files:
                        item['entry'] = asdict(self.client.info(item['entry']['id']))
            for version, files in plans.items():
                for item in files:
                    self._stopped()
                    entry = self.client.info(item['entry']['id'])
                    if entry.name != item['target'] and cloud_names:
                        original = Entry(**item['entry'])
                        if entry.name != original.name or entry.parent_id != original.parent_id:
                            raise CloudError('文件状态在执行前变化：' + entry.name)
                        siblings = self._listdir(entry.parent_id)
                        if any(other.id != entry.id and other.name.casefold()==item['target'].casefold() for other in siblings):
                            raise CloudError('云盘目标名称冲突：' + item['target'])
                        if self._registered_migration:
                            self._mutate_cloud('rename', entry.id, item['target'])
                        else:
                            self.client.rename(entry.id, item['target'])
                        entry = self._mutate_cloud('info', entry.id)
                        if entry.name != item['target']:
                            raise CheckpointError('文件改名结果无法确认：' + item['target'])
                    item['entry'] = asdict(entry)
                    self.registry.save_version(account, self.root, number, version, files)
            for path, content, owner in data:
                if not path.exists() or path.read_bytes()!=content:
                    self._publish(folder, identity, path, content, owner, staged[path])
                else:
                    self.registry.published(path, content)
        self._info(f'本地媒体生成完成 番号={number} 路径={str(folder)!r}')
        archive_changed = False
        if self.archive_loose:
            archive_changed = self._archive_movie(number, plans, cloud_root)
            changed = archive_changed or changed
            self._info(f'网盘整理完成 番号={number} 归档根ID={self._archive_id or cloud_root} 目标={relative!r}')
        for version, files in plans.items():
            self.registry.save_version(account, self.root, number, version, files, complete=True)
        refresh_needed = self.migrations.finish(number, self._policy_signature)
        if refresh_needed or relocated or (movie and (archive_changed or recovered or renamed)):
            self._migrated.add(number)
        self._registered_categories.add(Path(relative).parts[0])
        return 'ok' if changed else 'skip'

    def _mutate_cloud(self, method, *args):
        """Uncertain mutation outcomes must replay before advancing the cursor."""
        self._stopped()
        try:
            return getattr(self.client, method)(*args)
        except (AuthenticationError, RiskError, Stopped):
            raise
        except Exception as exc:
            detail = str(exc) if isinstance(exc, CloudError) else type(exc).__name__
            raise CheckpointError('网盘操作结果未确认，保留未完成位置：'+detail) from None

    def _directory(self, parent, name):
        entries = self._listdir(parent)
        matches = [entry for entry in entries if entry.name.casefold()==name.casefold()]
        if matches:
            if len(matches)!=1 or not matches[0].is_dir or matches[0].name!=name:
                raise CloudError('网盘目录名称冲突：' + name)
            return matches[0].id
        self._stopped()
        cid = self._mutate_cloud('mkdir', name, parent)
        try:
            entry = self._folder_info(cid)
        except DirectoryGone:
            raise CheckpointError('新建网盘目录结果未确认，保留未完成位置') from None
        if not entry.is_dir or entry.parent_id!=parent or entry.name!=name:
            raise CloudError('无法核验新建网盘目录：' + name)
        return cid

    def _archive_movie(self, number, plans, cloud_root):
        source_root = cloud_root
        cloud_root = self._archive_id if self._archive_id is not None else cloud_root
        self.migrations.cloud_intent(number, cloud_root)
        parent = self._directory(cloud_root, Path(self._relative(number)).parts[0])
        siblings = self._listdir(parent)
        matches = [entry for entry in siblings if entry.name.casefold() == number.casefold()]
        source_parents = {item['entry']['parent_id'] for files in plans.values() for item in files}
        old_folders = []
        for cid in source_parents:
            if cid == '0':
                continue
            folder = self._folder_info(cid)
            if self.extract_number(folder.name) == number:
                old_folders.append(folder)
                if folder.parent_id not in self._protected_cloud_roots():
                    old_parent = self._folder_info(folder.parent_id)
                    if self._category_matches(old_parent.name, number):
                        old_folders.append(old_parent)
            elif self._category_matches(folder.name, number):
                old_folders.append(folder)
        changed = False
        # Keep a new single-movie source directory intact when there is no target.
        if not matches and len(source_parents) == 1 and self.policy.layout == 'prefix' and self._archive_id is None:
            source = next(iter(source_parents))
            folder = self._folder_info(source) if source != '0' else None
            if (folder and folder.is_dir and source not in self._protected_cloud_roots() and source != parent
                    and self.extract_number(folder.name) == number
                    and folder.parent_id in (cloud_root, parent)):
                contents = self._listdir(source)
                managed = {item['entry']['id'] for files in plans.values() for item in files}
                if all(not entry.is_dir and (Path(entry.name).suffix.lower() not in VIDEO_EXTS | SUBTITLE_EXTS
                                            or entry.id in managed) for entry in contents):
                    self._stopped()
                    if folder.name != number:
                        self._mutate_cloud('rename', source, number)
                        folder = self._folder_info(source)
                        if folder.name != number:
                            raise CloudError('影片目录改名结果无法确认')
                    if folder.parent_id != parent:
                        self._mutate_cloud('move', [source], parent)
                        folder = self._folder_info(source)
                        if folder.parent_id != parent or folder.name != number:
                            raise CloudError('影片目录移动结果无法确认')
                    matches = [folder]
                    changed = True
        destination = matches[0].id if len(matches) == 1 and matches[0].is_dir and matches[0].name == number else self._directory(parent, number)
        self.migrations.add_cloud_cleanup(number, [entry for entry in old_folders if entry.id not in (destination, parent)])
        self.migrations.cloud_files(number, plans, destination)
        for version, files in plans.items():
            for item in files:
                self._stopped()
                entry = self.client.info(item['entry']['id'])
                if entry.parent_id!=destination:
                    expected = Entry(**item['entry'])
                    if entry.name!=item['target'] or entry.parent_id!=expected.parent_id:
                        raise CloudError('归档前文件位置发生变化：' + entry.name)
                    target = self._listdir(destination)
                    if any(other.id!=entry.id and other.name.casefold()==entry.name.casefold() for other in target):
                        raise CloudError('归档目标文件名冲突：' + entry.name)
                    self._mutate_cloud('move', [entry.id], destination)
                    entry = self._mutate_cloud('info', entry.id)
                    if entry.parent_id!=destination or entry.name!=item['target']:
                        raise CheckpointError('归档结果无法确认：' + item['target'])
                    changed = True
                item['entry'] = asdict(entry)
                self.registry.save_version(self.client.account, self.root, number, version, files)
        with self.registry.db:
            self.registry.db.execute('UPDATE library_movies SET cloud_root=?,cloud_folder=? WHERE account=? AND root=? AND number=?',
                (cloud_root, destination, self.client.account, str(self.root), number))
        self.migrations.add_cloud_cleanup(number, [entry for entry in old_folders if entry.id != destination and entry.id != parent])
        return changed

    @staticmethod
    def _snapshot(entry):
        # Resolve playback data live; checkpoints contain no pickcodes or URLs.
        return dict(id=entry.id, parent_id=entry.parent_id, name=entry.name, is_dir=entry.is_dir)

    def _initial_scan(self, roots):
        bindings = []
        category_roots = {}
        selected = {}
        for root in roots:
            cid = self.client.resolve_folder(root)
            folder = self._folder_info(cid) if cid!='0' else None
            selected[cid] = folder
        archive_roots = {}
        for cid, folder in selected.items():
            name = folder.name if folder else ''
            is_category = self._is_category(name)
            cloud_root = cid
            if folder and is_category:
                category_roots[cid] = folder
            if folder and self.extract_number(name):
                parent = self._folder_info(folder.parent_id) if folder.parent_id != '0' else None
                cloud_root = parent.parent_id if parent and self._is_category(parent.name) else folder.parent_id
            # Overlapping selections share the configured parent, regardless of
            # selection order. Only walk ancestors, never scan their children.
            ancestor = folder.parent_id if folder else None
            walked = {cid}
            while ancestor is not None and ancestor not in walked:
                walked.add(ancestor)
                if ancestor in selected:
                    outer = selected[ancestor]
                    cloud_root = ancestor
                    if outer and self.extract_number(outer.name):
                        parent = self._folder_info(outer.parent_id) if outer.parent_id != '0' else None
                        cloud_root = parent.parent_id if parent and self._is_category(parent.name) else outer.parent_id
                if ancestor == '0':
                    break
                outer = selected[ancestor] if ancestor in selected else self._folder_info(ancestor)
                ancestor = outer.parent_id
            bindings.append([cid, 0, name, cloud_root, False])
            archive_roots[cid] = cloud_root
        return dict(version=ScanProgress.VERSION, bindings=bindings, serial=len(bindings),
                    selected={cid: self._snapshot(folder) if folder else None for cid, folder in selected.items()},
                    archive_roots=archive_roots,
                    category_roots={cid: self._snapshot(folder) for cid, folder in category_roots.items()},
                    queue={str(index): task for index, task in enumerate(bindings)},
                    current=None, seen=[], attempted=[], owners={}, done=False, cycle_id=secrets.token_hex(16))

    def _commit_scan(self, state):
        self._progress.save(state)
        self._scan_state = state

    def _prepare_scan(self, roots, recursive, max_depth, strategy, stats):
        scope = dict(account=self.client.account, plugin_id=self.plugin_id, roots=list(roots),
                     recursive=recursive, max_depth=max_depth, output_root=str(self.root),
                     archive=self.archive_loose, numeric_ids=self.numeric_ids,
                     classification=self.policy.signature, archive_root=self.archive_root, naming_revision=NAMING_REVISION)
        signature = digest(json.dumps(scope, sort_keys=True, ensure_ascii=False).encode())
        self._progress = ScanProgress(self.registry, signature, strategy == 'incremental', self._info)
        state = self._progress.load() if strategy == 'incremental' else None
        if strategy == 'full':
            self._progress.clear()
        stats['scan_resumed'] = bool(state and not state['done'])
        self._resuming_scan = stats['scan_resumed']
        if state is None:
            state = self._initial_scan(roots)
        else:
            # Bind roots by ID before looking at any obsolete source paths.
            state['selected'] = {cid: self._snapshot(self._folder_info(cid)) if cid != '0' else None
                                 for cid in state['selected']}
            if state['done']:
                state.update(queue={str(index): task for index, task in enumerate(state['bindings'])},
                             serial=len(state['bindings']), current=None, seen=[], attempted=[], owners={}, done=False,
                             cycle_id=secrets.token_hex(16))
                self._info('上一轮增量扫描已完成，从头开启新一轮')
        self._scan_root_ids = set(state['selected'])
        self._scan_folders = {cid: Entry(**folder) if folder else None for cid, folder in state['selected'].items()}
        self._scan_archive_roots = state['archive_roots']
        self._scan_depth = max_depth if recursive else 1
        self._origin_ancestors, self._relatives, self._scope_categories = {}, {}, {}
        self._archive_id = None
        if self.archive_loose and self.archive_root:
            self._archive_id = state.get('archive_target')
            if self._archive_id is None:
                self._archive_id = self.client.resolve_folder(self.archive_root)
            if self._archive_id != '0':
                self._folder_info(self._archive_id)
            state['archive_target'] = self._archive_id
        self._excluded_scan_ids = set()
        if self._archive_id is not None and self._archive_id != '0':
            ancestors = self._ancestors(self._archive_id)[1:]
            if self._scan_root_ids.intersection(ancestors):
                self._excluded_scan_ids.add(self._archive_id)
                for cid in self._scan_root_ids:
                    if self._archive_id in self._ancestors(cid):
                        self._excluded_scan_ids.add(cid)
        state.setdefault('cycle_id', secrets.token_hex(16))
        self._policy_signature = digest(json.dumps(dict(policy=self.policy.signature,
            archive=self.archive_loose, target=self._archive_id,
            roots=state['archive_roots'], naming_revision=NAMING_REVISION), sort_keys=True).encode())
        self._attempted = set(state['attempted'])
        self._cycle_movie_roots = dict(state['owners'])
        self._current_entries = None
        self._commit_scan(state)
        self._info(('增量模式恢复扫描位置：' if stats['scan_resumed'] else
                    ('增量模式从头扫描：' if strategy == 'incremental' else '全量模式从头扫描：')) + self._next_position())

    def _next_position(self):
        state = getattr(self, '_scan_state', None)
        if not state or state['done']:
            return '本轮已完成' if state and state['done'] else '扫描根目录'
        current = state['current']
        if current and current['groups']:
            batch = current['groups'][min(current['groups'], key=int)]
            return current['display'] + ' → ' + batch['number']
        if state['queue']:
            task = state['queue'][min(state['queue'], key=int)]
            return task[2] or '/'
        return '本轮已完成'

    def _advance_scan(self):
        state = deepcopy(self._scan_state)
        current = state['current']
        del current['groups'][min(current['groups'], key=int)]
        if not current['groups']:
            state['current'] = None
            self._current_entries = None
        state['attempted'] = sorted(self._attempted)
        state['owners'] = dict(self._cycle_movie_roots)
        state['done'] = not state['queue'] and state['current'] is None
        self._commit_scan(state)

    def _drop_directory(self, key, cid, name, exc):
        """Skip a queued directory that no longer exists, keeping the rest of the queue."""
        state = deepcopy(self._scan_state)
        state['queue'].pop(key, None)
        if cid not in state['seen']:
            state['seen'].append(cid)
        self._commit_scan(state)
        self._error(f'待扫描目录已不存在，已跳过：{name or cid}（{exc}）')

    def _drop_current_directory(self, cid, display, exc):
        """Abandon the directory being processed when it disappears mid-batch."""
        state = deepcopy(self._scan_state)
        state['current'] = None
        if cid not in state['seen']:
            state['seen'].append(cid)
        state['done'] = not state['queue']
        self._current_entries = None
        self._commit_scan(state)
        self._error(f'当前扫描目录已不存在，已跳过：{display or cid}（{exc}）')

    def _check_directory_binding(self, cid):
        """A queued ID can survive a move; its authorization must also survive."""
        visited, chain = set(), []
        original = self._scan_folders.get(cid)
        while cid not in visited:
            if cid in self._scan_root_ids:
                root = self._scan_folders[cid]
                classified = False
                if (len(chain) > self._scan_depth and self._is_category(chain[-1].name) and
                        not (root and self._is_category(root.name))):
                    prefix = chain[-1]
                    classified = any(number and self._category_matches(prefix.name, number)
                                     for number in (self.extract_number(entry.name) for entry in self._listdir(prefix.id)))
                if len(chain) - classified <= self._scan_depth:
                    return original
                raise ScanReadError('待扫描目录已超出配置扫描深度，已保留位置，请核对目录或执行全量扫描')
            if cid == '0':
                break
            visited.add(cid)
            folder = self._folder_info(cid)
            if original is None:
                original = folder
            chain.append(folder)
            cid = folder.parent_id
        raise ScanReadError('待扫描目录已移出配置扫描范围，已保留位置，请核对目录或执行全量扫描')

    def _scan(self, stats):
        """Commit a complete directory snapshot before yielding its movies."""
        directory_path = getattr(self.client, 'directory_path', lambda cid:None)
        while not self._scan_state['done']:
            self._stopped()
            state = self._scan_state
            current = state['current']
            if current is not None:
                group = current['groups'][min(current['groups'], key=int)]
                number = group['number']
                subtitles = [entry for entry in current['media'].values()
                             if Path(entry['name']).suffix.lower() in SUBTITLE_EXTS and
                             self.extract_number(entry['name']) in (None, number)]
                if not (any(fid not in self._attempted for fid in group['videos']) or
                        any(entry['id'] not in self._attempted for entry in subtitles)):
                    self._advance_scan()
                    continue
                if self._current_entries is None or self._current_entries[0] != current['directory']:
                    cid = current['directory']
                    try:
                        folder = self._check_directory_binding(cid)
                        entries = self._listdir(cid)
                    except DirectoryGone as exc:
                        self._drop_current_directory(cid, current['display'], exc)
                        continue
                    self._current_folder_name = folder.name if folder else ''
                    self._current_entries = (cid, {entry.id: entry for entry in entries}, False)
                    self._directory_scope_checks = set()
                yield number, dict(videos=[], entries=[], root=group['root'], directory=current['directory'],
                                   paths={current['display']}, video_ids=group['videos'])
                continue
            if not state['queue']:
                state = deepcopy(state)
                state['done'] = True
                self._commit_scan(state)
                break
            key = min(state['queue'], key=int)
            cid, depth, folder_name, cloud_root, candidate_category = state['queue'][key]
            if cid in state['seen'] or cid in self._excluded_scan_ids:
                state = deepcopy(state)
                del state['queue'][key]
                self._commit_scan(state)
                continue
            # Do not pop the directory until all pagination and its snapshot
            # commit succeed. A read/commit error leaves it at the queue head.
            try:
                if self._resuming_scan:
                    folder = self._check_directory_binding(cid)
                    folder_name = folder.name if folder else ''
                elif cid in self._scan_folders:
                    folder = self._scan_folders[cid]
                    folder_name = folder.name if folder else ''
                entries = self._listdir(cid)
            except DirectoryGone as exc:
                # The directory was deleted or recycled after the checkpoint was
                # written. Skipping it is the only way forward: keeping the head
                # would repeat the same 800001 on every later run.
                self._drop_directory(key, cid, folder_name, exc)
                continue
            state = deepcopy(state)
            del state['queue'][key]
            if candidate_category:
                numbers = [self.extract_number(entry.name) for entry in entries]
                if not any(number and self._category_matches(folder_name, number) for number in numbers):
                    depth += 1
                    if depth > self._scan_depth:
                        state['seen'].append(cid)
                        self._commit_scan(state)
                        continue
            state['seen'].append(cid)
            display = directory_path(cid) or folder_name or '/'
            self._info(('扫描目录已解析：' if depth == 0 else '读取扫描目录：')+repr(display))
            local = defaultdict(list)
            for entry in entries:
                if entry.is_dir:
                    candidate = (depth == 0 and not candidate_category and cloud_root not in state['category_roots']
                                 and self._is_category(entry.name))
                    extra = 0 if candidate else 1
                    if depth+extra <= self._scan_depth:
                        state['queue'][str(state['serial'])] = [entry.id, depth+extra, entry.name, cloud_root, candidate]
                        state['serial'] += 1
                elif Path(entry.name).suffix.lower() in VIDEO_EXTS:
                    number = self.extract_number(entry.name) or self.extract_number(folder_name)
                    self._debug(f'番号识别 目录={display!r} 原文件名={entry.name!r} 文件ID={entry.id} 番号={number or "未识别"}')
                    if not number:
                        if entry.id not in self._attempted:
                            stats['nomatch'] += 1
                            self._attempted.add(entry.id)
                            self._error(f'番号未识别 目录={display!r} 原文件名={entry.name!r} 文件ID={entry.id}')
                    else:
                        local[number].append(entry)
            media = [entry for entry in entries if not entry.is_dir and
                     Path(entry.name).suffix.lower() in VIDEO_EXTS | SUBTITLE_EXTS]
            # A partial move may leave only subtitles in the original folder.
            for entry in media:
                if Path(entry.name).suffix.lower() not in SUBTITLE_EXTS or entry.id in self._attempted:
                    continue
                number = self.extract_number(entry.name) or self.extract_number(folder_name)
                if not number:
                    row = self.registry.db.execute('SELECT number FROM library_files WHERE account=? AND root=? AND fid=?',
                                                   (self.client.account, str(self.root), entry.id)).fetchone()
                    number = row['number'] if row else None
                if number:
                    local.setdefault(number, [])
            groups = {}
            for index, (number, videos) in enumerate(local.items()):
                base = state['category_roots'].get(cloud_root)
                resolved_root = base['parent_id'] if base and self._category_matches(base['name'], number) else cloud_root
                groups[str(index)] = dict(number=number, videos=[entry.id for entry in videos], root=resolved_root)
            if groups:
                state['current'] = dict(directory=cid, display=display,
                                        media={entry.id: self._snapshot(entry) for entry in media}, groups=groups)
            state['attempted'] = sorted(self._attempted)
            self._commit_scan(state)
            self._current_entries = (cid, {entry.id: entry for entry in media}, True) if groups else None
            self._current_folder_name = folder_name
            self._directory_scope_checks = set()

    def _materialize_batch(self, number, context):
        """Rehydrate only snapshot identities; newly inserted files wait a cycle."""
        current = self._scan_state['current']
        cid = current['directory']
        _, live, fresh = self._current_entries
        scope_key = (cid, category(number), context['root'])
        if scope_key not in self._directory_scope_checks:
            self._scope_categories = {}
            if not self._within_scan(cid, number, context['root']):
                raise ScanReadError('恢复目录已移出配置扫描范围，已保留未完成位置')
            self._directory_scope_checks.add(scope_key)
        for fid, snapshot in current['media'].items():
            needed = (fid in context['video_ids'] or
                      (Path(snapshot['name']).suffix.lower() in SUBTITLE_EXTS and
                       self.extract_number(snapshot['name']) in (None, number)))
            if not needed:
                context['entries'].append(Entry(**snapshot))
                continue
            try:
                entry = live.get(fid) or self.client.info(fid)
            except (AuthenticationError, RiskError, Stopped):
                raise
            except Exception:
                if fid in context['video_ids']:
                    raise CloudError('待处理视频已不存在或不可访问：'+snapshot['name']) from None
                self._error('待处理字幕不可用，已跳过：'+snapshot['name'])
                continue
            if entry.is_dir:
                raise CloudError('待处理文件身份已变化')
            if not fresh or entry.parent_id != cid:
                self._scope_categories = {}
                if not self._entry_allowed(entry, number, context['root']):
                    raise CloudError('待处理文件已移出配置扫描范围：'+entry.name)
            if fid in context['video_ids']:
                actual = self.extract_number(entry.name)
                if actual and actual != number and not self.naming.is_pending(number,entry):
                    raise CloudError('待处理视频名称指向其他番号：'+entry.name)
                if not actual and self.extract_number(self._current_folder_name) != number and not self.naming.is_pending(number,entry):
                    raise ScanReadError('快照目录的番号已变化，已保留位置，请执行全量扫描重新识别')
                context['videos'].append(entry)
            context['entries'].append(entry)

    def _is_category(self, name):
        return self.policy.is_category(name, self._registered_categories)

    def _within_scan(self, parent, number, cloud_root):
        """Check current ancestry of registered files without scanning siblings."""
        visited, chain = set(), []
        while parent not in visited:
            if parent in self._scan_root_ids:
                root_folder = self._scan_folders[parent]
                resolved_root = self._scan_archive_roots[parent]
                if root_folder and resolved_root == parent and self._category_matches(root_folder.name, number):
                    resolved_root = root_folder.parent_id
                if self.archive_loose and self._archive_id is None and resolved_root != cloud_root:
                    return False
                classified = False
                if (len(chain) > 1 and self._category_matches(chain[-1].name, number) and
                        not (root_folder and self._is_category(root_folder.name))):
                    # Match _scan's classification test, including the case
                    # where a sibling rather than this path proves the prefix.
                    cid = chain[-1].id
                    if cid not in self._scope_categories:
                        numbers = (self.extract_number(entry.name) for entry in self._listdir(cid))
                        self._scope_categories[cid] = any(n and self._category_matches(chain[-1].name, n) for n in numbers)
                    classified = self._scope_categories[cid]
                if len(chain) - classified <= self._scan_depth:
                    return True
            if parent == '0':
                return False
            visited.add(parent)
            folder = self._folder_info(parent)
            if not folder.is_dir:
                return False
            chain.append(folder)
            parent = folder.parent_id
        return False

    def _reconcile_registered(self, number, context):
        self._scope_categories = {}
        present = {entry.id for entry in context['videos']}
        present.update(entry.id for entry in context['entries'] if Path(entry.name).suffix.lower() in SUBTITLE_EXTS)
        plans = self.registry.versions(self.client.account, self.root, number)
        checked = set()
        scanned_videos = {entry.id for entry in context['videos']}
        fresh_parts = {_source_key(entry) for entry in context['videos'] if _part(entry.name)}
        for plan in plans:
            active_version = any(item['entry']['id'] in present for item in plan['files'])
            active_version = active_version or any(
                item['kind'] == 'video' and (
                    _source_key(Entry(**dict(item['entry'], parent_id=item.get('source_parent', item['entry']['parent_id']))),
                                item.get('source_name')) in fresh_parts or
                    (not scanned_videos and item.get('source_parent', item['entry']['parent_id']) == context['directory']))
                for item in plan['files'])
            for item in plan['files']:
                # Local publication can be complete while a subtitle remains in
                # its source directory after an interrupted/disabled archive.
                if plan['complete'] and not active_version:
                    continue
                fid = item['entry']['id']
                if fid in present or fid in checked:
                    continue
                checked.add(fid)
                try:
                    entry = self.client.info(fid)
                except (AuthenticationError, RiskError, Stopped):
                    raise
                except Exception:
                    self._error(f"历史登记文件不可用 番号={number} 原文件名={item['entry']['name']!r} 文件ID={fid} 原父目录={item['entry']['parent_id']}")
                    continue
                if entry.is_dir or not self._entry_allowed(entry, number, context['root']):
                    self._error(f"历史登记文件位于扫描范围外 番号={number} 原文件名={entry.name!r} 文件ID={fid} 当前父目录={entry.parent_id}")
                    continue
                actual_number = self.extract_number(entry.name)
                if actual_number and actual_number != number and not self.naming.is_pending(number,entry):
                    raise CloudError('已登记视频名称指向其他番号：' + entry.name)
                if item['kind'] == 'video':
                    context['videos'].append(entry)
                context['entries'].append(entry)
                present.add(fid)

    def _run(self, limit, stats):
        def work():
            for number in list(self.migrations.numbers(self._policy_signature, self._scan_state['cycle_id'])):
                if (self.policy.layout == 'prefix' and not self.archive_root and not self.migrations.plan(number)
                        and not self.naming.needs(number)):
                    continue
                yield number, dict(registered=True, videos=[], entries=[], paths={'登记库迁移任务'}, root=None)
            yield from self._scan(stats)
        batches = work()
        outcomes, movie_roots = {}, self._cycle_movie_roots
        priority = {'skip': 0, 'ok': 1, 'notfound': 2, 'fail': 3}
        while stats['ok']+stats['notfound']+stats['fail'] < limit:
            self._stopped()
            try:
                number, context = next(batches)
            except StopIteration:
                break
            self._info(f'开始处理影片 番号={number} 来源={sorted(context["paths"])!r}')
            plan = self.migrations.plan(number) or {}
            self._registered_migration = bool(context.get('registered') and
                (self.policy.layout == 'maker' or self.archive_root or plan.get('local_cleanup') or self.naming.needs(number)))
            try:
                if context.get('registered'):
                    self._registered_batch(number, context)
                previous_root = movie_roots.setdefault(number, context['root'])
                if self.archive_loose and self._archive_id is None and previous_root != context['root']:
                    raise CloudError('同番号来自多个扫描根目录，请选择共同上级目录后再进行网盘整理')
                if not context.get('registered'):
                    self._materialize_batch(number, context)
                    self._reconcile_registered(number, context)
                unique = {entry.id: entry for entry in context['entries']}
                status = self._process_movie(number, context['videos'], list(unique.values()), context['root']) if context['videos'] else 'skip'
            except CheckpointError:
                previous = outcomes.get(number)
                if previous is not None:
                    stats[previous] -= 1
                raise
            except (AuthenticationError, RiskError, Stopped, ScanReadError):
                raise
            except sqlite3.Error as exc:
                raise CheckpointError('媒体登记提交失败（'+type(exc).__name__+'），已保留未完成位置') from None
            except DirectoryGone as exc:
                if context.get('registered'):
                    raise ScanReadError('登记文件的目录不可访问，保留迁移任务') from None
                self._drop_current_directory(context['directory'], sorted(context['paths']), exc)
                continue
            except Exception as exc:
                status = 'fail'
                message = str(exc) if isinstance(exc, CloudError) else type(exc).__name__
                stats['errors'].append(number+'：'+message)
                files = [dict(name=entry.name, id=entry.id, parent=entry.parent_id) for entry in context['videos']]
                self._error(f'影片处理冲突 目录={sorted(context["paths"])!r} 番号={number} 原文件={files!r} 目标路径={str(self.root/movie_relative(number))!r} 原因={message}')
            self._attempted.update(entry.id for entry in context['videos'])
            present_ids = {entry.id for entry in context['entries']}
            for version in self.registry.versions(self.client.account, self.root, number):
                self._attempted.update(item['entry']['id'] for item in version['files']
                                       if item['entry']['id'] in present_ids)
            previous = outcomes.get(number)
            if previous is None:
                stats['total'] += 1
                stats[status] += 1
                outcomes[number] = status
            elif priority[status] > priority[previous]:
                stats[previous] -= 1
                stats[status] += 1
                outcomes[number] = status
            self._debug(f'刮削结果 番号={number} 状态={status}')
            self.migrations.attempted(number, self._scan_state['cycle_id'])
            if context.get('registered'):
                state = deepcopy(self._scan_state)
                state.update(attempted=sorted(self._attempted), owners=dict(movie_roots))
                self._commit_scan(state)
            else:
                self._advance_scan()
        else:
            self._info(f'已达到单次处理上限 {limit} 部，停止扫描后续目录，剩余影片下次执行')
            if not self._scan_state['done']:
                stats['scan_pause_reason'] = '达到单次处理上限，进度已保存' if self._progress.enabled else '达到单次处理上限，全量模式下次从头扫描'
        return stats

    def _registered_batch(self, number, context):
        """Resolve owned file identities without discovering destination subtrees."""
        movie = self.registry.movie(self.client.account, self.root, number)
        source_roots, videos, entries = set(), [], []
        for version in self.registry.versions(self.client.account, self.root, number):
            for item in version['files']:
                self._stopped()
                entry = self.client.info(item['entry']['id'])
                origin = self.migrations.origin(entry.id)
                if origin is None:
                    # Legacy rows have no authorization to an external target.
                    original_parent = item.get('source_parent', entry.parent_id)
                    try:
                        ancestors = self._ancestors(original_parent)
                    except DirectoryGone:
                        raise CloudError('旧登记来源无法核验，已跳过：'+entry.name) from None
                    selected = self._scan_root_ids.intersection(ancestors)
                    if not selected:
                        raise CloudError('旧登记来源无法核验，已跳过：'+entry.name)
                    source = next(cid for cid in ancestors if cid in selected)
                    source_root = self._scan_archive_roots[source]
                    self.migrations.record_origin(entry, source_root, ancestors)
                    origin = self.migrations.origin(entry.id)
                if not self._origin_allowed(origin):
                    raise CloudError('来源已从扫描目录移除，冻结迁移：'+entry.name)
                origin_root = self._origin_root(origin, number)
                source_roots.add(origin_root)
                if not self._entry_allowed(entry, number, origin_root):
                    raise CloudError('已登记文件实际位置无法核验；独立扫描根请选择共同上级或配置统一目标：'+entry.name)
                if entry.is_dir or (self.extract_number(entry.name) not in (None, number) and not self.naming.is_pending(number,entry)):
                    raise CloudError('已登记文件身份或番号变化：'+entry.name)
                entries.append(entry)
                if item['kind'] == 'video':
                    videos.append(entry)
        if len(source_roots) > 1 and self.archive_loose and self._archive_id is None:
            raise CloudError('独立扫描根的版本需要配置共同网盘整理目标')
        context['root'] = next(iter(source_roots), movie['cloud_root'])
        context.update(videos=videos, entries=entries)

    def _render_nfo(self, info, number):
        """Use one deterministic rendering for publication and format upgrades."""
        nfo = ET.fromstring(self.build_nfo(info, number))
        art = nfo.find("art")
        if art is None:
            art = ET.SubElement(nfo, "art")
        for kind, filename in (("poster", "poster.jpg"), ("fanart", "fanart.jpg")):
            node = art.find(kind)
            if node is None:
                node = ET.SubElement(art, kind)
            node.text = filename
        ET.indent(nfo, space="  ")
        return ET.tostring(nfo, encoding="utf-8", xml_declaration=True)

    def _scan_summary(self, stats):
        state = getattr(self, '_scan_state', None)
        stats['scan_cycle_complete'] = bool(state and state['done'])
        stats['library_layout'] = self.policy.layout
        stats['archive_target'] = self.archive_root or '跟随各扫描归档根'
        stats['migrated'] = len(self._migrated)
        stats['scan_next_position'] = ('配置扫描根目录（全量从头）' if stats['scan_strategy'] == 'full'
                                       else self._next_position())
        if stats['scan_cycle_complete']:
            self._info('本轮扫描完成；下次增量任务从头开启新一轮' if stats['scan_strategy'] == 'incremental' else '全量扫描完成')
        else:
            self._info('扫描暂停：'+stats.get('scan_pause_reason', '本次任务结束')+'；下次位置：'+stats['scan_next_position'])

    def run(self, roots, recursive=False, limit=30, max_depth=3, scan_strategy='full'):
        strategy = scan_strategy if scan_strategy in ('full', 'incremental') else 'full'
        stats = dict(total=0, ok=0, skip=0, nomatch=0, notfound=0, fail=0, errors=[],
                     scan_strategy=strategy, scan_resumed=False, scan_cycle_complete=False, scan_pause_reason='')
        self._metadata_cache, self._artwork_cache = {}, {}
        self._refreshed_subtitles, self._attempted = set(), set()
        self._migrated = set()
        self._scan_state = None
        try:
            self._stopped()
            self._prepare_scan(roots, recursive, max_depth, strategy, stats)
            self._run(limit, stats)
        except (AuthenticationError, RiskError) as exc:
            stats['stopped'] = True
            stats['scan_pause_reason'] = str(exc)
            self._scan_summary(stats)
            exc.stats = stats
            raise
        except Stopped as exc:
            stats["stopped"] = True
            stats['scan_pause_reason'] = str(exc) or '任务已停止，保留未完成位置'
        except Exception as exc:
            message = str(exc) if isinstance(exc, CloudError) else type(exc).__name__
            stats.update(stopped=True, scan_pause_reason=message)
            stats['fail'] += 1
            stats['errors'].append(message)
            self._error('扫描已暂停：'+message)
        if self.stop_event.is_set():
            stats["stopped"] = True
            stats['scan_pause_reason'] = stats['scan_pause_reason'] or '任务已停止，保留未完成位置'
        self._scan_summary(stats)
        return stats
