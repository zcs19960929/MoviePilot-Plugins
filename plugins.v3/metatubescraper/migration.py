"""Journaled relocation of owned outputs and file-level source provenance."""
import errno
import json
import os
import sqlite3
from pathlib import Path

from .cloud115 import CloudError, NotFoundError, AuthenticationError, RiskError, Stopped
from .direct import checked_path, digest
from .scan_progress import CheckpointError


class MigrationManager:
    def __init__(self, service):
        self.service, self.registry = service, service.registry
        with self.registry._lock, self.registry.db:
            self.registry.check_identity()
            self.registry.db.executescript('''
                CREATE TABLE IF NOT EXISTS library_origins (
                    account TEXT, root TEXT, fid TEXT, source_root TEXT, ancestors TEXT,
                    PRIMARY KEY(account,root,fid));
                CREATE TABLE IF NOT EXISTS library_migrations (
                    account TEXT, root TEXT, number TEXT, plan TEXT,
                    PRIMARY KEY(account,root,number));
                CREATE TABLE IF NOT EXISTS library_policies (
                    account TEXT, root TEXT, number TEXT, signature TEXT,
                    PRIMARY KEY(account,root,number));
                CREATE TABLE IF NOT EXISTS library_migration_attempts (
                    account TEXT, root TEXT, number TEXT, cycle TEXT,
                    PRIMARY KEY(account,root,number));
                CREATE TABLE IF NOT EXISTS library_categories (
                    account TEXT, root TEXT, name TEXT,
                    PRIMARY KEY(account,root,name));
            ''')

    @property
    def identity(self):
        return self.service.client.account, str(self.service.root)

    def plan(self, number):
        self.registry.check_identity()
        row = self.registry.db.execute('SELECT plan FROM library_migrations WHERE account=? AND root=? AND number=?',
                                       (*self.identity, number)).fetchone()
        return json.loads(row['plan']) if row else None

    def save(self, number, plan):
        try:
            with self.registry._lock, self.registry.db:
                self.registry.check_identity()
                self.registry.db.execute('INSERT INTO library_migrations VALUES(?,?,?,?) ON CONFLICT(account,root,number) DO UPDATE SET plan=excluded.plan',
                                         (*self.identity, number, json.dumps(plan, ensure_ascii=False)))
        except sqlite3.Error as exc:
            raise CheckpointError('迁移日志提交失败（'+type(exc).__name__+'），已保留未完成位置') from None

    def record_origin(self, entry, source_root, ancestors):
        try:
            with self.registry._lock, self.registry.db:
                self.registry.check_identity()
                self.registry.db.execute('INSERT OR IGNORE INTO library_origins VALUES(?,?,?,?,?)',
                                         (*self.identity, entry.id, source_root, json.dumps(ancestors)))
        except sqlite3.Error as exc:
            raise CheckpointError('来源登记失败（'+type(exc).__name__+'）') from None

    def origin(self, fid):
        row = self.registry.db.execute('SELECT source_root,ancestors FROM library_origins WHERE account=? AND root=? AND fid=?',
                                       (*self.identity, fid)).fetchone()
        return dict(source_root=row['source_root'], ancestors=json.loads(row['ancestors'])) if row else None

    def attempted(self, number, cycle):
        with self.registry._lock, self.registry.db:
            self.registry.check_identity()
            self.registry.db.execute('INSERT INTO library_migration_attempts VALUES(?,?,?,?) ON CONFLICT(account,root,number) DO UPDATE SET cycle=excluded.cycle',
                                     (*self.identity, number, cycle))

    def numbers(self, signature, cycle):
        rows = self.registry.db.execute('''SELECT m.number,p.signature,j.plan,a.cycle FROM library_movies m
            LEFT JOIN library_policies p USING(account,root,number)
            LEFT JOIN library_migrations j USING(account,root,number)
            LEFT JOIN library_migration_attempts a USING(account,root,number)
            WHERE m.account=? AND m.root=? ORDER BY m.rowid''', self.identity)
        pending = []
        for row in rows:
            if row['cycle'] == cycle:
                continue
            plan = json.loads(row['plan']) if row['plan'] else None
            if row['signature'] != signature or (plan and plan['stage'] != 'done'):
                pending.append((0 if plan and plan['stage'] != 'done' else 1, row['number']))
        for _, number in sorted(pending, key=lambda item:item[0]):
            yield number

    def categories(self):
        rows = self.registry.db.execute('SELECT relative_dir FROM library_movies WHERE account=? AND root=?', self.identity)
        names = {Path(row['relative_dir']).parts[0] for row in rows if len(Path(row['relative_dir']).parts) == 2}
        names.update(row['name'] for row in self.registry.db.execute(
            'SELECT name FROM library_categories WHERE account=? AND root=?', self.identity))
        for row in self.registry.db.execute('SELECT plan FROM library_migrations WHERE account=? AND root=?', self.identity):
            plan = json.loads(row['plan'])
            for item in plan.get('local', []):
                for key in ('source', 'target'):
                    path = Path(item[key])
                    try:
                        parts = path.relative_to(self.service.root).parts
                    except ValueError:
                        continue
                    if len(parts) == 3:
                        names.add(parts[0])
        return names

    def _owned(self, path, owner):
        path = Path(path)
        checked_path(self.service.root, path)
        self.registry.check_output(path, self.service.client.account, owner)
        return path.read_bytes() if path.is_file() else None

    def _acknowledge(self, source, target, owner):
        """Only acknowledge a verified intended move, never finish an old plan."""
        source, target = Path(source), Path(target)
        old = self._owned(source, owner)
        new = self._owned(target, owner)
        if new is None:
            return
        if old is not None and old != new:
            raise CloudError('迁移恢复内容冲突：'+str(source))
        if old is not None and source != target:
            self.service._stopped()
            source.unlink()
        with self.registry._lock, self.registry.db:
            self.registry.check_identity()
            self.registry.db.execute('UPDATE outputs SET digest=?,pending=NULL WHERE path=?', (digest(new), str(target)))
            if source != target:
                self.registry.db.execute('DELETE FROM outputs WHERE path=? AND account=? AND job=?',
                                         (str(source), self.service.client.account, owner))

    def _move_file(self, source, target, owner):
        source, target = Path(source), Path(target)
        old = self._owned(source, owner)
        new = self._owned(target, owner)
        if old is None:
            if new is not None:
                self._acknowledge(source, target, owner)
            return
        if new is not None and new != old:
            raise CloudError('迁移目标内容冲突：'+str(target))
        self.registry.reserve(target, self.service.client.account, owner, old)
        self.service._stopped()
        if new is None:
            # A hard link never overwrites a concurrently created destination.
            os.link(source, target, follow_symlinks=False)
        self._acknowledge(source, target, owner)

    def relocate_local(self, number, relative, owners):
        previous = self.plan(number)
        unfinished = previous and previous.get('stage') != 'done'
        cleanup = list(previous.get('local_cleanup', [])) if unfinished else []
        identities = dict(previous.get('local_cleanup_identities', {})) if unfinished else {}
        cleaned = list(previous.get('local_cleanup_done', [])) if unfinished else []

        def remember_directory(path):
            raw = str(path)
            if raw not in cleanup:
                cleanup.append(raw)
            try:
                stat = path.stat(follow_symlinks=False)
                identities[raw] = [stat.st_dev, stat.st_ino]
            except FileNotFoundError:
                identities[raw] = None
            if raw in cleaned:
                cleaned.remove(raw)
        if previous and previous.get('stage') not in ('local_complete', 'cleanup', 'done'):
            for item in previous.get('local', []):
                self._acknowledge(item['source'], item['target'], item['owner'])
        placeholders = ','.join('?' for _ in owners)
        rows = list(self.registry.db.execute('SELECT * FROM outputs WHERE account=? AND job IN ('+placeholders+')',
                                             (self.service.client.account, *owners)))
        folder = self.service.root/relative
        checked_path(self.service.root, folder/'movie.nfo')
        moves = []
        for row in rows:
            if row['job'] not in owners:
                continue
            source = Path(row['path'])
            if source.parent.name != number:
                raise CloudError('登记文件不在本片目录内：'+str(source))
            target = folder/source.name
            if source == target:
                self._owned(source, row['job'])
                continue
            old = self._owned(source, row['job'])
            new = self._owned(target, row['job'])
            if old is not None and new is not None and old != new:
                raise CloudError('迁移目标内容冲突：'+str(target))
            moves.append(dict(source=str(source), target=str(target), owner=row['job'],
                              digest=row['digest'], pending=row['pending']))
            remember_directory(source.parent)
            remember_directory(source.parent.parent)
        movie = self.registry.movie(*self.identity, number)
        if movie and movie['relative_dir'] != relative:
            old_folder = self.service.root/movie['relative_dir']
            remember_directory(old_folder)
            remember_directory(old_folder.parent)
        plan = dict(stage='local_pending', relative=relative, local=moves,
                    local_cleanup=list(dict.fromkeys(cleanup)),
                    local_cleanup_identities=identities, local_cleanup_done=cleaned,
                    refresh_needed=bool(moves or (movie and movie['relative_dir'] != relative) or
                                        (unfinished and previous.get('refresh_needed'))),
                    cloud_cleanup=previous.get('cloud_cleanup', []) if unfinished else [],
                    cloud_targets=previous.get('cloud_targets', []) if previous else [],
                    cloud_files=previous.get('cloud_files', []) if previous else [])
        if previous and 'naming' in previous:
            plan['naming'] = previous['naming']
        self.save(number, plan)
        with self.registry._lock, self.registry.db:
            self.registry.check_identity()
            history = {Path(relative).parts[0]}
            history.update(Path(raw).relative_to(self.service.root).parts[0] for raw in cleanup)
            self.registry.db.executemany('INSERT OR IGNORE INTO library_categories VALUES(?,?,?)',
                                         [(*self.identity, name) for name in history])
        folder.mkdir(parents=True, exist_ok=True)
        for item in moves:
            try:
                self._move_file(item['source'], item['target'], item['owner'])
            except OSError as exc:
                raise CheckpointError('本地迁移未完成（'+type(exc).__name__+'），原文件及迁移日志已保留') from None
        try:
            with self.registry._lock, self.registry.db:
                self.registry.check_identity()
                self.registry.db.execute('UPDATE library_movies SET relative_dir=? WHERE account=? AND root=? AND number=?',
                                         (relative, *self.identity, number))
                plan['stage'] = 'local_complete'
                self.registry.db.execute('INSERT INTO library_migrations VALUES(?,?,?,?) ON CONFLICT(account,root,number) DO UPDATE SET plan=excluded.plan',
                                         (*self.identity, number, json.dumps(plan, ensure_ascii=False)))
        except sqlite3.Error as exc:
            raise CheckpointError('迁移位置提交失败（'+type(exc).__name__+'）') from None
        return bool(moves or (movie and movie['relative_dir'] != relative))

    def add_cloud_cleanup(self, number, entries):
        plan = self.plan(number)
        known = {item['id'] for item in plan['cloud_cleanup']}
        for entry in entries:
            if entry and entry.id not in known:
                plan['cloud_cleanup'].append(dict(id=entry.id, parent_id=entry.parent_id, name=entry.name))
                known.add(entry.id)
        self.save(number, plan)

    def cloud_intent(self, number, target):
        plan = self.plan(number)
        movie = self.registry.movie(*self.identity, number)
        if movie and movie['cloud_root'] and movie['cloud_root'] != target:
            plan['refresh_needed'] = True
        if target not in plan['cloud_targets']:
            plan['cloud_targets'].append(target)
        plan['stage'] = 'cloud_pending'
        self.save(number, plan)

    def cloud_files(self, number, plans, destination):
        plan = self.plan(number)
        plan['cloud_files'] = [dict(id=item['entry']['id'], source=item['entry']['parent_id'],
            target=destination, name=item['target'], kind=item['kind'], version=version,
            origin=self.origin(item['entry']['id'])) for version, files in plans.items() for item in files]
        self.save(number, plan)

    def finish(self, number, signature):
        plan = self.plan(number)
        plan['stage'] = 'cleanup'
        self.save(number, plan)
        if self.service.cleanup_empty_dirs:
            for raw in sorted(set(plan['local_cleanup']), key=lambda name:len(Path(name).parts), reverse=True):
                if raw in plan['local_cleanup_done']:
                    continue
                path = Path(raw)
                try:
                    relative = path.relative_to(self.service.root)
                except ValueError:
                    raise CloudError('旧目录越出本地根目录') from None
                if len(relative.parts) not in (1, 2) or path.is_symlink():
                    continue
                if path.resolve() != path or self.service.root.resolve() != self.service.root:
                    raise CloudError('旧目录路径已被替换，停止清理')
                if path == self.service.root/plan['relative'] or path == (self.service.root/plan['relative']).parent:
                    continue
                try:
                    self.service._stopped()
                    stat = path.stat(follow_symlinks=False)
                    if plan['local_cleanup_identities'].get(raw) == [stat.st_dev, stat.st_ino]:
                        path.rmdir()
                        self.service._info('已清理本地旧空目录：'+str(path))
                    else:
                        self.service._info('旧目录身份已变化，保留：'+str(path))
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                        raise CheckpointError('旧空目录清理未完成（'+type(exc).__name__+'）') from None
                plan['local_cleanup_done'].append(raw)
                self.save(number, plan)
            if self.service.archive_loose:
                protected = self.service._protected_cloud_roots()
                for item in plan['cloud_cleanup']:
                    if item.get('done') or item['id'] in protected:
                        continue
                    try:
                        self.service._stopped()
                        removed = self.service.client.recycle_empty_directory(
                            item['id'], item['parent_id'], item['name'], protected)
                        self.service._info(('已移入回收站：' if removed else '保留非空旧目录：')+item['name'])
                    except NotFoundError:
                        pass
                    except (AuthenticationError, RiskError, Stopped):
                        raise
                    except Exception as exc:
                        # The recycle may already have succeeded. Keep the
                        # candidate until a later ID lookup confirms that.
                        raise CheckpointError('旧空目录回收未确认，保留日志：'+(str(exc) if isinstance(exc, CloudError) else type(exc).__name__)) from None
                    item['done'] = True
                    self.save(number, plan)
        plan['stage'] = 'done'
        try:
            with self.registry._lock, self.registry.db:
                self.registry.check_identity()
                self.registry.db.execute('INSERT INTO library_migrations VALUES(?,?,?,?) ON CONFLICT(account,root,number) DO UPDATE SET plan=excluded.plan',
                                         (*self.identity, number, json.dumps(plan, ensure_ascii=False)))
                self.registry.db.execute('INSERT INTO library_policies VALUES(?,?,?,?) ON CONFLICT(account,root,number) DO UPDATE SET signature=excluded.signature',
                                         (*self.identity, number, signature))
        except sqlite3.Error as exc:
            raise CheckpointError('迁移完成登记失败（'+type(exc).__name__+'）') from None
        return plan['refresh_needed']
