"""Mode-specific UI and safety checks, with the real form expressions executed."""
import json
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_metatube_auth import load_plugin, response

NODE = shutil.which('node')
HERE = Path(__file__).parent


@unittest.skipUnless(NODE, 'Node.js is required for form expression tests')
class FormTests(unittest.TestCase):
    def render(self, version='v3', **values):
        form, model = load_plugin(version, Mock()).get_form()
        changes = values.pop('changes', [])
        async_browse = values.pop('async_browse', None)
        cookie_check = values.pop('cookie_check', None)
        directory_flow = values.pop('directory_flow', None)
        directory_failure = values.pop('directory_failure', None)
        picker_flow = values.pop('picker_flow', None)
        model.update(values)
        reply = subprocess.run([NODE, str(HERE / 'form_model.js')],
                               input=json.dumps(dict(form=form, model=model, changes=changes, asyncBrowse=async_browse, cookieCheck=cookie_check, directoryFlow=directory_flow, directoryFailure=directory_failure, pickerFlow=picker_flow)),
                               capture_output=True, text=True, check=True)
        return json.loads(reply.stdout)

    def test_direct_directory_widgets_show_names_but_keep_ids_for_scanning(self):
        for version in ('v2','v3'):
            result=self.render(version,directory_flow={'edit':True})
            self.assertEqual(result['intermediate']['display'],'/电影')
            self.assertEqual(result['intermediate']['ids'],'id:7')
            self.assertEqual(result['snapshots'][1]['scan_dirs']['modelValue'],[{'title':'/电影','value':'id:7'}])
            self.assertEqual(result['model']['browse_display_path'],'/新电影')
            self.assertEqual(result['model']['browse_path'],'/新电影')
            self.assertEqual(result['model']['scan_dirs'],'id:7\n/新电影')
            self.assertEqual(result['model']['dir_labels']['id:7'],'/电影')
            self.assertNotIn('browse_path',result['snapshots'][0])
            self.assertNotIn('browse_display_path',result['snapshots'][0])

    def test_old_id_lists_can_render_chinese_chips_and_legacy_paths_stay_editable(self):
        direct=self.render(scan_dirs=['id:7','/jav'],dir_labels={'id:7':'/电影'})['snapshots'][0]
        self.assertEqual(direct['scan_dirs']['modelValue'],[{'title':'/电影','value':'id:7'},{'title':'/jav','value':'/jav'}])
        legacy=self.render(storage='115Lite',scan_dirs='/电影')['snapshots'][0]
        self.assertNotIn('browse_path',legacy)
        self.assertNotIn('browse_display_path',legacy)
        self.assertNotIn('modelValue',legacy['scan_dirs'])

    def test_identical_directory_names_keep_distinct_ids_after_edit_and_reload(self):
        labels={'id:7':'/电影','id:8':'/电影'}
        values=[{'title':'/电影','value':'id:7'},{'title':'/电影','value':'id:8'}]
        result=self.render(dir_labels=labels,changes=[{'field':'scan_dirs','value':values}])
        self.assertEqual(result['model']['scan_dirs'],'id:7\nid:8')
        reloaded=self.render(scan_dirs=result['model']['scan_dirs'],dir_labels=labels)
        self.assertEqual(reloaded['snapshots'][0]['scan_dirs']['modelValue'],values)

    def test_failed_navigation_never_adds_an_id_different_from_the_visible_directory(self):
        for version in ('v2','v3'):
            for action in ('up','enter'):
                result=self.render(version,browse_path='id:7',browse_display_path='/电影/中文合集',
                                   dir_parent='id:6',dir_labels={'id:7':'/电影/中文合集'},
                                   directory_failure={'action':action})
                self.assertEqual(result['model']['browse_path'],'id:7')
                self.assertEqual(result['model']['browse_display_path'],'/电影/中文合集')
                self.assertEqual(result['model']['dir_parent'],'id:6')
                self.assertEqual(result['model']['scan_dirs'],'')
                self.assertIn('读取',result['model']['dir_error'])

    def test_unresolved_old_id_does_not_display_root_or_allow_adding_a_false_label(self):
        for version in ('v2','v3'):
            result=self.render(version,browse_path='id:7',browse_display_path='/',dir_picker_open=True,
                               directory_failure={'action':'addOnly'})
            self.assertEqual(result['snapshots'][0]['browse_display_path'].get('modelValue'),'')
            self.assertEqual(result['model']['scan_dirs'],'')
            self.assertIn('读取',result['model']['dir_error'])

    def test_manually_typed_directory_ids_remain_visible_and_plain_paths_have_correct_labels(self):
        for value in ('id:','id:99','id:7','id:78','/新电影'):
            result=self.render(dir_picker_open=True,dir_labels={'id:7':'/电影'},changes=[{'field':'browse_display_path','value':value}])
            self.assertEqual(result['snapshots'][-1]['browse_display_path']['modelValue'],value)

    def test_picker_opens_and_reads_automatically_but_only_confirm_changes_scan_roots(self):
        for version in ('v2','v3'):
            result=self.render(version,scan_dirs='id:3',picker_flow={'confirm':True})
            self.assertTrue(result['intermediate']['loading'])
            self.assertEqual(result['intermediate']['unchanged'],'id:3')
            self.assertNotIn('browse_display_path',result['snapshots'][0])
            self.assertIn('browse_display_path',result['snapshots'][1])
            self.assertEqual(result['model']['scan_dirs'],'id:3\nid:7')
            self.assertFalse(result['model']['dir_picker_open'])
            self.assertEqual(result['snapshots'][-1]['scan_dirs']['modelValue'],[{'title':'id:3','value':'id:3'},{'title':'/电影','value':'id:7'}])

    def test_picker_cancel_and_late_results_preserve_existing_roots(self):
        for case in ({'cancel':True},{'cancelPending':True},{'cancelPending':True,'dismiss':True}):
            result=self.render(scan_dirs='/已有目录',picker_flow=case)
            self.assertEqual(result['model']['scan_dirs'],'/已有目录')
            self.assertFalse(result['model']['dir_picker_open'])
            self.assertFalse(result['model']['dir_ready'])
            self.assertEqual(result['model']['dir_items'],[])

    def test_picker_can_select_the_same_empty_directory_without_duplicate_roots(self):
        result=self.render(scan_dirs='id:7',picker_flow={'confirm':True})
        self.assertEqual(result['model']['scan_dirs'],'id:7')
        self.assertEqual(result['model']['dir_labels']['id:7'],'/电影')

    def test_legacy_picker_adds_paths_and_keeps_the_browser_out_of_the_main_form(self):
        for version in ('v2','v3'):
            for storage in ('local','115Lite','u115',{'value':'alist'}):
                result=self.render(version,storage=storage,browse_path='/电影',
                                   scan_dirs='/已有目录',picker_flow={'confirm':True})
                self.assertNotIn('browse_path',result['snapshots'][0])
                self.assertIn('browse_path',result['snapshots'][1])
                self.assertEqual(result['model']['scan_dirs'],'/已有目录\n/电影')
                self.assertFalse(result['model']['dir_picker_open'])

    def test_cookie_change_closes_picker_and_discards_pending_directory_results(self):
        for action in ('load','enter','up'):
            result=self.render(dir_picker_open=True,scan_dirs='id:3',
                               async_browse={'action':action,'editCookie':True})
            self.assertFalse(result['model']['dir_picker_open'])
            self.assertFalse(result['model']['dir_ready'])
            self.assertEqual(result['model']['dir_items'],[])
            self.assertEqual(result['model']['scan_dirs'],'id:3')
            self.assertEqual(result['model']['dir_labels'],{})

    def test_cookie_check_has_its_own_nearby_result_and_loading_state(self):
        for version in ('v2','v3'):
            for reject in (False,True):
                result=self.render(version,run_msg='existing scrape result',cookie_check={'reject':reject})
                self.assertEqual(result['model']['run_msg'],'existing scrape result')
                self.assertEqual(result['intermediate'],{'loading':True,'requests':1})
                self.assertFalse(result['model']['cookie115_checking'])
                self.assertIn('cookie115_msg',result['snapshots'][-1])
                self.assertIn('失败' if reject else 'Cookie 可用',result['model']['cookie115_msg'])

    def test_cookie_check_discards_a_result_after_mode_or_cookie_change(self):
        for change in ('switchMode','editCookie'):
            result=self.render(cookie_check={change:True})
            self.assertEqual(result['model']['cookie115_msg'],'')
            self.assertFalse(result['model']['cookie115_checking'])

    def test_direct_hides_legacy_actions_in_both_combobox_shapes(self):
        hidden = {'write_local', 'write_remote', 'remote_lib_root', 'organize', 'trigger_strm',
                  'file_interval', 'rename_files', 'download_previews', 'preview_limit'}
        for version in ('v2', 'v3'):
            for storage in ('115_direct', {'value': '115_direct', 'title': '115'}):
                with self.subTest(version=version, storage=storage):
                    fields = self.render(version, storage=storage, download_previews=True)['snapshots'][0]
                    self.assertFalse(hidden & fields.keys(), hidden & fields.keys())
                    self.assertTrue({'cookie115', 'playback_base_url', 'cloud_interval', 'local_strm_root'} <= fields.keys())
                    self.assertIn('补齐', fields['force_overwrite']['hint'])

    def test_schedule_modes_show_only_their_fields_and_retain_other_values(self):
        all_fields = {'cron_hour', 'cron_minute', 'schedule_weekday', 'schedule_interval', 'cron_expression'}
        visible = {
            'manual': set(), 'daily': {'cron_hour', 'cron_minute'},
            'weekly': {'cron_hour', 'cron_minute', 'schedule_weekday'},
            'interval': {'schedule_interval'}, 'cron': {'cron_expression'},
        }
        for version in ('v2', 'v3'):
            for mode, expected in visible.items():
                for shape in (mode, {'value': mode}):
                    with self.subTest(version=version, mode=shape):
                        fields = self.render(version, schedule_mode=shape)['snapshots'][0]
                        self.assertIn('schedule_mode', fields)
                        self.assertEqual(all_fields & fields.keys(), expected)
            result = self.render(version, cron_hour=0, cron_minute=0, schedule_weekday='sun',
                                 schedule_interval=90, cron_expression='*/15 * * * *',
                                 changes=[{'field': 'schedule_mode', 'value': 'cron'},
                                          {'field': 'schedule_mode', 'value': {'value': 'daily'}}])
            self.assertEqual(result['model']['cron_hour'], 0)
            self.assertEqual(result['model']['cron_minute'], 0)
            self.assertEqual(result['model']['schedule_weekday'], 'sun')
            self.assertEqual(result['model']['schedule_interval'], 90)
            self.assertEqual(result['model']['cron_expression'], '*/15 * * * *')

    def test_local_and_remote_mode_visibility_and_output_dependencies(self):
        for version in ('v2', 'v3'):
            with self.subTest(version=version):
                local = self.render(version, storage='local')['snapshots'][0]
                self.assertFalse({'cookie115', 'playback_base_url', 'cloud_interval', 'write_local',
                                  'write_remote', 'remote_lib_root', 'local_strm_root', 'organize', 'trigger_strm'} & local.keys())
                self.assertNotIn('preview_limit', local)
                self.assertIn('preview_limit', self.render(version, storage='local', download_previews=True)['snapshots'][0])
                lite = self.render(version, storage='115Lite', organize=True, write_remote=True)['snapshots'][0]
                self.assertFalse({'write_remote', 'organize', 'cookie115'} & lite.keys())
                self.assertTrue({'write_local', 'trigger_strm', 'remote_lib_root', 'local_strm_root', 'rename_files'} <= lite.keys())
                cloud = self.render(version, storage='u115', write_local=False, write_remote=True, organize=False)['snapshots'][0]
                self.assertFalse({'remote_lib_root', 'local_strm_root'} & cloud.keys())
                cloud = self.render(version, storage='alist', write_local=False, write_remote=True, organize=True)['snapshots'][0]
                self.assertIn('remote_lib_root', cloud)
                self.assertNotIn('local_strm_root', cloud)

    def test_storage_switch_resets_browser_but_keeps_credentials_and_saved_options(self):
        for version in ('v2', 'v3'):
            result = self.render(version, storage='115_direct', cookie115='fixture-cookie',
                                 scan_dirs='id:7', browse_path='id:7', dir_items=[{'value': 'id:8'}],
                                 pick_dir='id:8', trigger_strm=True,
                                 changes=[{'field': 'storage', 'value': 'local'},
                                          {'field': 'storage', 'value': {'value': '115_direct'}}])
            self.assertEqual(result['model']['storage'], '115_direct')
            self.assertEqual(result['model']['cookie115'], 'fixture-cookie')
            self.assertEqual(result['model']['scan_dirs'], 'id:7')
            self.assertTrue(result['model']['trigger_strm'])
            self.assertEqual(result['model']['browse_path'], '/')
            self.assertEqual(result['model']['dir_items'], [])
            self.assertIsNone(result['model']['pick_dir'])
            self.assertNotIn('cookie115', result['snapshots'][1])
            self.assertIn('cookie115', result['snapshots'][2])

    def test_old_browse_results_and_errors_cannot_restore_another_storage(self):
        for version in ('v2', 'v3'):
            for action in ('load', 'enter', 'up'):
                for away_back, reject in ((False, False), (True, False), (True, True)):
                    with self.subTest(version=version, action=action, away_back=away_back, reject=reject):
                        result = self.render(version, async_browse=dict(action=action, awayBack=away_back, reject=reject))
                        self.assertEqual(result['model']['browse_path'], '/')
                        self.assertEqual(result['model']['dir_items'], [])
                        self.assertFalse(result['model'].get('dir_loading', False))
                        self.assertNotIn('stale', result['model']['dir_error'])

    def test_older_response_does_not_clear_loading_or_replace_a_newer_browse(self):
        for action in ('load', 'enter', 'up'):
            result = self.render(async_browse=dict(action=action, overlap=True))
            self.assertTrue(result['intermediate'].get('loading'))
            self.assertEqual(result['intermediate']['names'], [])
            self.assertEqual(result['model']['browse_path'], 'id:88')
            self.assertEqual([item['title'] for item in result['model']['dir_items']], ['fresh'])


