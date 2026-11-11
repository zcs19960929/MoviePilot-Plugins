"""Display names are independent of registered version and playback identity."""
import dataclasses
import sqlite3
import types
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch

import test_metatube_maker as maker_fixtures


class NamingTests(unittest.TestCase):
    setUp = maker_fixtures.MakerLibraryTests.setUp
    service = maker_fixtures.MakerLibraryTests.service

    def legacy(self, count=1, second_ext='.AVI'):
        """Seed the actual 1.4.0 registry/output contract, without new name maps."""
        service = self.service(target='id:77', cleanup=False)
        if count > 1:
            self.cloud.entries['3'] = self.m.Entry('3','7','IPX-201 other'+second_ext,pickcode='c'*17)
        account, number = self.cloud.account, 'IPX-201'
        relative = 'IDEA POCKET/IPX-201'
        service.registry.ensure_movie(account, service.root, number, relative, self.metadata.return_value)
        category = self.cloud.mkdir('IDEA POCKET', '77')
        destination = self.cloud.mkdir(number, category)
        folder = service.root/relative
        folder.mkdir(parents=True)
        videos = [self.cloud.info('1')] + ([self.cloud.info('3')] if count > 1 else [])
        assigned = service.registry.assign_versions(account, service.root, number, [[e] for e in videos])
        for video in videos:
            version = assigned[video.id]
            stem = f'{number} - V{version:04d}'+self.m._part(video.name)
            files = [dict(entry=asdict(video), kind='video', target=stem+Path(video.name).suffix,
                          source_name=video.name, source_parent=video.parent_id)]
            if video.id == '1':
                subtitle = self.cloud.info('2')
                files.append(dict(entry=asdict(subtitle), kind='subtitle', target=stem+'.zh.ass'))
            for item in files:
                entry = self.cloud.info(item['entry']['id'])
                service.migrations.record_origin(entry, '7', ['7','0'])
                self.cloud.rename(entry.id, item['target'])
                self.cloud.move([entry.id], destination)
                entry = self.cloud.info(entry.id)
                item['entry'] = asdict(entry)
                if item['kind'] == 'video':
                    token = service.registry.media_token(account, entry)
                    filename, content = stem+'.strm', (service.base+'/api/v1/plugin/MetaTubeScraper/redirect_url?media_id='+token+'\n').encode()
                else:
                    filename, content = item['target'], b'subtitle:2'
                path, owner = folder/filename, self.m.version_job(service.root, number, version)
                service.registry.reserve(path, account, owner, content)
                path.write_bytes(content)
                service.registry.published(path, content)
            service.registry.save_version(account, service.root, number, version, files, complete=True)
        for name, content in [('movie.nfo',service._render_nfo(self.metadata.return_value,number)),
                              ('poster.jpg',b'image'),('fanart.jpg',b'image')]:
            path = folder/name
            service.registry.reserve(path, account, self.m.movie_job(service.root,number), content)
            path.write_bytes(content)
            service.registry.published(path, content)
        with service.registry.db:
            service.registry.db.execute('UPDATE library_movies SET cloud_root=?,cloud_folder=?',('77',destination))
        return service, folder, destination

    def permanently_delete(self, fid):
        gone = self.cloud.entries.pop(fid)
        sdk = types.SimpleNamespace(fs_file=Mock(return_value={'state': True, 'data': [
            {'fid': fid, 'cid': '0', 'n': gone.name, 'aid': 120},
        ]}))
        client = self.m.Cloud115('UID=12345_A1; CID=fixture; SEID=fixture', sdk=sdk, interval=0)
        info = self.cloud.info
        self.cloud.info = lambda key: client.info(key) if key == fid else info(key)

    def test_first_copy_is_plain_and_extensions_are_preserved(self):
        self.cloud.entries['1'] = dataclasses.replace(self.cloud.info('1'),name='IPX-201.MP4')
        service = self.service(target='id:77')
        result = service.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual(self.cloud.info('1').name,'IPX-201.MP4')
        folder = service.root/'IDEA POCKET/IPX-201'
        self.assertTrue((folder/'IPX-201.strm').is_file())
        self.assertEqual((folder/'IPX-201.zh.ass').read_bytes(),b'subtitle:2')
        self.assertEqual(service.run(['7'])['skip'],0)  # Source is now empty.

    def test_new_copies_use_one_namespace_across_extensions_and_remain_stable(self):
        service = self.service(target='id:77')
        self.assertEqual(service.run(['7'])['ok'],1)
        self.cloud.entries['3'] = self.m.Entry('3','7','IPX-201 another.AVI',pickcode='c'*17)
        self.assertEqual(service.run(['7'])['ok'],1)
        self.cloud.entries['4'] = self.m.Entry('4','7','IPX-201 third.mkv',pickcode='d'*17)
        result = service.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual([self.cloud.info(fid).name for fid in ('1','3','4')],
                         ['IPX-201.mp4','IPX-201 - V0001.AVI','IPX-201 - V0002.mkv'])
        self.assertEqual(len(list((service.root/'IDEA POCKET/IPX-201').glob('*.strm'))),3)

    def test_unregistered_video_at_destination_reserves_plain_name(self):
        category = self.cloud.mkdir('IDEA POCKET','77')
        movie = self.cloud.mkdir('IPX-201',category)
        self.cloud.entries['8'] = self.m.Entry('8',movie,'IPX-201.mp4',pickcode='z'*17)
        self.cloud.entries['1'] = dataclasses.replace(self.cloud.info('1'),name='IPX-201.AVI')
        service = self.service(target='id:77')
        result = service.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual(self.cloud.info('1').name,'IPX-201 - V0001.AVI')
        self.assertEqual(self.cloud.info('8').name,'IPX-201.mp4')

    def test_legacy_single_copy_is_renamed_and_playback_content_is_unchanged(self):
        old, folder, _ = self.legacy()
        link = (folder/'IPX-201 - V0001.strm').read_bytes()
        old.close()
        service = self.service(target='id:77')
        result = service.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual(self.cloud.info('1').name,'IPX-201.mp4')
        self.assertEqual((folder/'IPX-201.strm').read_bytes(),link)
        self.assertFalse((folder/'IPX-201 - V0001.strm').exists())
        self.assertEqual((folder/'IPX-201.zh.ass').read_bytes(),b'subtitle:2')
        self.assertEqual(self.cloud.downloads,[])

    def test_legacy_multiple_versions_shift_names_without_changing_ids(self):
        old, folder, _ = self.legacy(2)
        links = [(folder/f'IPX-201 - V{i:04d}.strm').read_bytes() for i in (1,2)]
        old.close()
        service = self.service(target='id:77')
        result = service.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual([self.cloud.info(fid).name for fid in ('1','3')],['IPX-201.mp4','IPX-201 - V0001.AVI'])
        self.assertEqual([(folder/name).read_bytes() for name in ('IPX-201.strm','IPX-201 - V0001.strm')],links)
        self.assertEqual([v['version'] for v in service.registry.versions(self.cloud.account,service.root,'IPX-201')],[1,2])

    def test_migration_retires_permanently_deleted_version_and_keeps_live_playback(self):
        old, folder, _ = self.legacy(2, second_ext='.mp4')
        live = (folder/'IPX-201 - V0001.strm').read_bytes()
        stale = folder/'IPX-201 - V0002.strm'
        token = stale.read_text().strip().split('media_id=')[1]
        old.close()
        self.permanently_delete('3')
        self.cloud.rename('1', 'IPX-201.mp4')
        service = self.service(target='id:77')
        result = service.run(['7'])
        self.assertEqual(result['fail'], 0, result)
        self.assertEqual(result['library_changed'], 1)
        self.assertFalse(stale.exists())
        self.assertIsNone(service.registry._output(stale))
        self.assertIsNone(service.registry.db.execute('SELECT token FROM media WHERE token=?', (token,)).fetchone())
        self.assertEqual((folder/'IPX-201.strm').read_bytes(), live)
        versions = service.registry.versions(self.cloud.account, service.root, 'IPX-201')
        self.assertEqual([row['version'] for row in versions], [1])
        self.assertEqual(service.run(['7'])['library_changed'], 0)

    def test_normal_scan_retires_deleted_copy_but_keeps_manually_edited_strm(self):
        del self.cloud.entries['2']
        self.cloud.entries['3'] = self.m.Entry('3', '7', 'IPX-201 alternate.mp4', pickcode='c'*17)
        service = self.service(archive=False)
        first = service.run(['7'])
        self.assertEqual(first['ok'], 1, first)
        self.assertEqual(first['library_changed'], 1)
        folder = service.root/'IDEA POCKET/IPX-201'
        live = (folder/'IPX-201.strm').read_bytes()
        edited = folder/'IPX-201 - V0001.strm'
        token = edited.read_text().strip().split('media_id=')[1]
        edited.write_bytes(b'manually edited')
        self.permanently_delete('3')

        result = service.run(['7'])
        self.assertEqual(result['fail'], 0, result)
        self.assertEqual(result['library_changed'], 1)
        self.assertEqual(edited.read_bytes(), b'manually edited')
        self.assertIsNone(service.registry._output(edited))
        self.assertIsNone(service.registry.db.execute('SELECT token FROM media WHERE token=?', (token,)).fetchone())
        self.assertEqual((folder/'IPX-201.strm').read_bytes(), live)

    def test_ambiguous_missing_file_does_not_retire_version(self):
        old, folder, _ = self.legacy(2)
        old.close()
        del self.cloud.entries['3']
        service = self.service(target='id:77')
        result = service.run(['7'])
        self.assertEqual(result['fail'], 1, result)
        self.assertTrue((folder/'IPX-201 - V0002.strm').is_file())
        self.assertEqual(len(service.registry.versions(self.cloud.account, service.root, 'IPX-201')), 2)

    def test_unowned_local_strm_reserves_the_slot_without_being_adopted(self):
        service = self.service(target='id:77')
        folder = service.root/'IDEA POCKET/IPX-201'
        folder.mkdir(parents=True)
        (folder/'IPX-201.strm').write_bytes(b'unowned movie')
        result = service.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual(self.cloud.info('1').name,'IPX-201 - V0001.mp4')
        self.assertEqual((folder/'IPX-201.strm').read_bytes(),b'unowned movie')
        self.assertTrue((folder/'IPX-201 - V0001.strm').is_file())

    def test_an_existing_plain_copy_wins_over_internal_version_order(self):
        del self.cloud.entries['2']
        category = self.cloud.mkdir('IDEA POCKET','77')
        destination = self.cloud.mkdir('IPX-201',category)
        self.cloud.entries['1'] = dataclasses.replace(self.cloud.info('1'),parent_id=destination,name='IPX-201 extra.mp4')
        self.cloud.entries['3'] = self.m.Entry('3',destination,'IPX-201.AVI',pickcode='c'*17)
        service = self.service(target='id:77')
        result = service.run(['77'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual(self.cloud.info('3').name,'IPX-201.AVI')
        self.assertEqual(self.cloud.info('1').name,'IPX-201 - V0001.mp4')

    def test_later_cd_part_and_subtitle_keep_the_plain_version_slot(self):
        self.cloud.entries['1'] = dataclasses.replace(self.cloud.info('1'),name='IPX-201-CD1.mp4')
        self.cloud.entries['2'] = dataclasses.replace(self.cloud.info('2'),name='IPX-201-CD1.zh.ass')
        service = self.service(target='id:77')
        self.assertEqual(service.run(['7'])['fail'],0)
        self.cloud.entries['3'] = self.m.Entry('3','7','IPX-201-CD2.MKV',pickcode='c'*17)
        self.cloud.entries['4'] = self.m.Entry('4','7','IPX-201-CD2.en.srt',pickcode='d'*17)
        result = service.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual(self.cloud.info('3').name,'IPX-201-CD2.MKV')
        folder = service.root/'IDEA POCKET/IPX-201'
        self.assertEqual(sorted(p.name for p in folder.glob('*.strm')),['IPX-201-CD1.strm','IPX-201-CD2.strm'])
        self.assertEqual((folder/'IPX-201-CD2.en.srt').read_bytes(),b'subtitle:4')
        self.assertEqual(len(service.registry.versions(self.cloud.account,service.root,'IPX-201')),1)

    def test_modified_legacy_strm_blocks_all_rename_operations(self):
        old, folder, _ = self.legacy(2)
        (folder/'IPX-201 - V0001.strm').write_bytes(b'manually modified')
        before = list(self.cloud.renames)
        old.close()
        service = self.service(target='id:77')
        result = service.run(['7'])
        self.assertEqual(result['fail'],1,result)
        self.assertEqual(self.cloud.renames,before)
        self.assertEqual((folder/'IPX-201 - V0001.strm').read_bytes(),b'manually modified')
        self.assertFalse((folder/'IPX-201.strm').exists())

    def test_lost_cloud_rename_response_recovers_from_temporary_name(self):
        old, folder, _ = self.legacy(2,second_ext='.mp4')
        link = (folder/'IPX-201 - V0001.strm').read_bytes()
        old.close()
        service = self.service(target='id:77')
        self.cloud.raise_after_rename = '1'
        result = service.run(['7'],scan_strategy='incremental')
        self.assertTrue(result.get('stopped'),result)
        self.assertTrue(self.cloud.info('1').name.startswith('.metatube-rename-'))
        service.close()
        again = self.service(target='id:77')
        result = again.run(['7'],scan_strategy='incremental')
        self.assertEqual(result['fail'],0,result)
        self.assertEqual(self.cloud.info('1').name,'IPX-201.mp4')
        self.assertEqual((folder/'IPX-201.strm').read_bytes(),link)
        self.assertFalse(any(p.name.startswith('.metatube-rename-') for p in folder.iterdir()))

    def test_local_link_before_acknowledgement_replays_without_overwrite(self):
        old, folder, _ = self.legacy(2)
        links = [(folder/f'IPX-201 - V{i:04d}.strm').read_bytes() for i in (1,2)]
        old.close()
        service = self.service(target='id:77')
        with patch.object(service.migrations,'_acknowledge',side_effect=sqlite3.OperationalError('interruption')):
            result = service.run(['7'],scan_strategy='incremental')
        self.assertTrue(result.get('stopped'),result)
        service.close()
        again = self.service(target='id:77')
        result = again.run(['7'],scan_strategy='incremental')
        self.assertEqual(result['fail'],0,result)
        self.assertEqual([(folder/n).read_bytes() for n in ('IPX-201.strm','IPX-201 - V0001.strm')],links)

    def test_cloud_rename_is_deferred_when_organizing_is_disabled(self):
        old, folder, _ = self.legacy()
        old.close()
        disabled = self.service(target='id:77',archive=False)
        before = list(self.cloud.renames)
        result = disabled.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertTrue((folder/'IPX-201.strm').exists())
        self.assertEqual(self.cloud.renames,before)
        self.assertTrue(disabled.naming.job('IPX-201')['cloud_deferred'])
        disabled.run(['7'])
        self.assertEqual(self.cloud.renames,before)
        disabled.close()
        enabled = self.service(target='id:77')
        result = enabled.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual(self.cloud.info('1').name,'IPX-201.mp4')
        self.assertFalse(enabled.naming.job('IPX-201')['cloud_deferred'])

    def test_new_subtitle_after_migration_uses_the_renamed_video(self):
        old, folder, _ = self.legacy()
        old.close()
        service = self.service(target='id:77')
        self.assertEqual(service.run(['7'])['fail'],0)
        self.cloud.entries['4'] = self.m.Entry('4','7','IPX-201.fr.srt',pickcode='d'*17)
        result = service.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual((folder/'IPX-201.fr.srt').read_bytes(),b'subtitle:4')
        self.assertEqual(self.cloud.downloads,['4'])

    def test_existing_plain_and_v0001_foreign_files_force_v0002(self):
        category = self.cloud.mkdir('IDEA POCKET','77')
        destination = self.cloud.mkdir('IPX-201',category)
        self.cloud.entries['8'] = self.m.Entry('8',destination,'IPX-201.mp4',pickcode='z'*17)
        self.cloud.entries['9'] = self.m.Entry('9',destination,'IPX-201 - V0001.mkv',pickcode='y'*17)
        self.cloud.entries['1'] = dataclasses.replace(self.cloud.info('1'),name='IPX-201.AVI')
        service = self.service(target='id:77')
        result = service.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual(self.cloud.info('1').name,'IPX-201 - V0002.AVI')
        self.assertEqual(self.cloud.info('8').name,'IPX-201.mp4')
        self.assertEqual(self.cloud.info('9').name,'IPX-201 - V0001.mkv')

    def test_legacy_migration_precedes_discovery_and_obeys_movie_limit(self):
        old, folder, _ = self.legacy()
        old.close()
        self.cloud.entries['4'] = self.m.Entry('4','7','IPX-202 incoming.mp4',pickcode='d'*17)
        self.cloud.listdir = Mock(wraps=self.cloud.listdir)
        service = self.service(target='id:77')
        result = service.run(['7'],limit=1,scan_strategy='incremental')
        self.assertEqual((result['ok'],result['fail']),(1,0),result)
        self.assertFalse(result['scan_cycle_complete'])
        self.assertTrue((folder/'IPX-201.strm').exists())
        self.assertFalse(any(call.args==('7',) for call in self.cloud.listdir.call_args_list))
        self.assertEqual(self.cloud.info('4').name,'IPX-202 incoming.mp4')
        next_result = service.run(['7'],limit=1,scan_strategy='incremental')
        self.assertEqual((next_result['ok'],next_result['fail']),(1,0),next_result)

    def test_local_interruption_can_resume_with_new_category_and_cloud_root(self):
        old, folder, _ = self.legacy(2)
        link = (folder/'IPX-201 - V0001.strm').read_bytes()
        old.close()
        service = self.service(target='id:77')
        with patch.object(service.migrations,'_acknowledge',side_effect=OSError('power loss')):
            result = service.run(['7'])
        self.assertTrue(result.get('stopped'),result)
        service.close()
        again = self.service(target='id:88',rules='主力=アイデアポケット')
        result = again.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual((again.root/'主力/IPX-201/IPX-201.strm').read_bytes(),link)
        cloud_movie = self.cloud.info(self.cloud.info('1').parent_id)
        self.assertEqual(self.cloud.info(cloud_movie.parent_id).parent_id,'88')

    def test_disabling_archive_while_cloud_names_are_temporary_freezes_cloud_steps(self):
        old, _, _ = self.legacy(2,second_ext='.mp4')
        old.close()
        service = self.service(target='id:77')
        self.cloud.raise_after_rename='1'
        service.run(['7'])
        before = {fid:(e.name,e.parent_id) for fid,e in self.cloud.entries.items()}
        service.close()
        disabled = self.service(target='id:77',archive=False)
        result = disabled.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual({fid:(e.name,e.parent_id) for fid,e in self.cloud.entries.items()},before)
        disabled.close()
        again = self.service(target='id:77')
        result = again.run(['7'])
        self.assertEqual(result['fail'],0,result)
        self.assertEqual(self.cloud.info('1').name,'IPX-201.mp4')

    def test_failed_artwork_before_first_publication_does_not_lose_file_registration(self):
        service = self.service()
        service.artwork = Mock(return_value=None)
        self.assertEqual(service.run(['7'])['fail'],1)
        self.assertEqual(self.cloud.renames,[])
        service.artwork = lambda info,kind:b'image'
        result = service.run(['7'])
        self.assertEqual((result['ok'],result['fail']),(1,0),result)
        self.assertTrue((service.root/'IDEA POCKET/IPX-201/IPX-201.strm').exists())
