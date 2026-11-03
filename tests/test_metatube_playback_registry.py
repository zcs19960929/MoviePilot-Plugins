"""Regression coverage for persistent playback IDs and read-only playback."""
import dataclasses
import json
import sqlite3
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from test_metatube_auth import load_plugin
from test_metatube_direct import components
import test_metatube_direct as fixtures


class PlaybackRegistryTests(unittest.TestCase):
    def test_concurrent_first_registration_preserves_both_returned_ids(self):
        for version in ('v2', 'v3'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                m = components(version)
                regs = [m.Registry(tmp), m.Registry(tmp)]
                self.addCleanup(regs[0].close); self.addCleanup(regs[1].close)
                barrier = threading.Barrier(2)
                class Connection:
                    def __init__(self, db): self.db, self.wrote = db, False
                    def __enter__(self): self.db.__enter__(); return self
                    def __exit__(self, *args): return self.db.__exit__(*args)
                    def execute(self, sql, args=()):
                        if sql.startswith('SELECT token') and not self.wrote:
                            row = self.db.execute(sql, args).fetchone()
                            barrier.wait(3)
                            return type('Cursor', (), {'fetchone': lambda s: row})()
                        if sql.startswith('INSERT'): self.wrote = True
                        return self.db.execute(sql, args)
                    def __getattr__(self, name): return getattr(self.db, name)
                for r in regs: r.db = Connection(r.db)
                entry = m.Entry('7', '0', 'DSOD-060.mp4', pickcode='fixture')
                with ThreadPoolExecutor(2) as pool:
                    tokens = list(pool.map(lambda r: r.media_token('12345', entry), regs))
                self.assertEqual(tokens[0], tokens[1], 'same file must retain one stable ID')
                for token in tokens: self.assertEqual(regs[0].media('12345', token), entry)

    def test_old_alias_and_canonical_survive_reopen_and_reject_rebinding(self):
        for version in ('v2', 'v3'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                m = components(version); r = m.Registry(tmp); self.addCleanup(r.close)
                entry = m.Entry('7', '0', 'DSOD-060.mp4', pickcode='fixture')
                canonical = r.media_token('12345', entry)
                self.assertTrue(callable(getattr(r, 'restore_media', None)), 'restore API missing')
                self.assertTrue(r.restore_media('12345', entry, 'a' * 64))
                self.assertFalse(r.restore_media('12345', entry, 'a' * 64))
                r.close(); r.reopen()
                for token in (canonical, 'a' * 64): self.assertEqual(r.media('12345', token), entry)
                with self.assertRaises(KeyError): r.media('other', 'a' * 64)
                with self.assertRaises(m.CloudError):
                    r.restore_media('12345', dataclasses.replace(entry, id='8'), 'a' * 64)
                with self.assertRaises(m.CloudError): r.restore_media('other', entry, 'a' * 64)
                self.assertEqual(r.media_token('12345', entry), canonical)

    def test_relative_output_rejected_before_creating_state_or_library(self):
        m = components()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)/'state'
            with self.assertRaisesRegex(m.CloudError, '绝对路径'):
                m.DirectService(client=Mock(), data_dir=state, output_root='media/link/japan',
                                playback_base_url='https://mp.example', metadata=Mock(), artwork=Mock(),
                                build_nfo=Mock(), extract_number=Mock())
            self.assertFalse(state.exists())
        for version in ('v2', 'v3'):
            plugin = load_plugin(version, Mock())
            plugin.init_plugin(dict(enabled=True, storage='115_direct', cookie115='fixture',
                                    metatube_url='http://backend', scan_dirs='id:0',
                                    local_strm_root='media/link/japan', playback_base_url='https://mp.example'))
            self.assertIn('绝对路径', ' '.join(plugin._config_errors))
            result = plugin._MetaTubeScraper__api_run()
            self.assertNotEqual(result['code'], 0)

    def test_strm_uses_current_plugin_instance(self):
        fixture = fixtures.DirectAcceptanceTests(); fixture.addCleanup = self.addCleanup
        with tempfile.TemporaryDirectory() as tmp:
            m, cloud, metadata, service = fixture.make(tmp)
            service.plugin_id = 'MetaTubeScraper_Clone1'
            self.assertEqual(service.run(['0'])['ok'], 1)
            url = (Path(tmp)/'library/DSOD-060/DSOD-060.strm').read_text()
            self.assertIn('/plugin/MetaTubeScraper_Clone1/redirect_url?', url)

    def test_playback_missing_database_returns_503_without_creating_files(self):
        from starlette.requests import Request
        for version in ('v2', 'v3'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                plugin = load_plugin(version, Mock())
                plugin.init_plugin(dict(storage='115_direct', cookie115='UID=12345_A; CID=x; SEID=x',
                                        local_strm_root=str(Path(tmp)/'library'), playback_base_url='https://mp.example'))
                plugin.get_data_path = lambda: Path(tmp)/'state'
                plugin._cloud115 = Mock(account='12345')
                req = Request({'type':'http', 'headers':[]})
                result = plugin._MetaTubeScraper__api_redirect(req, 'a'*64)
                self.assertEqual(result.status_code, 503)
                self.assertIn('登记数据库', json.loads(result.body)['error'])
                self.assertEqual(list(Path(tmp).iterdir()), [])
                self.assertEqual(result.headers.get('cache-control'), 'no-store')

    def test_legacy_schema_is_read_without_migration_and_upgrade_keeps_id(self):
        for version in ('v2','v3'):
            with self.subTest(version=version),tempfile.TemporaryDirectory() as tmp:
                m=components(version);r=m.Registry(tmp)
                entry=m.Entry('7','0','DSOD-060.mp4',pickcode='p')
                token=r.media_token('12345',entry)
                r.db.execute('DROP TABLE media_aliases');r.db.commit();r.close()
                file=Path(tmp)/'direct.sqlite3';original=file.read_bytes()
                reader=m.Registry(tmp,readonly=True)
                self.assertEqual(reader.media('12345',token),entry)
                with self.assertRaises(KeyError):reader.media('12345','b'*64)
                reader.close();self.assertEqual(file.read_bytes(),original)
                writer=m.Registry(tmp,create=False)
                self.assertEqual(writer.media_token('12345',entry),token)
                writer.restore_media('12345',entry,'a'*64)
                self.assertEqual(writer.media('12345','a'*64),entry)
                writer.close()


class CachedDatabaseTests(unittest.TestCase):
    def test_cached_playback_and_scan_services_reject_removed_or_replaced_database(self):
        from starlette.requests import Request
        import os
        for version in ('v2','v3'):
            for mode in ('playback','scan'):
                for change in ('delete','replace'):
                    with self.subTest(version=version,mode=mode,change=change),tempfile.TemporaryDirectory() as tmp:
                        plugin=load_plugin(version,Mock());plugin.init_plugin({'storage':'115_direct'})
                        g=plugin._MetaTubeScraper__get_direct_service.__globals__
                        state=Path(tmp)/'state';cloud=Mock(account='12345')
                        cloud.download_link.return_value=types.SimpleNamespace(url='https://cdn.example/v?t=4102444800',headers={},expires=4102444800)
                        if mode=='scan':
                            service=g['DirectService'](cloud,state,Path(tmp)/'out','https://mp.example',Mock(),Mock(),Mock(),Mock())
                            writer=service.registry
                        else:
                            writer=g['DirectService'](cloud,state,Path(tmp)/'out','https://mp.example',Mock(),Mock(),Mock(),Mock()).registry
                            self.addCleanup(writer.close)
                        entry=g['Cloud115']._entry({'fid':'7','cid':'0','n':'DSOD-060.mp4','pc':'p'})
                        token=writer.media_token('12345',entry)
                        if mode=='playback':service=g['PlaybackService'](cloud,state)
                        self.addCleanup(service.close)
                        setattr(plugin,'_direct_service' if mode=='scan' else '_playback_service',service)
                        req=Request({'type':'http','headers':[]})
                        self.assertEqual(plugin._MetaTubeScraper__api_redirect(req,token).status_code,302)
                        file=state/'direct.sqlite3'
                        if change=='delete':file.unlink()
                        else:
                            replacement=Path(tmp)/'replacement.db';sqlite3.connect(replacement).close();os.replace(replacement,file)
                        reply=plugin._MetaTubeScraper__api_redirect(req,token)
                        self.assertEqual(reply.status_code,503)
                        self.assertIn('登记数据库',json.loads(reply.body)['error'])
                        self.assertEqual(cloud.download_link.call_count,1)
