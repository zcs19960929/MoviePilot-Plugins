"""Atomic, versioned scan checkpoints in the existing media database."""
import json
import re

from .cloud115 import CloudError


class CheckpointError(CloudError):
    """Stop processing when scan position cannot be committed safely."""


class ScanProgress:
    VERSION = 1

    def __init__(self, registry, signature, enabled=True, info=None):
        self.registry, self.signature, self.enabled = registry, signature, enabled
        self.info = info or (lambda message: None)
        self.records = {}
        with registry._lock, registry.db:
            registry.check_identity()
            registry.db.execute('CREATE TABLE IF NOT EXISTS scan_checkpoint (key TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def clear(self):
        with self.registry._lock, self.registry.db:
            self.registry.check_identity()
            self.registry.db.execute('DELETE FROM scan_checkpoint')
        self.records = {}

    def load(self):
        with self.registry._lock:
            self.registry.check_identity()
            self.records = {row['key']: row['payload'] for row in
                            self.registry.db.execute('SELECT key,payload FROM scan_checkpoint')}
        if not self.records:
            return None
        try:
            meta = json.loads(self.records['meta'])
            if meta['signature'] != self.signature:
                self.info('扫描范围或设置已变化，旧增量进度已重置，将从头开始')
                self.clear()
                return None
            state = meta['state']
            if state['version'] != self.VERSION or not isinstance(meta['revision'], int):
                raise ValueError('unsupported checkpoint')
            for key in ('selected', 'archive_roots', 'category_roots'):
                if not isinstance(state[key], dict):
                    raise ValueError('invalid scope')
            if not isinstance(state['bindings'], list) or not isinstance(state['done'], bool):
                raise ValueError('invalid queue')
            state.update(queue={}, seen=[], attempted=[], owners={})
            if state['current'] is not None:
                state['current'].update(media={}, groups={})
            for key, payload in self.records.items():
                if key == 'meta':
                    continue
                prefix, item_id = key.split(':', 1)
                value = json.loads(payload)
                if prefix == 'queue':
                    state['queue'][item_id] = value
                elif prefix in ('seen', 'attempted'):
                    if not item_id.isdecimal():
                        raise ValueError('invalid identity')
                    state[prefix].append(item_id)
                elif prefix == 'owner':
                    state['owners'][item_id] = value
                elif prefix in ('media', 'batch') and state['current'] is not None:
                    state['current']['media' if prefix == 'media' else 'groups'][item_id] = value
                else:
                    raise ValueError('unknown checkpoint record')
            self._validate(state)
            return state
        except (ValueError, TypeError, KeyError, AttributeError):
            self.info('增量检查点损坏或格式不兼容，已重置，将从头开始')
            self.clear()
            return None

    @staticmethod
    def _validate(state):
        if not isinstance(state['serial'], int) or state['serial'] < 0:
            raise ValueError('invalid serial')
        for task in list(state['queue'].values()) + state['bindings']:
            if (not isinstance(task, list) or len(task) != 5 or
                    not all(isinstance(task[i], str) for i in (0, 2, 3)) or
                    not task[0].isdecimal() or not task[3].isdecimal() or
                    not isinstance(task[1], int) or task[1] < 0 or not isinstance(task[4], bool)):
                raise ValueError('invalid directory task')
        for key in state['queue']:
            if not key.isdecimal():
                raise ValueError('invalid queue order')
        for mapping in (state['selected'], state['category_roots']):
            for cid, folder in mapping.items():
                if not isinstance(cid, str) or not cid.isdecimal():
                    raise ValueError('invalid directory identity')
                if folder is None and mapping is state['selected'] and cid == '0':
                    continue
                if (not isinstance(folder, dict) or folder['id'] != cid or folder['is_dir'] is not True or
                        not isinstance(folder['name'], str) or not isinstance(folder['parent_id'], str) or
                        not folder['parent_id'].isdecimal()):
                    raise ValueError('invalid selected root')
        if set(state['archive_roots']) != set(state['selected']):
            raise ValueError('missing archive roots')
        if not all(isinstance(root, str) and root.isdecimal() for root in state['archive_roots'].values()):
            raise ValueError('invalid archive roots')
        for number, root in state['owners'].items():
            if not re.fullmatch(r'[A-Z0-9][A-Z0-9-]*', number) or not isinstance(root, str) or not root.isdecimal():
                raise ValueError('invalid archive owner')
        current = state['current']
        if current is not None:
            if (not isinstance(current['directory'], str) or not current['directory'].isdecimal() or
                    not isinstance(current['display'], str) or not current['groups']):
                raise ValueError('invalid current directory')
            for fid, entry in current['media'].items():
                if (not fid.isdecimal() or entry['id'] != fid or not isinstance(entry['name'], str) or
                        not isinstance(entry['parent_id'], str) or not entry['parent_id'].isdecimal() or
                        entry['is_dir'] is not False):
                    raise ValueError('invalid media snapshot')
            for key, batch in current['groups'].items():
                if (not key.isdecimal() or not re.fullmatch(r'[A-Z0-9][A-Z0-9-]*', batch['number']) or
                        not isinstance(batch['root'], str) or not batch['root'].isdecimal() or
                        not isinstance(batch['videos'], list) or
                        not all(fid in current['media'] for fid in batch['videos'])):
                    raise ValueError('invalid movie batch')
        if state['done'] and (state['queue'] or current is not None):
            raise ValueError('completed checkpoint still has work')

    def save(self, state):
        if not self.enabled:
            return
        try:
            self._validate(state)
            header = {key: value for key, value in state.items()
                      if key not in ('queue', 'seen', 'attempted', 'owners', 'current')}
            current = state['current']
            header['current'] = ({key: value for key, value in current.items() if key not in ('media', 'groups')}
                                 if current is not None else None)
            revision = json.loads(self.records['meta'])['revision'] + 1 if 'meta' in self.records else 1
            encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            records = {'meta': encode(dict(signature=self.signature, revision=revision, state=header))}
            for prefix in ('queue', 'owners'):
                name = 'owner' if prefix == 'owners' else prefix
                records.update((name+':'+key, encode(value)) for key, value in state[prefix].items())
            for prefix in ('seen', 'attempted'):
                records.update((prefix+':'+key, 'true') for key in state[prefix])
            if current is not None:
                records.update(('media:'+key, encode(value)) for key, value in current['media'].items())
                records.update(('batch:'+key, encode(value)) for key, value in current['groups'].items())
            # Persist only changed records: a large pending queue/snapshot is
            # written once, rather than serialized to SQLite after every movie.
            with self.registry._lock, self.registry.db:
                self.registry.check_identity()
                # Connection's context manager does not begin a transaction for
                # SELECT. Lock before the comparison so two connections cannot
                # both pass it and overwrite each other's position.
                self.registry.db.execute('BEGIN IMMEDIATE')
                row = self.registry.db.execute("SELECT payload FROM scan_checkpoint WHERE key='meta'").fetchone()
                if (row['payload'] if row else None) != self.records.get('meta'):
                    raise CheckpointError('扫描进度已被其他任务修改，请重新执行')
                self.registry.db.executemany('DELETE FROM scan_checkpoint WHERE key=?',
                    [(key,) for key in self.records.keys() - records.keys()])
                self.registry.db.executemany('INSERT INTO scan_checkpoint VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                    [(key, value) for key, value in records.items() if self.records.get(key) != value])
            self.records = records
        except CheckpointError:
            raise
        except Exception as error:
            raise CheckpointError('扫描进度保存失败（'+type(error).__name__+'），已停止，未提交影片下次重试') from None