class ConfigurationGuardTests(unittest.TestCase):
    def test_invalid_output_is_rejected_before_starting_a_task(self):
        for version in ('v2', 'v3'):
            for storage in ('115Lite', 'u115', 'alist'):
                plugin = load_plugin(version, Mock())
                plugin.init_plugin(dict(enabled=True, storage=storage, metatube_url='http://backend',
                                        scan_dirs='/source', write_local=False,
                                        write_remote=storage == '115Lite', organize=True, remote_lib_root='/jav'))
                thread = Mock()
                with patch.dict(plugin._MetaTubeScraper__api_run.__func__.__globals__,
                                threading=types.SimpleNamespace(Thread=thread)):
                    result = plugin._MetaTubeScraper__api_run()
                self.assertEqual(result['code'], 400)
                self.assertIn('输出', result['msg'])
                thread.assert_not_called()
                self.assertFalse(plugin.get_service())

    def test_missing_mapping_and_direct_credentials_are_named(self):
        cases = (
            ({'storage': 'u115', 'write_local': True}, '本地'),
            ({'storage': 'u115', 'write_local': True, 'local_strm_root': '/tmp/local'}, '网盘'),
            ({'storage': 'u115', 'write_local': False, 'write_remote': True, 'organize': True}, '网盘'),
            ({'storage': '115_direct'}, 'Cookie'),
        )
        for settings, missing in cases:
            plugin = load_plugin('v3', Mock())
            plugin.init_plugin(dict(enabled=True, metatube_url='http://backend', scan_dirs='/jav', **settings))
            with patch.dict(plugin._MetaTubeScraper__api_run.__func__.__globals__,
                            threading=types.SimpleNamespace(Thread=Mock())):
                result = plugin._MetaTubeScraper__api_run()
            self.assertEqual(result['code'], 400)
            self.assertIn(missing, result['msg'])

    def test_115lite_ignores_hidden_upload_and_move_flags_without_erasing_saved_values(self):
        for version in ('v2', 'v3'):
            with tempfile.TemporaryDirectory() as directory:
                def get(url, **kwargs):
                    if '/images/' in url:
                        return types.SimpleNamespace(status_code=200, headers={'content-type': 'image/jpeg'}, content=b'image', close=lambda: None)
                    return response(200, {'data': [{'id': 'film'}]} if url.endswith('/search') else {'data': {'id': 'film', 'provider': 'JAV321', 'title': 'Fixture'}})
                plugin = load_plugin(version, get)
                config = dict(enabled=True, storage='115Lite', metatube_url='http://backend',
                              scan_dirs='/jav', write_local=True, write_remote=True, organize=True,
                              remote_lib_root='/jav', local_strm_root=directory, trigger_strm=False)
                plugin.init_plugin(config)
                plugin.list_children = lambda path: [{'name': 'DSOD-060.mp4', 'type': 'file'}]
                plugin._MetaTubeScraper__throttle = lambda: None
                plugin._MetaTubeScraper__upload_bytes = Mock(side_effect=AssertionError('upload forbidden'))
                plugin._MetaTubeScraper__organize_move = Mock(side_effect=AssertionError('move forbidden'))
                self.assertEqual(plugin._MetaTubeScraper__process_target('/jav/DSOD-060'), 'ok')
                self.assertTrue((Path(directory) / 'DSOD-060/movie.nfo').is_file())
                self.assertEqual(plugin._MetaTubeScraper__process_target('/source/DSOD-060'), 'skip')
                plugin._MetaTubeScraper__upload_bytes.assert_not_called()
                plugin._MetaTubeScraper__organize_move.assert_not_called()
                self.assertTrue(config['write_remote'])
                self.assertTrue(config['organize'])


if __name__ == '__main__':
    unittest.main()
