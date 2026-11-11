"""Persistent display slots and journaled filename changes; version IDs stay fixed."""
import re
import secrets
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

from .cloud115 import CloudError, Entry
from .direct import VIDEO_EXTS, _part
from .scan_progress import CheckpointError

REVISION = 1


def display_stem(number, index, part=''):
    return number + (f' - V{index:04d}' if index else '') + part


def slot(name, number):
    path = Path(name)
    if path.suffix.lower() not in VIDEO_EXTS | {'.strm'}:
        return None
    match = re.fullmatch(re.escape(number)+r'(?: - V(\d+))?(?:-CD\d+)?', path.stem, re.I)
    return int(match[1] or 0) if match else None


class NamingManager:
    def __init__(self, service):
        self.s, self.r = service, service.registry
        with self.r._lock, self.r.db:
            self.r.db.execute('''CREATE TABLE IF NOT EXISTS library_version_names (
                account TEXT, root TEXT, number TEXT, version INTEGER, display_index INTEGER, revision INTEGER,
                PRIMARY KEY(account,root,number,version), UNIQUE(account,root,number,display_index))''')

    @property
    def identity(self):
        return self.s.client.account, str(self.s.root)

    def mapping(self, number):
        return {row['version']:row['display_index'] for row in self.r.db.execute(
            'SELECT version,display_index FROM library_version_names WHERE account=? AND root=? AND number=?',
            (*self.identity, number))}

    def job(self, number):
        return (self.s.migrations.plan(number) or {}).get('naming')

    def save(self, number, job):
        plan = self.s.migrations.plan(number) or dict(stage='naming_pending', local=[],
            local_cleanup=[], cloud_targets=[], cloud_cleanup=[], cloud_files=[], relative=self.s._relative(number))
        plan['naming'] = job
        self.s.migrations.save(number, plan)

    def needs(self, number):
        known = self.mapping(number)
        if any(v['version'] not in known for v in self.r.versions(*self.identity, number)):
            return True
        job = self.job(number)
        return bool(job and (any(i['phase'] != 'done' for i in job['local']) or
                             ((self.s.archive_loose or not job.get('requires_archive',True)) and
                              any(i['phase'] != 'done' for i in job['cloud']))))

    def pending_entry(self, number, entry):
        for item in (self.job(number) or {}).get('cloud', []):
            if item['id'] == entry.id and entry.name == item['temp']:
                return replace(entry, name=item['source'])
        return entry

    def is_pending(self, number, entry):
        return self.pending_entry(number, entry).name != entry.name

    def destination(self, number, cloud_root):
        parent = self.s._archive_id if self.s._archive_id is not None else cloud_root
        for name in Path(self.s._relative(number)).parts:
            matches = [e for e in self.s._listdir(parent) if e.name.casefold() == name.casefold()]
            if not matches:
                return None
            if len(matches) != 1 or not matches[0].is_dir or matches[0].name != name:
                raise CloudError('网盘目录名称冲突：'+name)
            parent = matches[0].id
        return parent

    def allocate(self, number, plans, cloud_root):
        existing = self.r.versions(*self.identity, number)
        all_files = [item for v in existing for item in v['files']]
        all_files += [item for files in plans.values() for item in files]
        managed = {item['entry']['id'] for item in all_files}
        parents = {item['entry']['parent_id'] for files in plans.values() for item in files}
        destination = self.destination(number, cloud_root)
        if destination is not None:
            parents.add(destination)
        occupied = set()
        for parent in parents:
            for entry in self.s._listdir(parent):
                index = slot(entry.name, number)
                if not entry.is_dir and entry.id not in managed and index is not None:
                    occupied.add(index)
        owners = {self.s._version_owner(number, v['version']) for v in existing}
        owned_paths = {row['path'] for row in self.r.db.execute('SELECT path,account,job FROM outputs')
                       if row['account'] == self.s.client.account and row['job'] in owners}
        movie = self.r.movie(*self.identity, number)
        folders = {self.s.root/self.s._relative(number)}
        if movie:
            folders.add(self.s.root/movie['relative_dir'])
        for folder in folders:
            # checked_path also rejects symlinked or replaced directory parents.
            self.s._checked_name(folder/'movie.nfo')
            if folder.exists():
                for path in folder.iterdir():
                    index = slot(path.name, number)
                    if index is not None and str(path) not in owned_paths:
                        occupied.add(index)
        try:
            with self.r._lock, self.r.db:
                self.r.check_identity()
                self.r.db.execute('BEGIN IMMEDIATE')
                mapping = self.mapping(number)
                if occupied.intersection(mapping.values()):
                    raise CloudError('已登记版本名称被外部文件占用，保留原件：'+number)
                used = occupied | set(mapping.values())
                versions = set(plans)
                versions.update(v['version'] for v in existing)
                base = {v for v, files in plans.items() if any(
                    item['kind'] == 'video' and slot(item['entry']['name'], number) == 0 for item in files)}
                for version in sorted(versions, key=lambda v:(v not in base, v)):
                    if version in mapping:
                        continue
                    index = 0
                    while index in used:
                        index += 1
                    self.r.db.execute('INSERT INTO library_version_names VALUES(?,?,?,?,?,?)',
                                      (*self.identity, number, version, index, REVISION))
                    mapping[version] = index
                    used.add(index)
                return mapping
        except sqlite3.Error as exc:
            raise CheckpointError('文件名编号提交失败（'+type(exc).__name__+'）') from None

    def _preflight_local(self, job):
        sources = {item['source'] for item in job['local'] if item['phase'] == 'planned'}
        for item in job['local']:
            if item['phase'] == 'done':
                continue
            source = item['source'] if item['phase'] == 'planned' else item['temp']
            self.s.migrations._owned(source, item['owner'])
            if item['target'] not in sources:
                self.s.migrations._owned(item['target'], item['owner'])

    def recover(self, number, cloud):
        job = self.job(number)
        if not job:
            return False
        local_changed = any(i['phase'] != 'done' for i in job['local'])
        self._preflight_local(job)
        for phase, source, target in [('planned','source','temp'), ('staged','temp','target')]:
            for item in job['local']:
                if item['phase'] != phase:
                    continue
                try:
                    self.s._stopped()
                    self.s.migrations._move_file(item[source], item[target], item['owner'])
                except OSError as exc:
                    raise CheckpointError('本地改名未完成（'+type(exc).__name__+'），保留日志') from None
                item['phase'] = 'staged' if phase == 'planned' else 'done'
                self.save(number, job)
                if Path(item['target']).suffix == '.strm':
                    self.s._library_changed.add(number)
        changed = local_changed
        if cloud:
            managed = {item['entry']['id'] for v in self.r.versions(*self.identity, number) for item in v['files']}
            managed.update(i['id'] for i in job['cloud'])
            for item in job['cloud']:
                if item['phase'] == 'done':
                    continue
                entry = self.s.client.info(item['id'])
                if entry.parent_id != item['parent'] or entry.name not in (item['source'],item['temp'],item['target']):
                    raise CloudError('待改名文件身份或位置变化：'+entry.name)
                for other in self.s._listdir(entry.parent_id):
                    if other.id not in managed and other.name.casefold() in (item['temp'].casefold(),item['target'].casefold()):
                        raise CloudError('网盘改名目标被占用：'+item['target'])
            for phase, target in [('planned','temp'), ('staged','target')]:
                for item in job['cloud']:
                    if item['phase'] != phase:
                        continue
                    entry = self.s.client.info(item['id'])
                    if entry.name == item['target']:
                        item['phase'] = 'done'
                    else:
                        destination = 'target' if item.get('direct') else target
                        if entry.name != item[destination]:
                            self.s._mutate_cloud('rename', entry.id, item[destination])
                        entry = self.s._mutate_cloud('info', entry.id)
                        if entry.name != item[destination] or entry.parent_id != item['parent']:
                            raise CheckpointError('网盘文件改名未确认，保留日志')
                        item['phase'] = 'staged' if phase == 'planned' and not item.get('direct') else 'done'
                    changed = True
                    self.save(number, job)
        job['cloud_deferred'] = bool(job.get('requires_archive',True) and any(i['phase'] != 'done' for i in job['cloud']))
        self.save(number, job)
        return changed

    def apply(self, number, plans, cloud_root, cloud):
        mapping = self.allocate(number, plans, cloud_root)
        old = {item['entry']['id']:(v['version'],item) for v in self.r.versions(*self.identity, number) for item in v['files']}
        new_video_stems = {}
        for version, files in plans.items():
            for item in files:
                if item['kind'] == 'video':
                    entry = Entry(**item['entry'])
                    new = display_stem(number,mapping[version],_part(entry.name))+Path(entry.name).suffix
                    new_video_stems[item['target']] = new
        desired = {}
        for version, files in plans.items():
            for item in files:
                entry = Entry(**item['entry'])
                if item['kind'] == 'video':
                    new = new_video_stems[item['target']]
                else:
                    candidates = sorted([key for key in new_video_stems if Path(item['target']).stem.startswith(Path(key).stem)],
                                        key=len, reverse=True)
                    if not candidates:
                        raise CloudError('已登记字幕缺少可核验的视频名称：'+item['target'])
                    source = candidates[0]
                    new = Path(new_video_stems[source]).stem+item['target'][len(Path(source).stem):]
                desired[entry.id] = (version,item,new)
        local = []
        for fid, (version,item,new) in desired.items():
            previous = old.get(fid)
            if not previous:
                continue
            before = previous[1]['target']
            before = Path(before).stem+'.strm' if item['kind']=='video' else before
            after = Path(new).stem+'.strm' if item['kind']=='video' else new
            if before == after:
                continue
            owner = self.s._version_owner(number, version)
            for row in self.r.db.execute('SELECT * FROM outputs WHERE account=? AND job=?', (self.s.client.account,owner)):
                source = Path(row['path'])
                if source.name == before:
                    local.append(dict(source=str(source),target=str(source.with_name(after)),owner=owner,
                                      digest=row['digest'] or row['pending'],phase='planned'))
        nonce = secrets.token_hex(10)
        cloud_items = []
        pending = {i['id']:i for i in (self.job(number) or {}).get('cloud',[]) if i['phase'] != 'done'}
        for fid, (_,item,new) in desired.items():
            entry = self.s.client.info(fid)
            if entry.name != new:
                if fid in pending:
                    if pending[fid]['target'] != new:
                        raise CloudError('未完成改名的目标发生变化，需先恢复原计划')
                    cloud_items.append(pending[fid])
                else:
                    cloud_items.append(dict(id=fid,parent=entry.parent_id,source=entry.name,target=new,
                        temp='.metatube-rename-'+nonce+'-'+fid+Path(entry.name).suffix,phase='planned'))
        dependency = any(a['id'] != b['id'] and a['parent']==b['parent'] and a['target'].casefold()==b['source'].casefold()
                         for a in cloud_items for b in cloud_items)
        for item in cloud_items:
            item.setdefault('direct',not dependency)
        for index,item in enumerate(local):
            item['temp'] = str(Path(item['source']).with_name('.metatube-rename-'+nonce+'-'+str(index)+'.tmp'))
        job = dict(revision=REVISION, local=local, cloud=cloud_items,
                   requires_archive=not (cloud and not self.s.archive_loose),
                   cloud_deferred=bool(cloud_items and not cloud))
        # Validate all source digests and final destinations before changing any file.
        self._preflight_local(job)
        self.save(number, job)
        # Persist the complete file association before later downloads can fail.
        # The journal above retains old paths while these rows adopt new names.
        for version, files in plans.items():
            for item in files:
                item['target'] = desired[item['entry']['id']][2]
            self.r.save_version(*self.identity, number, version, files)
        changed = self.recover(number, False)
        for fid, (_,item,new) in desired.items():
            item['target'] = new
            item['entry'] = asdict(self.s.client.info(fid))
        if changed:
            self.s._info(f'影片命名已调整 番号={number} 文件名={[value[2] for value in desired.values()]!r}')
        return plans, changed
