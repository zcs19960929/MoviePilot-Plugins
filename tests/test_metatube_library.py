"""Offline movie-level version registration and classified output acceptance."""
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

from test_metatube_auth import load_plugin
from test_metatube_direct import FakeCloud

ROOT = Path(__file__).resolve().parents[1]


def library_module():
    name = 'metatube_library_acceptance'
    package = types.ModuleType(name)
    package.__path__ = [str(ROOT/'plugins.v3/metatubescraper')]
    sys.modules[name] = package
    return importlib.import_module(name+'.library')


class LibraryAcceptanceTests(unittest.TestCase):
    def test_same_movie_in_separate_scan_roots_does_not_move_between_roots(self):
        with tempfile.TemporaryDirectory() as folder:
            _, cloud, _, service = self.make(folder, [
                dict(id='7', parent_id='0', name='source-a', is_dir=True),
                dict(id='8', parent_id='0', name='source-b', is_dir=True),
                dict(id='1', parent_id='7', name='IPX-922-A.mp4', pickcode='a'*17),
                dict(id='2', parent_id='8', name='IPX-922-B.mkv', pickcode='b'*17),
            ], archive=True)
            result = service.run(['7', '8'])
            self.assertEqual(result['fail'], 1)
            self.assertIn('共同上级', ' '.join(result['errors']))
            self.assertEqual(cloud.moves, [(['1'], '10')])
            self.assertEqual(cloud.renames, [('1', 'IPX-922.mp4')])
            self.assertTrue((service.root/'IPX/IPX-922/IPX-922.strm').is_file())
            self.assertEqual(cloud.entries['2'].name, 'IPX-922-B.mkv')

    def make(self, directory, entries, archive=False):
        m = library_module()
        cloud = FakeCloud(m, [m.Entry(**entry) for entry in entries])
        metadata = Mock(return_value={'provider':'JAV321','id':'film','title':'Title'})
        service = m.LibraryService(cloud, Path(directory)/'state', Path(directory)/'library',
            'https://mp.example', metadata, lambda info,kind:b'image',
            lambda info,number:b'<movie><title>Example</title></movie>',
            load_plugin('v3', Mock())._MetaTubeScraper__extract_number,
            archive_loose=archive, error=Mock())
        self.addCleanup(service.close)
        return m, cloud, metadata, service

    def test_two_sources_share_metadata_and_keep_stable_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            m,cloud,metadata,service = self.make(folder,[
                dict(id='7',parent_id='0',name='jav',is_dir=True),
                dict(id='8',parent_id='7',name='IPX-922 old',is_dir=True),
                dict(id='1',parent_id='7',name='IPX-922-U.mp4',pickcode='a'*17),
                dict(id='2',parent_id='8',name='IPX-922.mp4',pickcode='b'*17),
                dict(id='3',parent_id='7',name='IPX-922-U.zh.ass',pickcode='c'*17),
            ])
            self.assertEqual(service.run(['7'])['ok'],1)
            movie = Path(folder)/'library/IPX/IPX-922'
            self.assertEqual(sorted(p.name for p in movie.glob('*.strm')),
                ['IPX-922 - V0001.strm','IPX-922.strm'])
            self.assertEqual(metadata.call_count,1)
            tokens = {p.name:p.read_bytes() for p in movie.glob('*.strm')}
            self.assertEqual(service.run(['7'])['skip'],1)
            self.assertEqual(tokens,{p.name:p.read_bytes() for p in movie.glob('*.strm')})
            self.assertEqual(len(cloud.renames),3)




    def test_classified_scan_does_not_consume_movie_depth(self):
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,_,service = self.make(folder,[
                dict(id='7',parent_id='0',name='jav',is_dir=True),
                dict(id='8',parent_id='7',name='IPX',is_dir=True),
                dict(id='9',parent_id='8',name='IPX-922',is_dir=True),
                dict(id='1',parent_id='9',name='IPX-922.mp4',pickcode='a'*17),
            ])
            self.assertEqual(service.run(['7'])['ok'],1)

    def test_distinct_multipart_versions_and_ambiguous_subtitle(self):
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,_,service = self.make(folder,[
                dict(id='1',parent_id='0',name='IPX-922-A-CD1.mp4',pickcode='a'*17),
                dict(id='2',parent_id='0',name='IPX-922-A-CD2.mp4',pickcode='b'*17),
                dict(id='3',parent_id='0',name='IPX-922-B-CD1.mkv',pickcode='c'*17),
                dict(id='4',parent_id='0',name='IPX-922-B-CD2.mkv',pickcode='d'*17),
            ])
            self.assertEqual(service.run(['0'])['ok'],1)
            self.assertEqual(len(list((Path(folder)/'library/IPX/IPX-922').glob('*.strm'))),4)

    def test_archive_reuses_movie_directory_and_merges_new_version(self):
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,_,service = self.make(folder,[
                dict(id='7',parent_id='0',name='jav',is_dir=True),
                dict(id='8',parent_id='7',name='IPX',is_dir=True),
                dict(id='9',parent_id='8',name='IPX-922',is_dir=True),
                dict(id='1',parent_id='9',name='IPX-922.mp4',pickcode='a'*17),
                dict(id='2',parent_id='7',name='IPX-922-U.mkv',pickcode='b'*17),
            ], archive=True)
            self.assertEqual(service.run(['7'])['ok'],1)
            self.assertEqual(cloud.entries['1'].parent_id,'9')
            self.assertEqual(cloud.entries['2'].parent_id,'9')
            self.assertEqual(service.run(['7'])['skip'],1)

    def test_later_part_joins_original_version(self):
        with tempfile.TemporaryDirectory() as folder:
            m,cloud,_,service = self.make(folder,[dict(id='1',parent_id='0',name='IPX-922-A-CD1.mp4',pickcode='a'*17)])
            self.assertEqual(service.run(['0'])['ok'],1)
            cloud.entries['2']=m.Entry('2','0','IPX-922-A-CD2.mp4',pickcode='b'*17)
            self.assertEqual(service.run(['0'])['ok'],1)
            files=list((service.root/'IPX/IPX-922').glob('*.strm'))
            self.assertEqual(sorted(p.name for p in files),['IPX-922-CD1.strm','IPX-922-CD2.strm'])

    def test_subtitle_same_basename_is_bound_to_source_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,_,service=self.make(folder,[
                dict(id='7',parent_id='0',name='jav',is_dir=True),
                dict(id='8',parent_id='7',name='IPX-922-A',is_dir=True),
                dict(id='9',parent_id='7',name='IPX-922-B',is_dir=True),
                dict(id='1',parent_id='8',name='IPX-922.mp4',pickcode='a'*17),
                dict(id='2',parent_id='9',name='IPX-922.mp4',pickcode='b'*17),
                dict(id='3',parent_id='8',name='IPX-922.zh.ass',pickcode='c'*17),
                dict(id='4',parent_id='9',name='IPX-922.zh.ass',pickcode='d'*17),
            ])
            self.assertEqual(service.run(['7'])['ok'],1)
            self.assertEqual((service.root/'IPX/IPX-922/IPX-922.zh.ass').read_bytes(),b'subtitle:3')
            self.assertEqual((service.root/'IPX/IPX-922/IPX-922 - V0001.zh.ass').read_bytes(),b'subtitle:4')

    def test_ambiguous_subtitle_stops_before_any_cloud_rename(self):
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,_,service=self.make(folder,[
                dict(id='1',parent_id='0',name='IPX-922-A.mp4',pickcode='a'*17),
                dict(id='2',parent_id='0',name='IPX-922-B.mp4',pickcode='b'*17),
                dict(id='3',parent_id='0',name='IPX-922.zh.ass',pickcode='c'*17),
            ])
            self.assertEqual(service.run(['0'])['fail'],1)
            self.assertEqual(cloud.renames,[])
            self.assertIn('字幕',service._error.call_args.args[0])

    def test_movie_root_does_not_create_nested_classification(self):
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,_,service=self.make(folder,[
                dict(id='7',parent_id='0',name='jav',is_dir=True),
                dict(id='8',parent_id='7',name='IPX',is_dir=True),
                dict(id='9',parent_id='8',name='IPX-922',is_dir=True),
                dict(id='1',parent_id='9',name='IPX-922.mp4',pickcode='a'*17),
            ],archive=True)
            self.assertEqual(service.run(['9'])['ok'],1)
            self.assertEqual(cloud.directories,[])
            self.assertEqual(cloud.entries['1'].parent_id,'9')

    def test_new_movie_directory_moves_intact_to_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,_,service=self.make(folder,[
                dict(id='7',parent_id='0',name='jav',is_dir=True),
                dict(id='8',parent_id='7',name='www.98T.la@IPX-922',is_dir=True),
                dict(id='1',parent_id='8',name='IPX-922.mp4',pickcode='a'*17),
                dict(id='2',parent_id='8',name='notes.txt'),
            ],archive=True)
            self.assertEqual(service.run(['7'])['ok'],1)
            prefix=next(entry for entry in cloud.entries.values() if entry.is_dir and entry.name=='IPX')
            self.assertEqual(cloud.entries['8'].parent_id,prefix.id)
            self.assertEqual(cloud.entries['8'].name,'IPX-922')
            self.assertEqual(cloud.entries['2'].parent_id,'8')

    def test_partial_rename_and_archive_retry_keep_version_and_url(self):
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,_,service=self.make(folder,[
                dict(id='7',parent_id='0',name='jav',is_dir=True),
                dict(id='1',parent_id='7',name='IPX-922-A.mp4',pickcode='a'*17),
                dict(id='2',parent_id='7',name='IPX-922-B.mkv',pickcode='b'*17),
            ],archive=True)
            cloud.raise_after_rename='1'
            self.assertEqual(service.run(['7'])['fail'],1)
            self.assertEqual(service.run(['7'])['ok'],1)
            path=service.root/'IPX/IPX-922'
            original={p.name:p.read_bytes() for p in path.glob('*.strm')}
            self.assertEqual(service.run(['7'])['skip'],1)
            self.assertEqual(original,{p.name:p.read_bytes() for p in path.glob('*.strm')})



    def test_late_original_name_subtitle_uses_persisted_source_name(self):
        with tempfile.TemporaryDirectory() as folder:
            m,cloud,_,service=self.make(folder,[dict(id='1',parent_id='0',name='www.98T.la@IPX-922-U.mp4',pickcode='a'*17)])
            self.assertEqual(service.run(['0'])['ok'],1)
            cloud.entries['2']=m.Entry('2','0','www.98T.la@IPX-922-U.zh.ass',pickcode='b'*17)
            self.assertEqual(service.run(['0'])['ok'],1)
            self.assertEqual((service.root/'IPX/IPX-922/IPX-922.zh.ass').read_bytes(),b'subtitle:2')

    def test_generic_subtitle_uses_single_video_in_its_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,_,service=self.make(folder,[
                dict(id='7',parent_id='0',name='jav',is_dir=True),
                dict(id='8',parent_id='7',name='IPX-922-B',is_dir=True),
                dict(id='1',parent_id='7',name='IPX-922-A.mp4',pickcode='a'*17),
                dict(id='2',parent_id='8',name='IPX-922-B.mp4',pickcode='b'*17),
                dict(id='3',parent_id='8',name='zh.ass',pickcode='c'*17),
            ])
            self.assertEqual(service.run(['7'])['ok'],1)
            self.assertTrue((service.root/'IPX/IPX-922/IPX-922 - V0001.zh.ass').exists())
            self.assertFalse((service.root/'IPX/IPX-922/IPX-922.zh.ass').exists())

    def test_simultaneous_registration_keeps_one_version_per_file(self):
        import concurrent.futures
        with tempfile.TemporaryDirectory() as folder:
            m,cloud,_,service=self.make(folder,[dict(id='1',parent_id='0',name='IPX-922.mp4',pickcode='a'*17)])
            other=m.LibraryRegistry(Path(folder)/'state',service.root)
            try:
                group=[[cloud.entries['1']]]
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    results=list(pool.map(lambda registry:registry.assign_versions(cloud.account,service.root,'IPX-922',group),[service.registry,other]))
                self.assertEqual(results,[{'1':1},{'1':1}])
                self.assertEqual(len(service.registry.versions(cloud.account,service.root,'IPX-922')),1)
            finally:
                other.close()

    def test_cross_account_output_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,_,service=self.make(folder,[dict(id='1',parent_id='0',name='IPX-922.mp4',pickcode='a'*17)])
            path=service.root/'IPX/IPX-922/movie.nfo';path.parent.mkdir(parents=True)
            service.registry.reserve(path,'other-account','old-job',b'private')
            path.write_bytes(b'private');service.registry.published(path,b'private')
            self.assertEqual(service.run(['0'])['fail'],1)
            self.assertEqual(path.read_bytes(),b'private')
            self.assertEqual(cloud.renames,[])


    def test_uppercase_directories_do_not_bypass_depth(self):
        with tempfile.TemporaryDirectory() as folder:
            entries=[dict(id=str(index),parent_id=str(index-1),name=name,is_dir=True)
                     for index,name in enumerate(('AAA','BBB','CCC','DDD','EEE'),1)]
            entries.append(dict(id='6',parent_id='5',name='IPX-922.mp4',pickcode='a'*17))
            _,cloud,metadata,service=self.make(folder,entries)
            self.assertEqual(service.run(['0'])['total'],0)
            self.assertEqual(cloud.renames,[])
            metadata.assert_not_called()


    def test_partial_move_resumes_subtitle_left_in_source(self):
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,_,service=self.make(folder,[
                dict(id='7',parent_id='0',name='jav',is_dir=True),
                dict(id='1',parent_id='7',name='IPX-922.mp4',pickcode='a'*17),
                dict(id='2',parent_id='7',name='IPX-922.zh.ass',pickcode='b'*17),
            ],archive=True)
            cloud.interrupt_move_after='1'
            self.assertEqual(service.run(['7'])['fail'],1)
            self.assertNotEqual(cloud.entries['1'].parent_id,cloud.entries['2'].parent_id)
            self.assertEqual(service.run(['7'])['ok'],1)
            self.assertEqual(cloud.entries['1'].parent_id,cloud.entries['2'].parent_id)
            self.assertEqual(service.run(['7'])['skip'],1)
            files=service.registry.versions(cloud.account,service.root,'IPX-922')[0]['files']
            self.assertEqual({item['entry']['id'] for item in files},{'1','2'})


    def test_empty_outputs_ignore_stale_jobs_and_flat_output_rows(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,metadata,service=self.make(folder,[
                dict(id='2',parent_id='0',name='IPX-922-U.mp4',pickcode='a'*17),
                dict(id='3',parent_id='0',name='IPX-922-U.zh.ass',pickcode='b'*17),
            ])
            service.registry.db.execute('CREATE TABLE jobs(account TEXT,job TEXT,plan TEXT,complete INTEGER)')
            old=dict(number='IPX-922',metadata={'title':'stale'},files=[dict(entry=dict(id='1',parent_id='8',name='IPX-922.mp4',pickcode='z'*17),target='IPX-922.mp4',kind='video')])
            service.registry.db.execute('INSERT INTO jobs VALUES(?,?,?,1)',(cloud.account,'8:IPX-922',json.dumps(old)))
            service.registry.db.execute('INSERT INTO outputs VALUES(?,?,?,?,?)',(str(service.root/'IPX-922/IPX-922.strm'),cloud.account,'8:IPX-922','old-digest',None))
            service.registry.db.commit()
            # Empty shells can remain after the user clears previous outputs.
            (service.root/'IPX-922').mkdir()
            cloud.info=Mock(wraps=cloud.info)
            self.assertEqual(service.run(['0'])['ok'],1)
            output=service.root/'IPX/IPX-922'
            self.assertEqual({path.name for path in output.iterdir()},
                {'IPX-922.strm','IPX-922.zh.ass','movie.nfo','poster.jpg','fanart.jpg'})
            self.assertEqual(metadata.call_count,1)
            self.assertFalse(any(call.args==('1',) for call in cloud.info.call_args_list))
            self.assertEqual(service.run(['0'])['skip'],1)
            self.assertEqual(service.registry.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)

    def test_completed_missing_version_is_not_resurrected(self):
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            m,cloud,_,service=self.make(folder,[dict(id='1',parent_id='0',name='IPX-922.mp4',pickcode='a'*17)])
            self.assertEqual(service.run(['0'])['ok'],1)
            token=(service.root/'IPX/IPX-922/IPX-922.strm').read_text().strip().split('media_id=')[1]
            shutil.rmtree(service.root/'IPX/IPX-922')
            del cloud.entries['1']
            cloud.entries['2']=m.Entry('2','0','IPX-922-U.mp4',pickcode='b'*17)
            cloud.info=Mock(wraps=cloud.info)
            self.assertEqual(service.run(['0'])['ok'],1)
            self.assertFalse((service.root/'IPX/IPX-922/IPX-922.strm').exists())
            self.assertTrue((service.root/'IPX/IPX-922/IPX-922 - V0001.strm').exists())
            self.assertEqual(service.registry.media(cloud.account,token).id,'1')
            self.assertEqual(cloud.info.call_args_list.count(unittest.mock.call('1')), 1)


    def test_archive_toggle_keeps_tracked_subtitle_after_partial_move(self):
        with tempfile.TemporaryDirectory() as folder:
            _,cloud,_,service=self.make(folder,[
                dict(id='7',parent_id='0',name='jav',is_dir=True),
                dict(id='1',parent_id='7',name='IPX-922.mp4',pickcode='a'*17),
                dict(id='2',parent_id='7',name='IPX-922.zh.ass',pickcode='b'*17),
            ],archive=True)
            cloud.interrupt_move_after='1'
            self.assertEqual(service.run(['7'])['fail'],1)
            service.archive_loose=False
            self.assertEqual(service.run(['7'])['skip'],1)
            self.assertNotEqual(cloud.entries['1'].parent_id,cloud.entries['2'].parent_id)
            service.archive_loose=True
            self.assertEqual(service.run(['7'])['ok'],1)
            self.assertEqual(cloud.entries['1'].parent_id,cloud.entries['2'].parent_id)
            files=service.registry.versions(cloud.account,service.root,'IPX-922')[0]['files']
            self.assertEqual({item['entry']['id'] for item in files},{'1','2'})



if __name__=='__main__':
    unittest.main()
