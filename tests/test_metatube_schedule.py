"""Exercise the host service contract against real APScheduler 3 triggers."""
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from test_metatube_auth import load_plugin


class ScheduleTests(unittest.TestCase):
    def plugin(self, version="v3", **schedule):
        plugin = load_plugin(version, Mock())
        plugin.init_plugin(dict(enabled=True, storage="local", metatube_url="http://backend",
                                scan_dirs="/movies", **schedule))
        return plugin

    def trigger(self, plugin):
        service, = plugin.get_service()
        scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        job = scheduler.add_job(service["func"], service["trigger"], **service["kwargs"])
        self.assertEqual(str(job.trigger.timezone), "Asia/Shanghai")
        return job.trigger

    def test_old_daily_schedule_preserves_zero_hour_and_minute(self):
        for version in ("v2", "v3"):
            for hour, minute in ((0, 0), (4, 0), (0, 30), (23, 59)):
                with self.subTest(version=version, hour=hour, minute=minute):
                    trigger = self.trigger(self.plugin(version, cron_hour=hour, cron_minute=minute))
                    after = datetime(2026, 10, 29, tzinfo=ZoneInfo("Asia/Shanghai"))
                    fire = trigger.get_next_fire_time(None, after)
                    self.assertEqual((fire.hour, fire.minute, fire.second), (hour, minute, 0))

    def test_manual_mode_keeps_manual_api_available_without_timer(self):
        for version in ("v2", "v3"):
            plugin = self.plugin(version, schedule_mode="manual", cron_expression="bad")
            self.assertEqual(plugin.get_service(), [])
            self.assertTrue(plugin.get_state())
            with patch("threading.Thread") as thread:
                self.assertEqual(plugin._MetaTubeScraper__api_run()["code"], 0)
                thread.return_value.start.assert_called_once()

    def test_weekly_and_custom_cron_use_host_timezone(self):
        for version in ("v2", "v3"):
            cases = [
                dict(schedule_mode="weekly", schedule_weekday={"value": "mon"}, cron_hour=4, cron_minute=30),
                dict(schedule_mode={"value": "cron"}, cron_expression="  30   4 * * mon  "),
            ]
            for config in cases:
                with self.subTest(version=version, config=config):
                    trigger = self.trigger(self.plugin(version, **config))
                    fire = trigger.get_next_fire_time(None, datetime(2026, 10, 29, 23, tzinfo=ZoneInfo("Asia/Shanghai")))
                    self.assertEqual(fire.isoformat(), "2026-11-02T04:30:00+08:00")

    def test_interval_is_elapsed_minutes_and_ignores_inactive_daily_fields(self):
        for version in ("v2", "v3"):
            trigger = self.trigger(self.plugin(version, schedule_mode="interval", schedule_interval="90",
                                               cron_hour="bad", cron_minute="bad", cron_expression="bad"))
            self.assertEqual(trigger.interval.total_seconds(), 5400)

    def test_custom_cron_steps_and_numeric_weekdays_follow_apscheduler(self):
        plugin = self.plugin(schedule_mode="cron", cron_expression="*/15 9-17 * * 0-4")
        trigger = self.trigger(plugin)
        fire = trigger.get_next_fire_time(None, datetime(2026, 10, 30, 10, 7, tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertEqual(fire.isoformat(), "2026-10-30T10:15:00+08:00")

    def test_invalid_active_fields_disable_timer_and_show_error_without_blocking_manual(self):
        cases = [
            dict(schedule_mode="unknown"),
            dict(schedule_mode="daily", cron_hour="bad"),
            dict(schedule_mode="daily", cron_hour=24),
            dict(schedule_mode="daily", cron_minute=-1),
            dict(schedule_mode="weekly", schedule_weekday="noday"),
            dict(schedule_mode="interval", schedule_interval=0),
            dict(schedule_mode="interval", schedule_interval=1.5),
            dict(schedule_mode="interval", schedule_interval="nan"),
            dict(schedule_mode="cron", cron_expression=""),
            dict(schedule_mode="cron", cron_expression="0 0 * * * *"),
            dict(schedule_mode="cron", cron_expression="60 4 * * *"),
            dict(schedule_mode="cron", cron_expression="0 4 * * 7"),
            dict(schedule_mode="cron", cron_expression="0 4 * * nope"),
        ]
        for version in ("v2", "v3"):
            for config in cases:
                with self.subTest(version=version, config=config):
                    plugin = self.plugin(version, **config)
                    self.assertEqual(plugin.get_service(), [])
                    plugin.get_data = Mock(return_value=None)
                    self.assertIn("调度", str(plugin.get_page()))
                    self.assertTrue(plugin.get_state())
                    with patch("threading.Thread") as thread:
                        self.assertEqual(plugin._MetaTubeScraper__api_run()["code"], 0)
                        thread.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
