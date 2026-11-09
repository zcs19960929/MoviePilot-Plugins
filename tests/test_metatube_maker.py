"""Maker classification and resumable relocation through the actual service."""
import importlib
import dataclasses
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_metatube_direct import FakeCloud, components


class MakerCloud(FakeCloud):
    def __init__(self, module, entries):
        super().__init__(module, entries)
        self.recycled = []
        self.lose_recycle_response = False

    def recycle_empty_directory(self, cid, parent_id, name, protected=()):
        if cid in protected:
            raise self.module.CloudError('protected directory')
        entry = self.info(cid)
        if not entry.is_dir or entry.parent_id != parent_id or entry.name != name:
            raise self.module.CloudError('directory identity changed')
        if self.listdir(cid):
            return False
        self.recycled.append(cid)
        del self.entries[cid]
        if self.lose_recycle_response:
            self.lose_recycle_response = False
            raise self.module.CloudError('response lost after recycle')
        return True


class MakerPolicyTests(unittest.TestCase):
    def policy(self, *args, **kwargs):
        return importlib.import_module(components().__package__+'.classification').ClassificationPolicy(*args, **kwargs)

    def test_aliases_unicode_whitespace_and_case_are_normalized(self):
        policy = self.policy('maker')
        for maker, expected in [('Ｓ１', 'S1'), ('  idea   pocket ', 'IDEA POCKET'),
                                ('アイデアポケット', 'IDEA POCKET'), ('ムーディーズ', 'MOODYZ'),
                                ('マドンナ', 'MADONNA'), ('プレミアム', 'PREMIUM'), ('アタッカーズ', 'ATTACKERS')]:
            self.assertEqual(policy.category('IPX-201', {'maker':maker}), expected)

    def test_maker_precedes_label_and_other_is_distinct_from_unknown(self):
        policy = self.policy('maker')
        for metadata, expected in [({'maker':'new maker','label':'S1'}, '其他厂商'),
                                   ({'maker':' ', 'label':'S1'}, 'S1'), ({}, '未知厂商')]:
            self.assertEqual(policy.category('IPX-201', metadata), expected)
        self.assertEqual(self.policy().category('IPX-201', {}), 'IPX')

    def test_editable_groups_reject_ambiguous_aliases_and_unsafe_names(self):
        self.assertEqual(self.policy('maker', '主力=IDEA POCKET,アイデアポケット').category(
            'IPX-201', {'maker':'アイデアポケット'}), '主力')
        for rules in ('A=S1\nB=Ｓ１', 'A=S1\na=MOODYZ', '../坏=S1', '未知厂商=S1', 'bad/name=S1'):
            with self.subTest(rules=rules), self.assertRaises(ValueError):
                self.policy('maker', rules)


class MakerLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.m = components()
        self.cloud = MakerCloud(self.m, [
            self.m.Entry('7','0','jav',is_dir=True),
            self.m.Entry('1','7','IPX-201.mp4',pickcode='a'*17),
            self.m.Entry('2','7','IPX-201.zh.ass',pickcode='b'*17),
            self.m.Entry('77','0','archive',is_dir=True),
            self.m.Entry('88','0','next-archive',is_dir=True),
        ])
        self.metadata = Mock(return_value={'title':'Title','maker':'アイデアポケット','provider':'FANZA','id':'film'})

    def service(self, layout='maker', target='', rules=None, archive=True, cleanup=True, version='v3'):
        module = components(version)
        service = module.LibraryService(self.cloud, Path(self.temporary.name)/'state',
            Path(self.temporary.name)/'library', 'https://mp.example', self.metadata,
            lambda info,kind:b'image', lambda info,number:b'<movie><title>Title</title></movie>',
            lambda text: (__import__('re').search(r'[A-Z]{2,6}-\d{2,5}', text.upper()) or [None])[0],
            archive_loose=archive, library_layout=layout, maker_groups=rules,
            archive_root=target, cleanup_empty_dirs=cleanup, info=Mock(), error=Mock())
        self.addCleanup(service.close)
        return service

    def test_maker_layout_and_custom_cloud_target(self):
        service = self.service(target='id:77')
        result = service.run(['7'])
        self.assertEqual(result['fail'], 0, result)
        movie = service.root/'IDEA POCKET/IPX-201'
        self.assertTrue((movie/'IPX-201 - V0001.strm').is_file())
        self.assertEqual((movie/'IPX-201 - V0001.zh.ass').read_bytes(), b'subtitle:2')
        folder = self.cloud.info(self.cloud.info('1').parent_id)
        category = self.cloud.info(folder.parent_id)
        self.assertEqual((category.parent_id, category.name, folder.name), ('77','IDEA POCKET','IPX-201'))
        self.assertIn('7', self.cloud.entries)

    def test_existing_prefix_outputs_migrate_with_unchanged_playback_token(self):
        old = self.service(layout='prefix', cleanup=False)
        self.assertEqual(old.run(['7'])['ok'], 1)
        old_folder = old.root/'IPX/IPX-201'
        url = (old_folder/'IPX-201 - V0001.strm').read_bytes()
        old.close()
        new = self.service()
        result = new.run(['7'])
        self.assertEqual(result['fail'], 0, result)
        new_folder = new.root/'IDEA POCKET/IPX-201'
        self.assertEqual((new_folder/'IPX-201 - V0001.strm').read_bytes(), url)
        self.assertFalse(old_folder.exists())
        self.assertFalse((new.root/'IPX').exists())
        self.assertTrue(self.cloud.recycled)
        self.assertNotIn('7', self.cloud.recycled)

    def test_modified_owned_file_blocks_migration_before_cloud_moves(self):
        old = self.service(layout='prefix', cleanup=False)
        old.run(['7'])
        folder = old.root/'IPX/IPX-201'
        (folder/'movie.nfo').write_bytes(b'manual edit')
        moves = list(self.cloud.moves)
        old.close()
        result = self.service().run(['7'])
        self.assertEqual(result['fail'], 1)
        self.assertEqual((folder/'movie.nfo').read_bytes(), b'manual edit')
        self.assertEqual(self.cloud.moves, moves)

    def test_rules_and_target_changes_revisit_registered_movies_outside_sources(self):
        first = self.service(target='id:77')
        self.assertEqual(first.run(['7'])['ok'], 1)
        url = (first.root/'IDEA POCKET/IPX-201/IPX-201 - V0001.strm').read_bytes()
        first.close()
        second = self.service(target='id:88', rules='主力=アイデアポケット')
        result = second.run(['7'])
        self.assertEqual(result['fail'], 0, result)
        self.assertEqual((second.root/'主力/IPX-201/IPX-201 - V0001.strm').read_bytes(), url)
        folder = self.cloud.info(self.cloud.info('1').parent_id)
        self.assertEqual(self.cloud.info(folder.parent_id).parent_id, '88')
        self.assertEqual(self.cloud.info(folder.parent_id).name, '主力')

    def test_v2_uses_the_same_maker_layout(self):
        result = self.service(version='v2', target='id:77').run(['7'])
        self.assertEqual(result['fail'], 0, result)
        self.assertTrue((Path(self.temporary.name)/'library/IDEA POCKET/IPX-201/movie.nfo').exists())

    def test_nested_destination_is_not_discovered_or_read_for_registered_recovery(self):
        self.cloud.entries['77'] = dataclasses.replace(self.cloud.entries['77'], parent_id='7')
        self.cloud.entries['3'] = self.m.Entry('3','77','IPX-999.mp4',pickcode='c'*17)
        first = self.service(target='id:77')
        result = first.run(['7'], recursive=True)
        self.assertEqual(result['ok'], 1, result)
        self.assertEqual(self.cloud.info('3').name, 'IPX-999.mp4')
        first.close()
        second = self.service(target='id:88', rules='主力=アイデアポケット')
        self.cloud.listdir = Mock(wraps=self.cloud.listdir)
        result = second.run(['7'], limit=1)
        self.assertEqual(result['ok'], 1, result)
        self.assertFalse(any(call.args == ('77',) for call in self.cloud.listdir.call_args_list))

    def test_explicit_common_destination_merges_independent_sources_as_versions(self):
        self.cloud.entries.update({
            '8':self.m.Entry('8','0','incoming',is_dir=True),
            '3':self.m.Entry('3','8','IPX-201 another.mkv',pickcode='c'*17)})
        service = self.service(target='id:77')
        result = service.run(['7','8'])
        self.assertEqual((result['ok'], result['fail']), (1,0), result)
        self.assertEqual(len(list((service.root/'IDEA POCKET/IPX-201').glob('*.strm'))), 2)
        self.assertEqual(self.cloud.info('1').parent_id, self.cloud.info('3').parent_id)
        service.close()
        frozen = self.service(target='id:88', rules='主力=アイデアポケット')
        before = list(self.cloud.moves)
        result = frozen.run(['7'])
        self.assertEqual(result['fail'], 1, result)
        self.assertEqual(self.cloud.moves, before)
        self.assertTrue((frozen.root/'IDEA POCKET/IPX-201/movie.nfo').exists())

    def test_independent_root_guard_survives_full_scan_order_change(self):
        first = self.service()
        first.run(['7'])
        first.close()
        self.cloud.entries.update({'8':self.m.Entry('8','0','other',is_dir=True),
            '3':self.m.Entry('3','8','IPX-201 other.mp4',pickcode='c'*17)})
        second = self.service()
        result = second.run(['8','7'])
        self.assertEqual(result['fail'], 1, result)
        self.assertEqual(self.cloud.info('3').parent_id, '8')
        self.assertEqual(len(list((second.root/'IDEA POCKET/IPX-201').glob('*.strm'))), 1)

    def test_selected_category_root_reuses_parent_on_repeat_and_reclassification(self):
        self.cloud.entries['7'] = dataclasses.replace(self.cloud.entries['7'], name='IPX')
        first = self.service(layout='prefix')
        self.assertEqual(first.run(['7'])['fail'], 0)
        again = first.run(['7'])
        self.assertEqual((again['skip'], again['fail']), (1,0), again)
        first.close()
        second = self.service()
        result = second.run(['7'])
        self.assertEqual(result['fail'], 0, result)
        folder = self.cloud.info(self.cloud.info('1').parent_id)
        self.assertEqual(self.cloud.info(folder.parent_id).parent_id, '0')
        self.assertEqual(self.cloud.info('7').parent_id, '0')

    def test_prefix_scan_recognizes_unregistered_configured_maker_directory(self):
        self.cloud.entries['9'] = self.m.Entry('9','7','IDEA POCKET',is_dir=True)
        self.cloud.entries['10'] = self.m.Entry('10','9','IPX-201',is_dir=True)
        for fid in ('1','2'):
            self.cloud.entries[fid] = dataclasses.replace(self.cloud.entries[fid], parent_id='10')
        service = self.service(layout='prefix')
        result = service.run(['7'])
        self.assertEqual(result['ok'], 1, result)
        self.assertTrue((service.root/'IPX/IPX-201/movie.nfo').exists())

    def test_completed_cleanup_does_not_delete_recreated_empty_local_directory(self):
        first = self.service(layout='prefix', cleanup=False)
        first.run(['7'])
        first.close()
        second = self.service()
        self.assertEqual(second.run(['7'])['fail'], 0)
        recreated = second.root/'IPX/IPX-201'
        recreated.mkdir(parents=True)
        result = second.run(['7'])
        self.assertEqual((result['skip'],result['migrated']), (1,0), result)
        self.assertTrue(recreated.is_dir())

    def test_enabling_cleanup_does_not_replay_completed_cloud_cleanup_candidates(self):
        first = self.service(layout='prefix', cleanup=False)
        first.run(['7'])
        old_folder = self.cloud.info('1').parent_id
        first.close()
        second = self.service(cleanup=False)
        second.run(['7'])
        second.close()
        self.assertIn(old_folder, self.cloud.entries)
        third = self.service(cleanup=True)
        result = third.run(['7'])
        self.assertEqual(result['fail'], 0, result)
        self.assertIn(old_folder, self.cloud.entries)

    def test_lost_move_response_is_recovered_on_the_very_next_incremental_task(self):
        del self.cloud.entries['2']
        first = self.service(target='id:77')
        first.run(['7'])
        first.close()
        second = self.service(target='id:88')
        self.cloud.interrupt_move_after = '1'
        result = second.run(['7'], limit=1, scan_strategy='incremental')
        self.assertEqual(result['fail'], 1, result)
        self.assertTrue(result.get('stopped'), result)
        second.close()
        third = self.service(target='id:88')
        result = third.run(['7'], limit=1, scan_strategy='incremental')
        self.assertEqual(result['fail'], 0, result)
        self.assertEqual(third.migrations.plan('IPX-201')['stage'], 'done')
        self.assertEqual(third.registry.movie(self.cloud.account,third.root,'IPX-201')['cloud_root'], '88')

    def test_local_move_replays_after_link_but_before_registry_commit(self):
        old = self.service(layout='prefix', cleanup=False)
        old.run(['7'])
        url = (old.root/'IPX/IPX-201/IPX-201 - V0001.strm').read_bytes()
        old.close()
        new = self.service()
        with patch.object(new.migrations, '_acknowledge', side_effect=OSError('power loss')):
            result = new.run(['7'], scan_strategy='incremental')
        self.assertTrue(result['stopped'], result)
        self.assertEqual(new.migrations.plan('IPX-201')['stage'], 'local_pending')
        new.close()
        restarted = self.service(rules='最新分类=アイデアポケット')
        result = restarted.run(['7'], scan_strategy='incremental')
        self.assertEqual(result['fail'], 0, result)
        self.assertEqual((restarted.root/'最新分类/IPX-201/IPX-201 - V0001.strm').read_bytes(), url)
        self.assertFalse((restarted.root/'IPX').exists())
        self.assertFalse((restarted.root/'IDEA POCKET').exists())

    def test_unowned_identical_target_is_a_conflict_and_unrelated_source_file_stays(self):
        old = self.service(layout='prefix', cleanup=False)
        old.run(['7'])
        original = old.root/'IPX/IPX-201'
        (original/'notes.txt').write_text('keep')
        target = old.root/'IDEA POCKET/IPX-201'
        target.mkdir(parents=True)
        (target/'movie.nfo').write_bytes((original/'movie.nfo').read_bytes())
        old.close()
        new = self.service()
        result = new.run(['7'])
        self.assertEqual(result['fail'], 1, result)
        self.assertTrue((original/'movie.nfo').exists())
        (target/'movie.nfo').unlink()
        result = new.run(['7'])
        self.assertEqual(result['fail'], 0, result)
        self.assertEqual((original/'notes.txt').read_text(), 'keep')

    def test_cleanup_lost_response_recovers_and_preserves_source_and_target_roots(self):
        old = self.service(layout='prefix', cleanup=False)
        old.run(['7'])
        old.close()
        new = self.service(target='id:77')
        self.cloud.lose_recycle_response = True
        result = new.run(['7'], scan_strategy='incremental')
        self.assertTrue(result.get('stopped'), result)
        self.assertEqual(new.migrations.plan('IPX-201')['stage'], 'cleanup')
        new.close()
        again = self.service(target='id:77')
        result = again.run(['7'], scan_strategy='incremental')
        self.assertEqual(result['fail'], 0, result)
        self.assertEqual(again.migrations.plan('IPX-201')['stage'], 'done')
        self.assertEqual(result['migrated'], 1, 'cleanup replay must still request media-server refresh')
        self.assertTrue({'7','77'}.issubset(self.cloud.entries))
        self.assertEqual(len(set(self.cloud.recycled)), len(self.cloud.recycled))

    def test_archive_off_during_pending_migration_stops_cloud_operations(self):
        first = self.service(target='id:77')
        first.run(['7'])
        first.close()
        second = self.service(target='id:88')
        self.cloud.interrupt_move_after = '1'
        result = second.run(['7'])
        self.assertEqual(result['fail'], 1, result)
        second.close()
        third = self.service(archive=False, rules='主力=アイデアポケット')
        before = (list(self.cloud.moves), list(self.cloud.renames), list(self.cloud.recycled))
        result = third.run(['7'])
        self.assertEqual(result['fail'], 0, result)
        self.assertEqual(before, (self.cloud.moves, self.cloud.renames, self.cloud.recycled))
        self.assertTrue((third.root/'主力/IPX-201/movie.nfo').exists())

    def test_archive_off_does_not_replay_pending_rename(self):
        first = self.service(layout='prefix')
        self.cloud.fail_rename = '1'
        self.assertEqual(first.run(['7'])['fail'], 1)
        first.close()
        self.cloud.fail_rename = None
        again = self.service(archive=False)
        before = list(self.cloud.renames)
        result = again.run(['7'])
        self.assertEqual(result['fail'], 0, result)
        self.assertEqual(self.cloud.renames, before)
        self.assertTrue((again.root/'IDEA POCKET/IPX-201/IPX-201 - V0001.strm').exists())

    def test_cloud_move_before_database_commit_replays_using_actual_file_id(self):
        first = self.service(target='id:77')
        first.run(['7'])
        token = (first.root/'IDEA POCKET/IPX-201/IPX-201 - V0001.strm').read_bytes()
        first.close()
        second = self.service(target='id:88')
        save = second.registry.save_version
        def interrupted(*args, **kwargs):
            folder = self.cloud.info(self.cloud.info('1').parent_id)
            if self.cloud.info(folder.parent_id).parent_id == '88':
                raise sqlite3.OperationalError('simulated commit interruption')
            return save(*args, **kwargs)
        with patch.object(second.registry, 'save_version', side_effect=interrupted):
            result = second.run(['7'], scan_strategy='incremental')
        self.assertTrue(result['stopped'], result)
        self.assertEqual(second.migrations.plan('IPX-201')['stage'], 'cloud_pending')
        second.close()
        third = self.service(target='id:88')
        result = third.run(['7'], scan_strategy='incremental')
        self.assertEqual(result['fail'], 0, result)
        self.assertEqual(third.migrations.plan('IPX-201')['stage'], 'done')
        self.assertEqual((third.root/'IDEA POCKET/IPX-201/IPX-201 - V0001.strm').read_bytes(), token)
        self.assertEqual(self.cloud.info('1').parent_id, self.cloud.info('2').parent_id)


