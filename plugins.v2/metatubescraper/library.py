"""Movie-level 115 library, stable versions and classified local publication."""
import hashlib
import json
import os
import re
import tempfile
import threading
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .cloud115 import AuthenticationError, CloudError, Entry, RiskError, Stopped
from .direct import PlaybackService, Registry, SUBTITLE_EXTS, VIDEO_EXTS, _part, checked_path, digest


def category(number):
    prefix = number.rsplit('-', 1)[0]
    return '数字番号' if prefix.isdigit() else prefix


def movie_relative(number):
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9-]*', number):
        raise CloudError('番号不适合作为输出目录')
    return str(Path(category(number))/number)


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
                 plugin_id='MetaTubeScraper', debug=None, info=None, archive_loose=False, error=None):
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
        self._debug = debug or (lambda message: None)
        self._info = info or (lambda message: None)
        self._error = error or (lambda message: None)
        self._cache = {}
        self._play_lock = threading.RLock()

    def _stopped(self):
        if self.stop_event.is_set():
            raise Stopped('任务已停止')

    def _folder(self, number):
        folder = self.root/movie_relative(number)
        checked_path(self.root, folder/'movie.nfo')
        folder.mkdir(parents=True, exist_ok=True)
        return folder

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
        targets = {}
        for entry in videos:
            version = assigned[entry.id]
            previous = known.get(entry.id)
            target = previous[1]['target'] if previous else variant_stem(number, version, _part(entry.name)) + Path(entry.name).suffix.lower()
            if target.casefold() in targets:
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
            for video in videos:
                if video.parent_id != entry.parent_id:
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
            elif (len([video for video in videos if video.parent_id == entry.parent_id]) == 1
                  and len([video for video in entries if video.parent_id == entry.parent_id and Path(video.name).suffix.lower() in VIDEO_EXTS]) == 1
                  and not self.extract_number(entry.name)):
                owner = next(video for video in videos if video.parent_id == entry.parent_id)
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
        relative = movie_relative(number)
        movie = self.registry.movie(account, self.root, number)
        if movie and movie['relative_dir'] != relative:
            raise CloudError('已登记的影片输出位置与番号分类不一致')
        info = movie['metadata'] if movie else None
        if not info or self.force:
            info = self.metadata(number)
        if not info or not info.get('title'):
            return 'notfound'
        plans = self._plans(number, videos, entries)
        self.registry.ensure_movie(account, self.root, number, relative, info)
        folder = self._folder(number)
        identity = folder.stat()
        shared_owner = movie_job(self.root, number)
        shared = {'movie.nfo': self._render_nfo(info, number)}
        for kind, name in (('poster','poster.jpg'), ('fanart','fanart.jpg')):
            old = self.registry.owned_bytes(folder/name, account, shared_owner)
            shared[name] = old if old is not None and not self.force else self.artwork(info, kind)
            if not shared[name]:
                raise CloudError('图片下载失败：' + name)
        data = []
        changed = False
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
                    content = old if old is not None and not self.force else self.client.download_bytes(entry)
                if not content:
                    raise CloudError('字幕下载结果为空：' + name)
                data.append((folder/name, content, owner))
                changed = changed or old != content or entry.name != item['target']
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
            for version, files in plans.items():
                for item in files:
                    self._stopped()
                    entry = self.client.info(item['entry']['id'])
                    if entry.name != item['target']:
                        original = Entry(**item['entry'])
                        if entry.name != original.name or entry.parent_id != original.parent_id:
                            raise CloudError('文件状态在执行前变化：' + entry.name)
                        siblings = self.client.listdir(entry.parent_id)
                        if any(other.id != entry.id and other.name.casefold()==item['target'].casefold() for other in siblings):
                            raise CloudError('云盘目标名称冲突：' + item['target'])
                        self.client.rename(entry.id, item['target'])
                        entry = self.client.info(entry.id)
                        if entry.name != item['target']:
                            raise CloudError('文件改名结果无法确认：' + item['target'])
                    item['entry'] = asdict(entry)
                    self.registry.save_version(account, self.root, number, version, files)
            for path, content, owner in data:
                if not path.exists() or path.read_bytes()!=content:
                    self._publish(folder, identity, path, content, owner, staged[path])
                else:
                    self.registry.published(path, content)
        if self.archive_loose:
            changed = self._archive_movie(number, plans, cloud_root) or changed
        for version, files in plans.items():
            self.registry.save_version(account, self.root, number, version, files, complete=True)
        return 'ok' if changed else 'skip'

    def _directory(self, parent, name):
        entries = self.client.listdir(parent)
        matches = [entry for entry in entries if entry.name.casefold()==name.casefold()]
        if matches:
            if len(matches)!=1 or not matches[0].is_dir or matches[0].name!=name:
                raise CloudError('网盘目录名称冲突：' + name)
            return matches[0].id
        self._stopped()
        cid = self.client.mkdir(name, parent)
        entry = self.client.info(cid)
        if not entry.is_dir or entry.parent_id!=parent or entry.name!=name:
            raise CloudError('无法核验新建网盘目录：' + name)
        return cid

    def _archive_movie(self, number, plans, cloud_root):
        parent = self._directory(cloud_root, category(number))
        siblings = self.client.listdir(parent)
        matches = [entry for entry in siblings if entry.name.casefold() == number.casefold()]
        source_parents = {item['entry']['parent_id'] for files in plans.values() for item in files}
        changed = False
        # Keep a new single-movie source directory intact when there is no target.
        if not matches and len(source_parents) == 1:
            source = next(iter(source_parents))
            folder = self.client.info(source) if source != '0' else None
            if (folder and folder.is_dir and source not in (cloud_root, parent)
                    and self.extract_number(folder.name) == number
                    and folder.parent_id in (cloud_root, parent)):
                contents = self.client.listdir(source)
                managed = {item['entry']['id'] for files in plans.values() for item in files}
                if all(not entry.is_dir and (Path(entry.name).suffix.lower() not in VIDEO_EXTS | SUBTITLE_EXTS
                                            or entry.id in managed) for entry in contents):
                    self._stopped()
                    if folder.name != number:
                        self.client.rename(source, number)
                        folder = self.client.info(source)
                        if folder.name != number:
                            raise CloudError('影片目录改名结果无法确认')
                    if folder.parent_id != parent:
                        self.client.move([source], parent)
                        folder = self.client.info(source)
                        if folder.parent_id != parent or folder.name != number:
                            raise CloudError('影片目录移动结果无法确认')
                    matches = [folder]
                    changed = True
        destination = matches[0].id if len(matches) == 1 and matches[0].is_dir and matches[0].name == number else self._directory(parent, number)
        for version, files in plans.items():
            for item in files:
                self._stopped()
                entry = self.client.info(item['entry']['id'])
                if entry.parent_id!=destination:
                    expected = Entry(**item['entry'])
                    if entry.name!=item['target'] or entry.parent_id!=expected.parent_id:
                        raise CloudError('归档前文件位置发生变化：' + entry.name)
                    target = self.client.listdir(destination)
                    if any(other.id!=entry.id and other.name.casefold()==entry.name.casefold() for other in target):
                        raise CloudError('归档目标文件名冲突：' + entry.name)
                    self.client.move([entry.id], destination)
                    entry = self.client.info(entry.id)
                    if entry.parent_id!=destination or entry.name!=item['target']:
                        raise CloudError('归档结果无法确认：' + item['target'])
                    changed = True
                item['entry'] = asdict(entry)
                self.registry.save_version(self.client.account, self.root, number, version, files)
        with self.registry.db:
            self.registry.db.execute('UPDATE library_movies SET cloud_root=?,cloud_folder=? WHERE account=? AND root=? AND number=?',
                (cloud_root, destination, self.client.account, str(self.root), number))
        return changed

    def _scan(self, roots, recursive, max_depth, stats):
        queue = []
        category_roots = {}
        for root in roots:
            cid = self.client.resolve_folder(root)
            folder = self.client.info(cid) if cid!='0' else None
            name = folder.name if folder else ''
            is_category = self._is_category(name)
            cloud_root = cid
            if folder and is_category:
                category_roots[cid] = folder
            if folder and self.extract_number(name):
                parent = self.client.info(folder.parent_id) if folder.parent_id != '0' else None
                cloud_root = parent.parent_id if parent and self._is_category(parent.name) else folder.parent_id
            queue.append((cid, 0, name, cloud_root, False))
        seen, movies = set(), defaultdict(lambda:dict(videos=[], entries=[], root=None, roots=set(), paths=set()))
        directory_path = getattr(self.client, 'directory_path', lambda cid:None)
        while queue:
            self._stopped()
            cid, depth, folder_name, cloud_root, candidate_category = queue.pop(0)
            if cid in seen:
                continue
            entries = self.client.listdir(cid)
            if candidate_category:
                numbers = [self.extract_number(entry.name) for entry in entries]
                if not any(number and category(number) == folder_name for number in numbers):
                    depth += 1
                    if depth > (max_depth if recursive else 1):
                        continue
            seen.add(cid)
            display = directory_path(cid) or folder_name or '/'
            self._info(('扫描目录已解析：' if depth == 0 else '读取扫描目录：')+repr(display))
            local = defaultdict(list)
            for entry in entries:
                if entry.is_dir:
                    candidate = (depth == 0 and not candidate_category and cloud_root not in category_roots
                                 and self._is_category(entry.name))
                    extra = 0 if candidate else 1
                    if depth+extra <= (max_depth if recursive else 1):
                        queue.append((entry.id, depth+extra, entry.name, cloud_root, candidate))
                elif Path(entry.name).suffix.lower() in VIDEO_EXTS:
                    number = self.extract_number(entry.name) or self.extract_number(folder_name)
                    self._debug(f'番号识别 目录={display!r} 原文件名={entry.name!r} 文件ID={entry.id} 番号={number or "未识别"}')
                    if not number:
                        stats['nomatch'] += 1
                        self._error(f'番号未识别 目录={display!r} 原文件名={entry.name!r} 文件ID={entry.id}')
                    else:
                        local[number].append(entry)
            for number,videos in local.items():
                movie = movies[number]
                movie['videos'].extend(videos)
                movie['entries'].extend(entry for entry in entries if not entry.is_dir and
                                        (Path(entry.name).suffix.lower() in VIDEO_EXTS | SUBTITLE_EXTS))
                base = category_roots.get(cloud_root)
                resolved_root = base.parent_id if base and base.name == category(number) else cloud_root
                movie['root'] = movie['root'] or resolved_root
                movie['roots'].add(resolved_root)
                movie['paths'].add(display)
        return movies, seen

    @staticmethod
    def _is_category(name):
        return name=='数字番号' or bool(re.fullmatch(r'[A-Z]+(?:-[A-Z]+)*|\d{2,3}[A-Z]{2,6}', name))

    def _reconcile_registered(self, number, context, seen):
        present = {entry.id for entry in context['videos']}
        present.update(entry.id for entry in context['entries'] if Path(entry.name).suffix.lower() in SUBTITLE_EXTS)
        plans = self.registry.versions(self.client.account, self.root, number)
        checked = set()
        scanned_videos = {entry.id for entry in context['videos']}
        for plan in plans:
            active_version = any(item['kind'] == 'video' and item['entry']['id'] in scanned_videos
                                 for item in plan['files'])
            for item in plan['files']:
                # Local publication can be complete while a subtitle remains in
                # its source directory after an interrupted/disabled archive.
                if plan['complete'] and (item['kind'] == 'video' or not active_version):
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
                if entry.is_dir or entry.parent_id not in seen:
                    self._error(f"历史登记文件位于扫描范围外 番号={number} 原文件名={entry.name!r} 文件ID={fid} 当前父目录={entry.parent_id}")
                    continue
                actual_number = self.extract_number(entry.name)
                if actual_number and actual_number != number:
                    raise CloudError('已登记视频名称指向其他番号：' + entry.name)
                if item['kind'] == 'video':
                    context['videos'].append(entry)
                context['entries'].append(entry)
                present.add(fid)

    def _run(self, roots, recursive, limit, max_depth, stats):
        movies, seen = self._scan(roots, recursive, max_depth, stats)
        for number, context in movies.items():
            self._stopped()
            if stats['ok']+stats['notfound']+stats['fail'] >= limit:
                break
            stats['total'] += 1
            try:
                if self.archive_loose and len(context['roots']) > 1:
                    raise CloudError('同番号来自多个扫描根目录，请选择共同上级目录后再进行网盘整理')
                self._reconcile_registered(number, context, seen)
                unique = {entry.id: entry for entry in context['entries']}
                status = self._process_movie(number, context['videos'], list(unique.values()), context['root'])
            except (AuthenticationError, RiskError, Stopped):
                raise
            except Exception as exc:
                status = 'fail'
                message = str(exc) if isinstance(exc, CloudError) else type(exc).__name__
                stats['errors'].append(number+'：'+message)
                files = [dict(name=entry.name, id=entry.id, parent=entry.parent_id) for entry in context['videos']]
                self._error(f'影片处理冲突 目录={sorted(context["paths"])!r} 番号={number} 原文件={files!r} 目标路径={str(self.root/movie_relative(number))!r} 原因={message}')
            stats[status] += 1
            self._debug(f'刮削结果 番号={number} 状态={status}')
        return stats

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

    def run(self, roots, recursive=False, limit=30, max_depth=3):
        stats = dict(total=0, ok=0, skip=0, nomatch=0, notfound=0, fail=0, errors=[])
        try:
            self._run(roots, recursive, limit, max_depth, stats)
        except (AuthenticationError, RiskError) as exc:
            exc.stats = stats
            raise
        except Stopped:
            stats["stopped"] = True
        if self.stop_event.is_set():
            stats["stopped"] = True
        return stats
