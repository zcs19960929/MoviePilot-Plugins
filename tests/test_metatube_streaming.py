"""Observable directory-by-directory publication, using an offline cloud."""
import tempfile
import dataclasses
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

import test_metatube_library as fixtures
from test_metatube_direct import components


class StreamingTests(unittest.TestCase):
    make = fixtures.LibraryAcceptanceTests.make

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def library(self, entries, archive=True):
        return self.make(self.temporary.name, entries, archive)

    def test_first_movie_published_and_archived_before_next_directory_read(self):
        _, cloud, _, service = self.library([
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='IPX-201', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='IPX-202', is_dir=True),
            dict(id='2', parent_id='9', name='IPX-202.mp4', pickcode='b'*17),
        ])
        original = cloud.listdir
        observations = []
        def listing(cid):
            if cid == '9':
                observations.append((
                    (service.root/'IPX/IPX-201/IPX-201 - V0001.strm').is_file(),
                    cloud.entries[cloud.entries['8'].parent_id].name,
                ))
            return original(cid)
        cloud.listdir = listing
        result = service.run(['7'])
        self.assertTrue(observations)
        self.assertEqual(observations[0], (True, 'IPX'))
        self.assertEqual(result['ok'], 2)

    def test_limit_stops_before_reading_next_directory(self):
        _, cloud, _, service = self.library([
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='IPX-201', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='IPX-202', is_dir=True),
            dict(id='2', parent_id='9', name='IPX-202.mp4', pickcode='b'*17),
        ])
        cloud.listdir = Mock(wraps=cloud.listdir)
        result = service.run(['7'], limit=1)
        self.assertEqual(result['ok'], 1)
        self.assertFalse(any(call.args == ('9',) for call in cloud.listdir.call_args_list))
        self.assertEqual(cloud.entries['2'].name, 'IPX-202.mp4')

    def test_completed_movie_skip_does_not_consume_next_run_limit(self):
        m, cloud, _, service = self.library([
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='IPX-201', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='incoming', is_dir=True),
        ], archive=False)
        self.assertEqual(service.run(['7'])['ok'], 1)
        cloud.entries['2'] = m.Entry('2', '9', 'IPX-202.mp4', pickcode='b'*17)
        result = service.run(['7'], limit=1)
        self.assertEqual((result['total'], result['ok'], result['skip']), (2, 1, 1))
        self.assertTrue((service.root/'IPX/IPX-202/IPX-202 - V0001.strm').is_file())

    def test_notfound_is_cached_and_counted_once_across_batches(self):
        _, cloud, metadata, service = self.library([
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='first', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='second', is_dir=True),
            dict(id='2', parent_id='9', name='IPX-201-other.mp4', pickcode='b'*17),
            dict(id='3', parent_id='9', name='IPX-202.mp4', pickcode='c'*17),
        ])
        metadata.return_value = None
        result = service.run(['7'], limit=2)
        self.assertEqual((result['total'], result['notfound']), (2, 2))
        self.assertEqual(metadata.call_count, 2)
        self.assertEqual(cloud.renames, [])

    def test_later_registered_version_still_repairs_its_missing_strm(self):
        _, cloud, _, service = self.library([
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='first', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='second', is_dir=True),
            dict(id='2', parent_id='9', name='IPX-201-other.mkv', pickcode='b'*17),
        ], archive=False)
        self.assertEqual(service.run(['7'])['ok'], 1)
        second = service.root/'IPX/IPX-201/IPX-201 - V0002.strm'
        original_link = second.read_bytes()
        second.unlink()
        result = service.run(['7'])
        self.assertEqual(result['fail'], 0)
        self.assertTrue(second.is_file())
        self.assertEqual(second.read_bytes(), original_link)

    def test_flat_and_classified_sources_merge_without_processing_moved_video_twice(self):
        _, cloud, metadata, service = self.library([
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='IPX-201', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='IPX', is_dir=True),
            dict(id='10', parent_id='9', name='IPX-201', is_dir=True),
            dict(id='2', parent_id='10', name='IPX-201-other.mkv', pickcode='b'*17),
        ])
        service.force = True
        service.artwork = Mock(return_value=b'image')
        result = service.run(['7'])
        movie = service.root/'IPX/IPX-201'
        self.assertEqual(sorted(p.name for p in movie.glob('*.strm')),
                         ['IPX-201 - V0001.strm', 'IPX-201 - V0002.strm'])
        self.assertEqual(cloud.entries['1'].parent_id, '10')
        self.assertEqual(cloud.entries['2'].parent_id, '10')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['ok'], 1)
        self.assertEqual(metadata.call_count, 1)
        self.assertEqual(service.artwork.call_count, 2)
        self.assertEqual([fid for fid, _ in cloud.renames].count('1'), 1)

    def test_new_subtitle_uses_already_processed_video_as_anchor(self):
        m, cloud, _, service = self.library([
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='IPX-201', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='IPX', is_dir=True),
            dict(id='10', parent_id='9', name='IPX-201', is_dir=True),
        ])
        original = cloud.listdir
        def listing(cid):
            if cid == '10' and cloud.entries['1'].parent_id == '10':
                cloud.entries.setdefault('3', m.Entry('3', '10', 'IPX-201.zh.ass', pickcode='c'*17))
            return original(cid)
        cloud.listdir = listing
        result = service.run(['7'])
        self.assertEqual(result['fail'], 0)
        self.assertEqual((service.root/'IPX/IPX-201/IPX-201 - V0001.zh.ass').read_bytes(), b'subtitle:3')
        files = service.registry.versions(cloud.account, service.root, 'IPX-201')[0]['files']
        self.assertEqual({item['entry']['id'] for item in files}, {'1', '3'})

    def test_later_batch_does_not_replace_existing_multipart_registration(self):
        m, cloud, _, service = self.library([
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='IPX-201', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201-CD1.mp4', pickcode='a'*17),
            dict(id='11', parent_id='7', name='IPX', is_dir=True),
            dict(id='12', parent_id='11', name='IPX-201', is_dir=True),
        ])
        self.assertEqual(service.run(['7'])['ok'], 1)
        original_link = (service.root/'IPX/IPX-201/IPX-201 - V0001-CD1.strm').read_bytes()
        # A new upload recreates the old incoming directory after empty cleanup.
        cloud.entries['8'] = m.Entry('8', '7', 'IPX-201', is_dir=True)
        cloud.entries['2'] = m.Entry('2', '8', 'IPX-201-CD2.mp4', pickcode='b'*17)
        result = service.run(['7'])
        self.assertEqual(result['fail'], 0)
        files = service.registry.versions(cloud.account, service.root, 'IPX-201')[0]['files']
        self.assertEqual({item['entry']['id'] for item in files}, {'1', '2'})
        self.assertEqual(sorted(p.name for p in (service.root/'IPX/IPX-201').glob('*.strm')),
                         ['IPX-201 - V0001-CD1.strm', 'IPX-201 - V0001-CD2.strm'])
        self.assertEqual((service.root/'IPX/IPX-201/IPX-201 - V0001-CD1.strm').read_bytes(), original_link)

    def test_later_failed_batch_dominates_success_without_double_counting_movie(self):
        _, cloud, _, service = self.library([
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='first', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
            dict(id='9', parent_id='7', name='second', is_dir=True),
            dict(id='2', parent_id='9', name='IPX-201-other.mkv', pickcode='b'*17),
        ], archive=False)
        cloud.fail_rename = '2'
        result = service.run(['7'])
        self.assertTrue((service.root/'IPX/IPX-201/IPX-201 - V0001.strm').is_file())
        self.assertEqual((result['total'], result['ok'], result['fail']), (1, 0, 1))

    def test_overlapping_selected_roots_share_parent_archive_scope(self):
        _, cloud, _, service = self.library([
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='incoming', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201.mp4', pickcode='a'*17),
        ])
        result = service.run(['7', '8'])
        self.assertEqual(result['fail'], 0)
        prefix = cloud.entries[cloud.entries['1'].parent_id]
        self.assertEqual(prefix.name, 'IPX-201')
        self.assertEqual(cloud.entries[prefix.parent_id].name, 'IPX')
        self.assertEqual(cloud.entries[prefix.parent_id].parent_id, '7')
        self.assertEqual(result['total'], 1)

    def test_recovery_does_not_mutate_registered_parts_outside_scan_depth(self):
        _, cloud, _, service = self.library([
            dict(id='7', parent_id='0', name='jav', is_dir=True),
            dict(id='8', parent_id='7', name='IPX-201', is_dir=True),
            dict(id='1', parent_id='8', name='IPX-201-CD1.mp4', pickcode='a'*17),
            dict(id='2', parent_id='8', name='IPX-201-CD2.mp4', pickcode='b'*17),
            dict(id='9', parent_id='7', name='deep', is_dir=True),
            dict(id='10', parent_id='9', name='deeper', is_dir=True),
        ], archive=False)
        self.assertEqual(service.run(['7'])['ok'], 1)
        cloud.entries['1'] = dataclasses.replace(cloud.entries['1'], parent_id='10', name='IPX-201-CD1.mp4')
        before = list(cloud.renames)
        service.run(['7'])
        self.assertEqual(cloud.entries['1'].name, 'IPX-201-CD1.mp4')
        self.assertEqual(cloud.renames, before)

    def test_recovery_requires_the_same_category_depth_rule_as_scanning(self):
        for classified in (False, True):
            with self.subTest(classified=classified), tempfile.TemporaryDirectory() as temporary:
                m, cloud, _, service = self.make(temporary, [
                    dict(id='7', parent_id='0', name='jav', is_dir=True),
                    dict(id='8', parent_id='7', name='incoming', is_dir=True),
                    dict(id='1', parent_id='8', name='IPX-201-CD1.mp4', pickcode='a'*17),
                    dict(id='2', parent_id='8', name='IPX-201-CD2.mp4', pickcode='b'*17),
                    dict(id='9', parent_id='7', name='IPX', is_dir=True),
                    dict(id='10', parent_id='9', name='deep', is_dir=True),
                ])
                self.assertEqual(service.run(['7'])['ok'], 1)
                cloud.entries['1'] = dataclasses.replace(cloud.entries['1'], parent_id='10', name='IPX-201-CD1.mp4')
                if classified:
                    cloud.entries['11'] = m.Entry('11', '9', 'IPX-999', is_dir=True)
                service.run(['7'])
                self.assertEqual(cloud.entries['1'].name,
                                 'IPX-201 - V0001-CD1.mp4' if classified else 'IPX-201-CD1.mp4')

    def test_recovery_does_not_merge_parts_from_independent_archive_roots(self):
        _, cloud, _, service = self.library([
            dict(id='7', parent_id='0', name='source-a', is_dir=True),
            dict(id='8', parent_id='0', name='source-b', is_dir=True),
            dict(id='1', parent_id='7', name='IPX-201-CD1.mp4', pickcode='a'*17),
            dict(id='2', parent_id='7', name='IPX-201-CD2.mp4', pickcode='b'*17),
        ], archive=False)
        self.assertEqual(service.run(['7', '8'])['ok'], 1)
        cloud.entries['2'] = dataclasses.replace(cloud.entries['2'], parent_id='8')
        service.archive_loose = True
        result = service.run(['7', '8'])
        self.assertEqual(cloud.entries['2'].parent_id, '8')
        self.assertFalse(any('2' in fids for fids, _ in cloud.moves))
        self.assertEqual(result['fail'], 1)
        self.assertIn('共同上级', ' '.join(result['errors']))

    def test_current_directory_pagination_finishes_before_first_mutation(self):
        entries = [dict(id='1', parent_id='0', name='IPX-201.mp4', pickcode='a'*17)]
        entries += [dict(id=str(1000+i), parent_id='0', name=f'note-{i}.txt') for i in range(499)]
        entries += [dict(id='2', parent_id='0', name='IPX-201.zh.ass', pickcode='b'*17)]
        _, cloud, _, service = self.library(entries, archive=False)
        pages = []
        def page(payload, **kwargs):
            offset = payload['offset']
            if len(pages) < 2:
                self.assertEqual(cloud.renames, [])
            pages.append(offset)
            raw = [dict(id=e.id, parent_id=e.parent_id, name=e.name, is_dir=e.is_dir,
                        pickcode=e.pickcode) for e in cloud.entries.values()]
            return dict(state=True, count=501, path=[{'cid':'0'}], data=raw[offset:offset+500])
        adapter = components().Cloud115('UID=12345_A1; CID=fixture; SEID=fixture', interval=0,
                                       sdk=types.SimpleNamespace(fs_files=page))
        cloud.listdir = adapter.listdir
        result = service.run(['0'])
        self.assertEqual(result['ok'], 1)
        self.assertEqual(pages[:2], [0, 500])
        self.assertEqual((service.root/'IPX/IPX-201/IPX-201 - V0001.zh.ass').read_bytes(), b'subtitle:2')


if __name__ == '__main__':
    unittest.main()