class CloudCleanupTests(unittest.TestCase):
    def test_unicode_directory_creation_and_guarded_recycle(self):
        for version in ('v2','v3'):
            m = components(version)
            sdk = Mock()
            sdk.fs_mkdir.return_value = {'state':True, 'cid':'9'}
            client = m.Cloud115('UID=12345_A; CID=test; SEID=test', interval=0, sdk=sdk)
            self.assertEqual(client.mkdir('IDEA POCKET', '7'), '9')
            self.assertEqual(client.mkdir('主力厂商', '7'), '9')
            folder = m.Entry('9','7','主力厂商',is_dir=True)
            client.info = Mock(side_effect=[folder, folder, m.NotFoundError('gone')])
            client.listdir = Mock(return_value=[])
            self.assertTrue(client.recycle_empty_directory('9','7','主力厂商',{'7'}))
            self.assertEqual(client.listdir.call_count, 2)
            sdk.fs_delete.assert_called_once()
            self.assertEqual(sdk.fs_delete.call_args.args, ('9',))

    def test_recycle_rejects_identity_changes_and_nonempty_or_protected_directories(self):
        m = components()
        client = m.Cloud115('UID=12345_A; CID=test; SEID=test', interval=0, sdk=Mock())
        folder = m.Entry('9','7','S1',is_dir=True)
        client.info = Mock(return_value=folder)
        client.listdir = Mock(return_value=[m.Entry('1','9','unrelated.txt')])
        self.assertFalse(client.recycle_empty_directory('9','7','S1'))
        for args in [('9','7','changed',()), ('9','7','S1',{'9'}), ('0','7','S1',())]:
            with self.assertRaises(m.CloudError):
                client.recycle_empty_directory(*args)
        client.sdk.fs_delete.assert_not_called()


if __name__ == '__main__':
    unittest.main()
