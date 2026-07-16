import datetime
import unittest
from unittest import mock

import tikhub_proxy as proxy


class InternalSchedulerTests(unittest.TestCase):
    def setUp(self):
        with proxy.INTERNAL_SCHEDULER_STATE_LOCK:
            self.original_state = dict(proxy.INTERNAL_SCHEDULER_STATE)
            proxy.INTERNAL_SCHEDULER_STATE.update({
                "thread_started": False,
                "active_slot": "",
                "completed_slot": "",
                "running_slot": "",
                "attempts": 0,
                "last_triggered_at": "",
                "last_completed_at": "",
                "last_error": "",
                "next_retry_at": "",
                "next_retry_timestamp": 0.0,
                "needs_report_check": True,
            })

    def tearDown(self):
        with proxy.INTERNAL_SCHEDULER_STATE_LOCK:
            proxy.INTERNAL_SCHEDULER_STATE.clear()
            proxy.INTERNAL_SCHEDULER_STATE.update(self.original_state)

    def beijing(self, day, hour, minute=0, second=0):
        return datetime.datetime(2026, 7, day, hour, minute, second, tzinfo=proxy.BEIJING_TZ)

    def test_schedule_time_parser_accepts_two_beijing_slots(self):
        self.assertEqual(
            proxy._parse_internal_schedule_times("12:00,00:00;12:00 invalid 24:00"),
            (0, 720),
        )

    def test_due_and_next_slots_follow_beijing_time(self):
        schedule = (0, 720)
        before_noon = self.beijing(16, 11, 59)
        after_noon = self.beijing(16, 12, 0, 1)

        self.assertEqual(proxy._internal_schedule_slot(before_noon, schedule), self.beijing(16, 0))
        self.assertEqual(proxy._next_internal_schedule_slot(before_noon, schedule), self.beijing(16, 12))
        self.assertEqual(proxy._internal_schedule_slot(after_noon, schedule), self.beijing(16, 12))
        self.assertEqual(proxy._next_internal_schedule_slot(after_noon, schedule), self.beijing(17, 0))

    def test_tick_skips_slot_when_database_already_has_new_report(self):
        now = self.beijing(16, 12, 5)
        latest = self.beijing(16, 12, 1)
        with mock.patch.object(proxy, "INTERNAL_SCHEDULER_ENABLED", True), \
                mock.patch.object(proxy, "INTERNAL_SCHEDULE_TIMES", "00:00,12:00"), \
                mock.patch.object(proxy, "_latest_persisted_report_at", return_value=latest), \
                mock.patch.object(proxy, "_start_internal_scheduled_job") as start:
            self.assertFalse(proxy._internal_scheduler_tick(now))

        start.assert_not_called()
        with proxy.INTERNAL_SCHEDULER_STATE_LOCK:
            self.assertEqual(proxy.INTERNAL_SCHEDULER_STATE["completed_slot"], "2026-07-16T12:00+08:00")

    def test_tick_starts_catchup_when_latest_report_is_older_than_slot(self):
        now = self.beijing(16, 12, 5)
        latest = self.beijing(16, 11, 45)
        with mock.patch.object(proxy, "INTERNAL_SCHEDULER_ENABLED", True), \
                mock.patch.object(proxy, "INTERNAL_SCHEDULE_TIMES", "00:00,12:00"), \
                mock.patch.object(proxy, "_latest_persisted_report_at", return_value=latest), \
                mock.patch.object(proxy, "_start_internal_scheduled_job", return_value=True) as start:
            self.assertTrue(proxy._internal_scheduler_tick(now))

        self.assertEqual(start.call_args.args[0], self.beijing(16, 12))
        self.assertEqual(start.call_args.args[1], now)

    def test_database_check_error_waits_instead_of_risking_duplicate_run(self):
        now = self.beijing(16, 12, 5)
        with mock.patch.object(proxy, "INTERNAL_SCHEDULER_ENABLED", True), \
                mock.patch.object(proxy, "INTERNAL_SCHEDULE_TIMES", "00:00,12:00"), \
                mock.patch.object(proxy, "_latest_persisted_report_at", side_effect=RuntimeError("temporary outage")), \
                mock.patch.object(proxy, "_start_internal_scheduled_job") as start:
            self.assertFalse(proxy._internal_scheduler_tick(now))

        start.assert_not_called()
        with proxy.INTERNAL_SCHEDULER_STATE_LOCK:
            self.assertTrue(proxy.INTERNAL_SCHEDULER_STATE["needs_report_check"])
            self.assertIn("temporary outage", proxy.INTERNAL_SCHEDULER_STATE["last_error"])
            self.assertGreater(proxy.INTERNAL_SCHEDULER_STATE["next_retry_timestamp"], now.timestamp())

    def test_failed_result_is_retried_and_supabase_save_failure_counts_as_error(self):
        self.assertEqual(
            proxy._internal_scheduled_result_error({
                "accounts_requested": 2,
                "accounts_ok": 0,
                "supabase": {"configured": True, "ok": True},
            }),
            "all scheduled accounts failed",
        )
        self.assertIn(
            "database unavailable",
            proxy._internal_scheduled_result_error({
                "accounts_requested": 2,
                "accounts_ok": 2,
                "supabase": {"configured": True, "ok": False, "error": "database unavailable"},
            }),
        )

    def test_status_exposes_beijing_plan_and_next_run(self):
        now = self.beijing(16, 11, 59)
        with mock.patch.object(proxy, "INTERNAL_SCHEDULER_ENABLED", True), \
                mock.patch.object(proxy, "INTERNAL_SCHEDULE_TIMES", "00:00,12:00"):
            status = proxy._internal_scheduler_status(now)

        self.assertTrue(status["enabled"])
        self.assertEqual(status["timezone"], "Asia/Shanghai")
        self.assertEqual(status["times"], ["00:00", "12:00"])
        self.assertEqual(status["next_run_at"], "2026-07-16T12:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
