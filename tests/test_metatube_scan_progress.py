"""Durable scan position, tested against the real library and an offline cloud."""
import dataclasses
import importlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_metatube_library as fixtures
from test_metatube_auth import load_plugin


class ScanProgressTests(unittest.TestCase):
    make = fixtures.LibraryAcceptanceTests.make

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def library(self, count=3, archive=False, entries=None):
        entries = entries if entries is not None else [
            dict(id=str(i+1), parent_id='0', name=f'IPX-{i+201}.mp4', pickcode='a'*17)
            for i in range(count)]
        return self.make(self.temporary.name, entries, archive)

    def restart(self, service):
        service.close()
        new = type(service)(service.client, Path(self.temporary.name)/'state', service.root,
                            service.base, service.metadata, service.artwork, service.build_nfo,
                            service.extract_number, archive_loose=service.archive_loose,
                            plugin_id=service.plugin_id, info=Mock(), error=Mock())
        self.addCleanup(new.close)
        return new

    def test_one_hundred_movies_resume_mid_directory_across_restarts(self):
        _, cloud, _, service = self.library(100)
        results = []
        for _ in range(4):
            result = service.run(['0'], limit=30, scan_strategy='incremental')
            results.append(result)
            self.assertEqual(result['fail'], 0)
            service = self.restart(service)
        self.assertEqual([result['ok'] for result in results], [30, 30, 30, 10])
        self.assertEqual([result['skip'] for result in results], [0, 0, 0, 0])
        self.assertTrue(results[-1]['scan_cycle_complete'])
        self.assertEqual(len(list(service.root.rglob('*.strm'))), 100)

    def test_next_cycle_discovers_files_before_previous_cursor(self):
        m, cloud, _, service = self.library(2)
        first = service.run(['0'], limit=1, scan_strategy='incremental')
        self.assertFalse(first['scan_cycle_complete'])
        cloud.entries['3'] = m.Entry('3', '0', 'IPX-203.mp4', pickcode='c'*17)
        second = service.run(['0'], limit=1, scan_strategy='incremental')
        self.assertTrue(second['scan_cycle_complete'])
        self.assertFalse((service.root/'IPX/IPX-203').exists())
        third = service.run(['0'], limit=1, scan_strategy='incremental')
        self.assertFalse(third['scan_resumed'])
        self.assertEqual((third['ok'], third['skip']), (1, 2))
        self.assertTrue((service.root/'IPX/IPX-203/IPX-203 - V0001.strm').is_file())

    def test_resuming_does_not_revisit_finished_directories(self):
        _, cloud, _, service = self.library(entries=[
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='IPX-201', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='IPX-202', is_dir=True),
            dict(id='2', parent_id='9', name='IPX-202.mp4', pickcode='b'*17),
        ])
        self.assertEqual(service.run(['7'], limit=1, scan_strategy='incremental')['ok'], 1)
        service = self.restart(service)
        cloud.listdir = Mock(wraps=cloud.listdir)
        result = service.run(['7'], limit=1, scan_strategy='incremental')
        self.assertTrue(result['scan_resumed'])
        self.assertEqual(result['ok'], 1)
        self.assertFalse(any(call.args == ('8',) for call in cloud.listdir.call_args_list))

    def test_movie_failure_and_notfound_advance_to_following_movies(self):
        _, cloud, metadata, service = self.library()
        original = metadata.return_value
        def lookup(number):
            if number == 'IPX-201':
                return None
            return original
        metadata.side_effect = lookup
        first = service.run(['0'], limit=1, scan_strategy='incremental')
        self.assertEqual(first['notfound'], 1)
        cloud.fail_rename = '2'
        second = service.run(['0'], limit=1, scan_strategy='incremental')
        self.assertEqual(second['fail'], 1)
        third = service.run(['0'], limit=1, scan_strategy='incremental')
        self.assertEqual(third['ok'], 1)
        self.assertTrue(third['scan_cycle_complete'])

    def test_risk_keeps_current_movie_for_retry(self):
        m, cloud, metadata, service = self.library()
        value = metadata.return_value
        metadata.side_effect = m.RiskError('verification required')
        with self.assertRaises(m.RiskError):
            service.run(['0'], limit=1, scan_strategy='incremental')
        metadata.side_effect = None
        metadata.return_value = value
        service = self.restart(service)
        result = service.run(['0'], limit=1, scan_strategy='incremental')
        self.assertEqual(result['ok'], 1)
        self.assertTrue((service.root/'IPX/IPX-201/IPX-201 - V0001.strm').is_file())
        self.assertEqual(cloud.entries['2'].name, 'IPX-202.mp4')

    def test_stop_after_publication_replays_without_renaming_or_changing_link(self):
        m, cloud, _, service = self.library()
        original = service._process_movie
        def interrupted(*args):
            original(*args)
            raise m.Stopped('simulated process interruption before checkpoint')
        service._process_movie = interrupted
        first = service.run(['0'], limit=1, scan_strategy='incremental')
        self.assertTrue(first['stopped'])
        path = service.root/'IPX/IPX-201/IPX-201 - V0001.strm'
        link, renames = path.read_bytes(), list(cloud.renames)
        service = self.restart(service)
        service.run(['0'], limit=1, scan_strategy='incremental')
        self.assertEqual(path.read_bytes(), link)
        self.assertEqual([item for item in cloud.renames if item[0] == '1'], renames)

    def test_directory_failure_retains_queue_head(self):
        m, cloud, _, service = self.library(entries=[
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='IPX-201', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='IPX-202', is_dir=True),
            dict(id='2', parent_id='9', name='IPX-202.mp4', pickcode='b'*17),
        ])
        service.run(['7'], limit=1, scan_strategy='incremental')
        original = cloud.listdir
        cloud.listdir = lambda cid: (_ for _ in ()).throw(m.CloudError('page changed')) if cid == '9' else original(cid)
        failed = service.run(['7'], limit=1, scan_strategy='incremental')
        self.assertTrue(failed['stopped'])
        self.assertIn('page changed', failed['scan_pause_reason'])
        cloud.listdir = original
        result = self.restart(service).run(['7'], limit=1, scan_strategy='incremental')
        self.assertEqual(result['ok'], 1)
        self.assertTrue((service.root/'IPX/IPX-202/IPX-202 - V0001.strm').is_file())

    def test_pending_directory_moved_outside_scope_is_not_read_or_modified(self):
        _, cloud, _, service = self.library(entries=[
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='IPX-201', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='IPX-202', is_dir=True),
            dict(id='2', parent_id='9', name='IPX-202.mp4', pickcode='b'*17),
            dict(id='10', parent_id='0', name='outside', is_dir=True),
        ])
        service.run(['7'], limit=1, scan_strategy='incremental')
        cloud.entries['9'] = dataclasses.replace(cloud.entries['9'], parent_id='10')
        cloud.listdir = Mock(wraps=cloud.listdir)
        service = self.restart(service)
        result = service.run(['7'], limit=1, scan_strategy='incremental')
        self.assertTrue(result['stopped'])
        self.assertEqual(cloud.entries['2'].name, 'IPX-202.mp4')
        self.assertFalse(any(call.args == ('9',) for call in cloud.listdir.call_args_list))

    def test_pending_directory_moved_beyond_depth_is_not_read_or_advanced(self):
        _, cloud, _, service = self.library(entries=[
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='IPX-201', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='IPX-202', is_dir=True),
            dict(id='2', parent_id='9', name='IPX-202.mp4', pickcode='b'*17),
            dict(id='10', parent_id='7', name='deeper', is_dir=True),
        ])
        service.run(['7'], limit=1, scan_strategy='incremental')
        cloud.entries['9'] = dataclasses.replace(cloud.entries['9'], parent_id='10')
        cloud.listdir = Mock(wraps=cloud.listdir)
        service = self.restart(service)
        result = service.run(['7'], limit=1, scan_strategy='incremental')
        self.assertTrue(result.get('stopped', False))
        self.assertFalse(any(call.args == ('9',) for call in cloud.listdir.call_args_list))
        cloud.entries['9'] = dataclasses.replace(cloud.entries['9'], parent_id='7')
        again = service.run(['7'], limit=1, scan_strategy='incremental')
        self.assertEqual(again['ok'], 1)

    def test_unsnapshotted_directory_uses_live_name_after_rename(self):
        m, cloud, metadata, service = self.library(entries=[
            dict(id='7', parent_id='0', name='IPX-201', is_dir=True),
            dict(id='1', parent_id='7', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='8', parent_id='0', name='IPX-202', is_dir=True),
            dict(id='2', parent_id='8', name='movie.mp4', pickcode='b'*17),
        ])
        service.run(['7', '8'], limit=1, scan_strategy='incremental')
        cloud.entries['8'] = dataclasses.replace(cloud.entries['8'], name='IPX-203')
        metadata.reset_mock()
        result = self.restart(service).run(['7', '8'], limit=1, scan_strategy='incremental')
        self.assertEqual(result['ok'], 1)
        self.assertEqual([call.args[0] for call in metadata.call_args_list], ['IPX-203'])
        self.assertEqual(cloud.entries['2'].name, 'IPX-203 - V0001.mp4')
        self.assertFalse((service.root/'IPX/IPX-202').exists())

    def test_full_mode_discards_old_cursor_and_keeps_media_registry(self):
        _, cloud, _, service = self.library(3)
        service.run(['0'], limit=1, scan_strategy='incremental')
        path = service.root/'IPX/IPX-201/IPX-201 - V0001.strm'
        link = path.read_bytes()
        full = service.run(['0'], limit=1)
        self.assertEqual((full['ok'], full['skip']), (1, 1))
        again = service.run(['0'], limit=1, scan_strategy='incremental')
        self.assertFalse(again['scan_resumed'])
        self.assertEqual((again['ok'], again['skip']), (1, 2))
        self.assertEqual(path.read_bytes(), link)

    def test_full_mode_reports_next_start_at_roots_not_remaining_batch(self):
        _, _, _, service = self.library()
        result = service.run(['0'], limit=1)
        self.assertIn('从头', result['scan_next_position'])
        self.assertNotIn('IPX-202', result['scan_next_position'])

    def test_resumption_uses_root_ids_after_selected_path_was_moved(self):
        _, cloud, _, service = self.library(archive=True, entries=[
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='IPX-201 incoming', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='IPX-202', is_dir=True),
            dict(id='2', parent_id='9', name='IPX-202.mp4', pickcode='b'*17),
        ])
        roots = ['/jav/IPX-201 incoming', 'id:9']
        cloud.resolve_folder = lambda value: '8' if value == roots[0] else '9'
        service.run(roots, limit=1, scan_strategy='incremental')
        self.assertEqual(cloud.entries['8'].name, 'IPX-201')
        service = self.restart(service)
        cloud.resolve_folder = Mock(side_effect=AssertionError('old path must not be resolved'))
        result = service.run(roots, limit=1, scan_strategy='incremental')
        self.assertEqual(result['ok'], 1)
        self.assertTrue(result['scan_resumed'])
        cloud.resolve_folder.assert_not_called()

    def test_independent_root_guard_survives_task_boundary(self):
        _, cloud, _, service = self.library(archive=True, entries=[
            dict(id='7', parent_id='0', name='source-a', is_dir=True),
            dict(id='8', parent_id='0', name='source-b', is_dir=True),
            dict(id='1', parent_id='7', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='2', parent_id='8', name='IPX-201-other.mp4', pickcode='b'*17),
        ])
        service.run(['7', '8'], limit=1, scan_strategy='incremental')
        result = self.restart(service).run(['7', '8'], limit=1, scan_strategy='incremental')
        self.assertEqual(result['fail'], 1)
        self.assertEqual(cloud.entries['2'].parent_id, '8')
        self.assertEqual(cloud.entries['2'].name, 'IPX-201-other.mp4')

    def test_snapshot_save_failure_stops_before_any_mutation(self):
        _, cloud, _, service = self.library()
        progress = importlib.import_module(type(service).__module__.rsplit('.', 1)[0]+'.scan_progress')
        original = progress.ScanProgress.save
        def saving(store, state):
            if state['current'] is not None:
                raise progress.CheckpointError('snapshot write failed')
            return original(store, state)
        with patch.object(progress.ScanProgress, 'save', saving):
            result = service.run(['0'], limit=1, scan_strategy='incremental')
        self.assertTrue(result['stopped'])
        self.assertEqual(cloud.renames, [])
        self.assertEqual(list(service.root.rglob('*.strm')), [])
        again = self.restart(service).run(['0'], limit=1, scan_strategy='incremental')
        self.assertEqual(again['ok'], 1)
        self.assertTrue((service.root/'IPX/IPX-201/IPX-201 - V0001.strm').is_file())

    def test_post_publication_commit_failure_preserves_uncommitted_batch(self):
        _, cloud, _, service = self.library(2)
        progress = importlib.import_module(type(service).__module__.rsplit('.', 1)[0]+'.scan_progress')
        original = progress.ScanProgress.save
        def saving(store, state):
            if state['attempted']:
                raise progress.CheckpointError('advance write failed')
            return original(store, state)
        with patch.object(progress.ScanProgress, 'save', saving):
            result = service.run(['0'], limit=1, scan_strategy='incremental')
        self.assertTrue(result['stopped'])
        first = service.root/'IPX/IPX-201/IPX-201 - V0001.strm'
        link = first.read_bytes()
        self.assertEqual(cloud.entries['2'].name, 'IPX-202.mp4')
        again = self.restart(service).run(['0'], limit=1, scan_strategy='incremental')
        self.assertEqual((again['ok'], again['skip']), (1, 1))
        self.assertEqual(first.read_bytes(), link)
        self.assertEqual([item for item in cloud.renames if item[0] == '1'], [('1', 'IPX-201 - V0001.mp4')])

    def test_corrupt_checkpoint_restarts_and_does_not_remove_media(self):
        _, cloud, _, service = self.library()
        service.run(['0'], limit=1, scan_strategy='incremental')
        with service.registry.db:
            service.registry.db.execute("UPDATE scan_checkpoint SET payload='invalid json' WHERE key='meta'")
        first = service.root/'IPX/IPX-201/IPX-201 - V0001.strm'
        link = first.read_bytes()
        result = self.restart(service).run(['0'], limit=1, scan_strategy='incremental')
        self.assertFalse(result['scan_resumed'])
        self.assertEqual((result['ok'], result['skip']), (1, 1))
        self.assertEqual(first.read_bytes(), link)

    def test_incomplete_corrupt_snapshot_is_reset_instead_of_stalling(self):
        _, cloud, _, service = self.library()
        service.run(['0'], limit=1, scan_strategy='incremental')
        with service.registry.db:
            service.registry.db.execute("DELETE FROM scan_checkpoint WHERE key LIKE 'batch:%'")
        result = self.restart(service).run(['0'], limit=1, scan_strategy='incremental')
        self.assertFalse(result['scan_resumed'])
        self.assertEqual((result['ok'], result['skip']), (1, 1))
        self.assertFalse(result.get('stopped', False))

    def test_stale_checkpoint_writer_cannot_overwrite_a_newer_position(self):
        _, cloud, _, service = self.library()
        service.run(['0'], limit=1, scan_strategy='incremental')
        progress = importlib.import_module(type(service).__module__.rsplit('.', 1)[0]+'.scan_progress')
        stale = progress.ScanProgress(service.registry, service._progress.signature)
        previous = stale.load()
        service.run(['0'], limit=1, scan_strategy='incremental')
        with self.assertRaises(progress.CheckpointError):
            stale.save(previous)
        result = self.restart(service).run(['0'], limit=1, scan_strategy='incremental')
        self.assertEqual((result['ok'], result['skip']), (1, 0))
        self.assertTrue((service.root/'IPX/IPX-203/IPX-203 - V0001.strm').is_file())

    def test_concurrent_checkpoint_compare_and_write_are_one_transaction(self):
        _, _, _, service = self.library()
        service.run(['0'], limit=1, scan_strategy='incremental')
        progress = importlib.import_module(type(service).__module__.rsplit('.', 1)[0]+'.scan_progress')
        registry_b = type(service.registry)(service.registry.directory, service.root)
        self.addCleanup(registry_b.close)
        writer_a = progress.ScanProgress(service.registry, service._progress.signature)
        writer_b = progress.ScanProgress(registry_b, service._progress.signature)
        state_a, state_b = writer_a.load(), writer_b.load()
        state_a['serial'] += 10
        state_b['serial'] += 20
        read_a, release_a, done_b = threading.Event(), threading.Event(), threading.Event()
        connection = service.registry.db
        class PausedCursor:
            def __init__(self, cursor): self.cursor = cursor
            def fetchone(self):
                value = self.cursor.fetchone()
                read_a.set()
                if not release_a.wait(5): raise RuntimeError('test did not release writer')
                return value
        class PausedConnection:
            def __enter__(self): connection.__enter__(); return self
            def __exit__(self, *args): return connection.__exit__(*args)
            def __getattr__(self, name): return getattr(connection, name)
            def execute(self, sql, *args):
                cursor = connection.execute(sql, *args)
                return PausedCursor(cursor) if sql == "SELECT payload FROM scan_checkpoint WHERE key='meta'" else cursor
        service.registry.db = PausedConnection()
        outcomes = []
        def save(writer, state, done=None):
            try:
                writer.save(state)
                outcomes.append('saved')
            except progress.CheckpointError:
                outcomes.append('stale')
            finally:
                if done: done.set()
        a = threading.Thread(target=save, args=(writer_a, state_a))
        b = threading.Thread(target=save, args=(writer_b, state_b, done_b))
        try:
            a.start()
            self.assertTrue(read_a.wait(2))
            b.start()
            done_b.wait(1)
        finally:
            release_a.set()
            a.join(3)
            if b.ident is not None: b.join(3)
            service.registry.db = connection
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertCountEqual(outcomes, ['saved', 'stale'])

    def test_directory_read_failures_during_movie_processing_do_not_advance(self):
        for classification_read in (False, True):
            with self.subTest(classification_read=classification_read), tempfile.TemporaryDirectory() as temporary:
                entries = [dict(id='1', parent_id='0', name='IPX-201.mp4', pickcode='a'*17)]
                roots, failing = ['0'], '0'
                if classification_read:
                    entries = [
                        dict(id='7', parent_id='0', name='jav', is_dir=True),
                        dict(id='8', parent_id='7', name='IPX', is_dir=True),
                        dict(id='9', parent_id='8', name='IPX-201', is_dir=True),
                        dict(id='1', parent_id='9', name='IPX-201.mp4', pickcode='a'*17),
                    ]
                    roots, failing = ['7'], '8'
                m, cloud, _, service = self.make(temporary, entries)
                original = cloud.listdir
                calls = []
                def listing(cid):
                    if cid == failing:
                        calls.append(cid)
                        if len(calls) == 2: raise m.CloudError('transient directory pagination failure')
                    return original(cid)
                cloud.listdir = listing
                result = service.run(roots, limit=1, scan_strategy='incremental')
                self.assertTrue(result.get('stopped', False))
                self.assertFalse(result['scan_cycle_complete'])
                cloud.listdir = original
                again = service.run(roots, limit=1, scan_strategy='incremental')
                self.assertTrue(again['scan_resumed'])
                self.assertEqual(again['ok'], 1)
                self.assertTrue((service.root/'IPX/IPX-201/IPX-201 - V0001.strm').is_file())

    def test_scope_settings_invalidate_checkpoint_but_limit_does_not(self):
        for setting in ('archive', 'numeric', 'recursive', 'roots', 'output', 'account', 'limit'):
            with self.subTest(setting=setting), tempfile.TemporaryDirectory() as temporary:
                _, cloud, _, service = self.make(temporary, [
                    dict(id=str(i+1), parent_id='0', name=f'IPX-{i+201}.mp4', pickcode='a'*17)
                    for i in range(4)])
                service.run(['0'], limit=1, scan_strategy='incremental')
                options = dict(limit=1, scan_strategy='incremental')
                roots = ['0']
                if setting == 'archive': service.archive_loose = True
                if setting == 'numeric': service.numeric_ids = True
                if setting == 'recursive': options['recursive'] = True
                if setting == 'roots': roots = ['id:0']
                if setting == 'output': service.root = Path(temporary)/'other-output'
                if setting == 'account': cloud.account = 'other-account'
                if setting == 'limit': options['limit'] = 2
                result = service.run(roots, **options)
                self.assertEqual(result['scan_resumed'], setting == 'limit')
                if setting == 'limit':
                    self.assertEqual((result['ok'], result['skip']), (2, 0))


class StrategyConfigurationTests(unittest.TestCase):
    def test_saved_strategy_is_normalized_and_legacy_configs_default_to_full(self):
        for version in ('v2', 'v3'):
            plugin = load_plugin(version, Mock())
            plugin.init_plugin({'storage':'115_direct'})
            self.assertEqual(plugin._scan_strategy, 'full')
            plugin.init_plugin({'storage':'115_direct', 'scan_strategy': {'value':'incremental'}})
            self.assertEqual(plugin._scan_strategy, 'incremental')

    def test_run_page_reports_incremental_resume_and_cycle_state(self):
        plugin = load_plugin('v3', Mock())
        plugin.init_plugin({'storage':'115_direct'})
        plugin.get_data = lambda key: dict(scan_strategy='incremental', scan_resumed=True,
            scan_next_position='/jav/IPX-202', scan_cycle_complete=False, scan_pause_reason='达到单次上限')
        page = str(plugin.get_page())
        for value in ('增量', '/jav/IPX-202', '达到单次上限'):
            self.assertIn(value, page)


if __name__ == '__main__':
    unittest.main()
