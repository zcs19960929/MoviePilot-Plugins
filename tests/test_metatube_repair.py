"""Safe legacy capability recovery: only exact, verified cloud files."""
import importlib
import dataclasses
import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_metatube_direct import components, FakeCloud


def repair_module(version='v3'):
    import importlib.util
    path = Path(__file__).resolve().parents[1]/f'plugins.{version}/metatubescraper'
    name = 'repair_acceptance_'+version
    package=types.ModuleType(name);package.__path__=[str(path)]
    with patch.dict(sys.modules,{name:package}):return importlib.import_module(name+'.repair')


class RecoveryTests(unittest.TestCase):
    def make(self,tmp,version='v3',multipart=False):
        path=Path(__file__).resolve().parents[1]/f'plugins.{version}/metatubescraper/repair.py'
        self.assertTrue(path.exists(),'read-only repair planner is missing')
        m=repair_module(version)
        root=Path(tmp)/'library';folder=root/'DSOD-060';folder.mkdir(parents=True)
        stem='DSOD-060-CD1' if multipart else 'DSOD-060'
        file=folder/(stem+'.strm')
        file.write_text('https://mp.example/api/v1/plugin/MetaTubeScraper/redirect_url?media_id='+'a'*64+'\n')
        (folder/'movie.nfo').write_text('<movie><num>DSOD-060</num></movie>')
        state=Path(tmp)/'state';r=m.Registry(state);r.close()
        cloud=FakeCloud(m,[m.Entry('10','0','folder',True),m.Entry('7','10',stem+'.mp4',pickcode='p')])
        recovery=m.Recovery(cloud,state,root,'https://mp.example',['0'])
        return m,cloud,recovery,file

    def test_preview_and_apply_preserve_files_and_canonical_ids_both_versions(self):
        for version in ('v2','v3'):
            with self.subTest(version=version),tempfile.TemporaryDirectory() as tmp:
                m,cloud,recovery,file=self.make(tmp,version)
                r=m.Registry(recovery.data_dir);canonical=r.media_token(cloud.account,cloud.entries['7']);r.close()
                before={p:p.read_bytes() for p in file.parent.iterdir()}
                db=(recovery.data_dir/'direct.sqlite3');db_before=db.read_bytes()
                preview=recovery.preview()
                self.assertEqual(len(preview['ready']),1);self.assertEqual(db.read_bytes(),db_before)
                self.assertEqual(recovery.apply(preview)['restored'],1)
                r=m.Registry(recovery.data_dir,readonly=True)
                self.assertEqual(r.media(cloud.account,'a'*64).id,'7')
                self.assertEqual(r.media(cloud.account,canonical).id,'7');r.close()
                again=recovery.preview();self.assertEqual(again['already'],1)
                self.assertEqual(recovery.apply(again)['restored'],0)
                self.assertEqual({p:p.read_bytes() for p in file.parent.iterdir()},before)
                self.assertEqual(cloud.renames,[]);self.assertEqual(cloud.downloads,[]);self.assertEqual(cloud.links,[])

    def test_multiple_versions_and_wrong_parts_are_not_guessed(self):
        for multipart in (False,True):
            with self.subTest(multipart=multipart),tempfile.TemporaryDirectory() as tmp:
                m,cloud,recovery,file=self.make(tmp,multipart=multipart)
                if multipart:cloud.entries['7']=dataclasses.replace(cloud.entries['7'],name='DSOD-060-CD2.mp4')
                else:cloud.entries['8']=dataclasses.replace(cloud.entries['7'],id='8',name='DSOD-060.mkv')
                result=recovery.preview();self.assertEqual(result['ready'],[]);self.assertEqual(len(result['skipped']),1)
                self.assertEqual(recovery.apply(result)['restored'],0)

    def test_part_can_be_recovered_and_other_directories_are_out_of_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            m,cloud,recovery,file=self.make(tmp,multipart=True)
            cloud.entries['8']=m.Entry('8','999','DSOD-060-CD1.mp4',pickcode='other')
            self.assertEqual(recovery.apply(recovery.preview())['restored'],1)

    def test_changed_strm_and_changed_cloud_entry_abort_before_writing(self):
        for change in ('local','cloud','account','database'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                m,cloud,recovery,file=self.make(tmp)
                plan=recovery.preview()
                if change=='local':file.write_text(file.read_text()+'edited')
                elif change=='cloud':cloud.entries['7']=dataclasses.replace(cloud.entries['7'],parent_id='other')
                elif change=='account':cloud.account='other'
                else:
                    r=m.Registry(recovery.data_dir);r.media_token('12345',cloud.entries['7']);r.close()
                result=recovery.apply(plan)
                self.assertEqual(result['restored'],0);self.assertEqual(len(result['skipped']),1)
                r=m.Registry(recovery.data_dir,readonly=True)
                with self.assertRaises(KeyError):r.media('12345','a'*64)
                r.close()

    def test_symlink_foreign_urls_and_nfo_disagreement_are_skipped(self):
        for change in ('symlink','url','nfo'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                m,cloud,recovery,file=self.make(tmp)
                if change=='symlink':
                    data=file.read_text();file.unlink();target=Path(tmp)/'outside.strm';target.write_text(data);file.symlink_to(target)
                elif change=='url':file.write_text(file.read_text().replace('mp.example','other.example'))
                else:(file.parent/'movie.nfo').write_text('<movie><num>OTHER-001</num></movie>')
                plan=recovery.preview();self.assertEqual(plan['ready'],[]);self.assertEqual(len(plan['skipped']),1)

    def test_missing_database_is_not_created_by_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            m,cloud,recovery,file=self.make(tmp)
            (recovery.data_dir/'direct.sqlite3').unlink();recovery.data_dir.rmdir()
            with self.assertRaisesRegex(m.CloudError,'登记数据库'):recovery.preview()
            self.assertFalse(recovery.data_dir.exists())

    def test_apply_rechecks_new_duplicate_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            m,cloud,recovery,file=self.make(tmp)
            plan=recovery.preview()
            cloud.entries['8']=dataclasses.replace(cloud.entries['7'],id='8',name='DSOD-060.mkv')
            result=recovery.apply(plan)
            self.assertEqual(result['restored'],0)
            self.assertEqual(len(result['skipped']),1)

    def test_apply_never_creates_or_touches_a_replaced_database(self):
        import sqlite3,os
        for change in ('delete','replace'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                m,cloud,recovery,file=self.make(tmp);plan=recovery.preview()
                db=recovery.data_dir/'direct.sqlite3'
                if change=='delete':db.unlink()
                else:
                    replacement=Path(tmp)/'replacement.db';sqlite3.connect(replacement).close();os.replace(replacement,db)
                original=db.read_bytes() if db.exists() else None
                with self.assertRaisesRegex(m.CloudError,'登记数据库'):
                    recovery.apply(plan)
                self.assertEqual(db.read_bytes() if db.exists() else None,original)


class RecoveryCLITests(unittest.TestCase):
    make = RecoveryTests.make
    def test_cli_defaults_to_read_only_then_backs_up_before_explicit_apply(self):
        import contextlib,io,json,importlib.util
        with tempfile.TemporaryDirectory() as tmp:
            m,cloud,recovery,file=self.make(tmp)
            source=Path(__file__).resolve().parents[1]/'scripts/repair_metatube_registry.py'
            spec=importlib.util.spec_from_file_location('repair_cli_test',source)
            cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
            self.assertTrue(hasattr(m,'Cloud115'),'repair runtime must export its independent client')
            config=Path(tmp)/'private.json';config.write_text(json.dumps(dict(storage='115_direct',enabled=False,
                cookie115='fixture-cookie-secret',scan_dirs=['0'],playback_base_url='https://mp.example')))
            backup=Path(tmp)/'backup'
            args=['--config-file',str(config),'--data-dir',str(recovery.data_dir),'--output-root',str(recovery.root),'--backup-dir',str(backup)]
            db=recovery.data_dir/'direct.sqlite3';original=db.read_bytes()
            output=io.StringIO()
            with patch.object(cli,'runtime',return_value=m),patch.object(m,'Cloud115',return_value=cloud),contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(args),0);self.assertEqual(db.read_bytes(),original);self.assertFalse(backup.exists())
                self.assertEqual(cli.main(args+['--apply']),0)
            self.assertNotIn('fixture-cookie-secret',output.getvalue());self.assertNotIn('a'*64,output.getvalue())
            import sqlite3
            saved=sqlite3.connect(backup/'registry.sqlite3')
            self.assertEqual(saved.execute('SELECT count(*) FROM media').fetchone()[0],0)
            self.assertEqual(saved.execute('SELECT count(*) FROM media_aliases').fetchone()[0],0)
            saved.close()
            self.assertEqual((backup/'strm/DSOD-060/DSOD-060.strm').read_bytes(),file.read_bytes())
            self.assertEqual((backup/'config.json').stat().st_mode & 0o777,0o600)
