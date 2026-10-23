"""Fake-clock scheduler tests: no network, credentials, or broker orders."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from src.safety import NY, SafetyError
from src.scheduling import MAX_WAIT_SECONDS, prepare_start


class Timer:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


class Broker:
    def __init__(self, start, timer=None, close="16:00", is_open=True):
        self.start = start
        self.timer = timer or Timer()
        self.close = close
        self.is_open = is_open
        self.session_exists = True
        self.trace = []
        self.clock_transform = lambda value: value
        self.on_calendar = None
        self.account = Mock(side_effect=AssertionError("Scheduler must not read account"))
        self.execute = Mock(side_effect=AssertionError("Scheduler must not submit orders"))

    def clock(self):
        self.trace.append("clock")
        timestamp = self.start + timedelta(seconds=self.timer.elapsed)
        return NS(timestamp=self.clock_transform(timestamp), is_open=self.is_open)

    def calendar(self, start, end):
        self.trace.append("calendar")
        if self.on_calendar:
            self.on_calendar()
        if not self.session_exists:
            return []
        return [NS(date=start + timedelta(days=i), open="09:30", close=self.close)
                for i in range((end - start).days + 1)
                if (start + timedelta(days=i)).weekday() < 5]


class SchedulingTests(unittest.TestCase):
    def run_prepare(self, broker, **kwargs):
        summary = {}
        outcome = prepare_start(
            broker, summary, sleeper=broker.timer.sleep,
            monotonic=broker.timer.monotonic, **kwargs,
        )
        self.assertEqual(broker.trace[:2], ["clock", "calendar"])
        broker.account.assert_not_called()
        broker.execute.assert_not_called()
        return outcome, summary

    def test_normal_dst_and_standard_time_have_distinct_correct_crons(self):
        for day, utc_hour in ((date(2026, 10, 20), 19), (date(2026, 11, 3), 20)):
            with self.subTest(day=day):
                start = datetime(day.year, day.month, day.day, utc_hour, 45, tzinfo=timezone.utc)
                broker = Broker(start)
                outcome, summary = self.run_prepare(broker, event_name="schedule", scheduled_cron=f"45 {utc_hour} * * 1-5")
                self.assertEqual(outcome, "ready")
                self.assertEqual(datetime.fromisoformat(summary["target_time"]).hour, 15)
                self.assertEqual(summary["delay_minutes"], 0)
                self.assertEqual(summary["session_date"], day.isoformat())

    def test_early_close_uses_correct_utc_cron_in_both_offsets(self):
        for day, utc_hour in ((date(2026, 7, 2), 16), (date(2026, 11, 27), 17)):
            with self.subTest(day=day):
                start = datetime(day.year, day.month, day.day, utc_hour, 45, tzinfo=timezone.utc)
                outcome, summary = self.run_prepare(Broker(start, close="13:00"), event_name="schedule", scheduled_cron=f"45 {utc_hour} * * 1-5")
                self.assertEqual(outcome, "ready")
                self.assertEqual(datetime.fromisoformat(summary["target_time"]).hour, 12)

    def test_all_warm_cron_families_are_calendar_derived(self):
        cases = ((date(2026, 10, 20), "16:00", 19, 14),
                 (date(2026, 11, 3), "16:00", 20, 15),
                 (date(2026, 7, 2), "13:00", 16, 11),
                 (date(2026, 11, 27), "13:00", 17, 12))
        for day, close, target_hour, warm_hour in cases:
            with self.subTest(day=day):
                start = datetime(day.year, day.month, day.day, target_hour, 45, tzinfo=timezone.utc)
                outcome, _ = self.run_prepare(Broker(start, close=close), event_name="schedule", scheduled_cron=f"17 {warm_hour} * * 1-5")
                self.assertEqual(outcome, "ready")

    def test_alternative_dst_earlyclose_and_unknown_crons_skip_even_after_close(self):
        for cron in ("45 20 * * 1-5", "45 16 * * 1-5", "45 17 * * 1-5", "17 15 * * 1-5", "17 11 * * 1-5", "*/5 * * * *", ""):
            with self.subTest(cron=cron):
                broker = Broker(datetime(2026, 10, 20, 17, 10, tzinfo=NY), is_open=False)
                outcome, _ = self.run_prepare(broker, event_name="schedule", scheduled_cron=cron, wait_for_window=True)
                self.assertEqual(outcome, "skipped")
                self.assertEqual(broker.timer.sleeps, [])

    def test_true_delayed_schedule_is_missed_even_when_clock_is_closed(self):
        broker = Broker(datetime(2026, 10, 20, 16, 12, tzinfo=NY), is_open=False)
        outcome, summary = self.run_prepare(broker, event_name="schedule", scheduled_cron="45 19 * * 1-5")
        self.assertEqual(outcome, "missed_window")
        self.assertEqual(summary["delay_minutes"], 27)
        self.assertIn("15:50", summary["start_deadline"])
        self.assertIn("16:00", summary["market_close"])

    def test_manual_before_or_after_window_skips_quietly_without_wait(self):
        for hour, minute in ((10, 17), (15, 44), (15, 50), (17, 0)):
            with self.subTest(hour=hour, minute=minute):
                broker = Broker(datetime(2026, 10, 20, hour, minute, tzinfo=NY))
                outcome, _ = self.run_prepare(broker, event_name="workflow_dispatch")
                self.assertEqual(outcome, "skipped")
                self.assertFalse(broker.timer.sleeps)

    def test_holiday_and_weekend_skip_before_any_wait(self):
        holiday = Broker(datetime(2026, 10, 20, 15, 45, tzinfo=NY))
        holiday.session_exists = False
        weekend = Broker(datetime(2026, 10, 24, 15, 45, tzinfo=NY), is_open=False)
        for broker in (holiday, weekend):
            outcome, summary = self.run_prepare(broker, event_name="schedule", scheduled_cron="45 19 * * 1-5", wait_for_window=True)
            self.assertEqual(outcome, "skipped")
            self.assertIsNone(summary["target_time"])
            self.assertEqual(broker.timer.sleeps, [])

    def test_328_minute_warm_start_sleeps_in_at_most_60_second_steps(self):
        broker = Broker(datetime(2026, 10, 20, 14, 17, tzinfo=timezone.utc))
        with self.assertLogs("trading_eod.scheduling", level="INFO") as logs:
            outcome, summary = self.run_prepare(broker, event_name="schedule", scheduled_cron="17 14 * * 1-5", wait_for_window=True)
        self.assertEqual(outcome, "ready")
        self.assertEqual(sum(broker.timer.sleeps), 328 * 60)
        self.assertTrue(all(0 < seconds <= 60 for seconds in broker.timer.sleeps))
        self.assertEqual(broker.trace.count("calendar"), 2)
        self.assertEqual(broker.trace[-2:], ["clock", "calendar"])
        self.assertIn("15:45", summary["observed_at"])
        self.assertEqual(len(logs.records), 2)  # No minute-by-minute log noise.
        self.assertIn("target_et=2026-10-20T15:45:00-04:00", logs.output[0])
        self.assertIn("expected_wait_minutes=328.0", logs.output[0])
        self.assertIn("max_wait_minutes=330", logs.output[0])
        self.assertIn("Prestart wait finished", logs.output[1])
        self.assertIn("elapsed_minutes=328.0", logs.output[1])

    def test_start_deadline_stays_exclusive_with_fractional_seconds(self):
        for delta, expected in ((timedelta(0), "ready"),
                                (timedelta(minutes=5, microseconds=-1), "ready"),
                                (timedelta(minutes=5), "missed_window")):
            broker = Broker(datetime(2026, 10, 20, 15, 45, tzinfo=NY) + delta)
            outcome, _ = self.run_prepare(broker, event_name="schedule", scheduled_cron="45 19 * * 1-5")
            self.assertEqual(outcome, expected)

    def test_closed_market_inside_start_window_blocks(self):
        broker = Broker(datetime(2026, 10, 20, 15, 45, tzinfo=NY), is_open=False)
        with self.assertRaisesRegex(SafetyError, "closed inside"):
            self.run_prepare(broker, event_name="schedule", scheduled_cron="45 19 * * 1-5")

    def test_wait_rechecks_market_open_flag(self):
        broker = Broker(datetime(2026, 10, 20, 15, 44, tzinfo=NY), is_open=False)
        with self.assertRaisesRegex(SafetyError, "closed inside"):
            self.run_prepare(broker, event_name="schedule", scheduled_cron="45 19 * * 1-5", wait_for_window=True)
        self.assertEqual(broker.trace.count("calendar"), 2)

    def test_clock_jump_past_start_deadline_does_not_authorize_orders(self):
        broker = Broker(datetime(2026, 10, 20, 15, 44, tzinfo=NY))
        broker.clock_transform = lambda value: value + timedelta(minutes=10) if broker.timer.elapsed else value
        outcome, _ = self.run_prepare(broker, event_name="schedule", scheduled_cron="45 19 * * 1-5", wait_for_window=True)
        self.assertEqual(outcome, "missed_window")

    def test_session_date_change_during_wait_blocks(self):
        broker = Broker(datetime(2026, 10, 20, 15, 44, tzinfo=NY))
        broker.clock_transform = lambda value: value + timedelta(days=1) if broker.timer.elapsed else value
        with self.assertRaisesRegex(SafetyError, "date changed"):
            self.run_prepare(broker, wait_for_window=True)

    def test_calendar_target_change_or_session_removal_after_wait_blocks(self):
        for remove in (False, True):
            with self.subTest(remove=remove):
                broker = Broker(datetime(2026, 10, 20, 15, 44, tzinfo=NY))
                def change_calendar():
                    if broker.timer.elapsed:
                        if remove:
                            broker.session_exists = False
                        else:
                            broker.close = "13:00"
                broker.on_calendar = change_calendar
                with self.assertRaisesRegex(SafetyError, "session or target changed"):
                    self.run_prepare(broker, wait_for_window=True)

    def test_stalled_broker_clock_stops_at_monotonic_limit(self):
        broker = Broker(datetime(2026, 10, 20, 15, 44, tzinfo=NY))
        broker.clock_transform = lambda value: broker.start
        with self.assertRaisesRegex(SafetyError, "330 minutes exhausted"):
            self.run_prepare(broker, wait_for_window=True)
        self.assertEqual(broker.timer.elapsed, MAX_WAIT_SECONDS)
        self.assertEqual(max(broker.timer.sleeps), 60)

    def test_target_too_far_away_is_rejected_without_sleeping(self):
        broker = Broker(datetime(2026, 10, 20, 9, 0, tzinfo=NY))
        with self.assertRaisesRegex(SafetyError, "exceeds maximum"):
            self.run_prepare(broker, wait_for_window=True)
        self.assertFalse(broker.timer.sleeps)

    def test_manual_explicit_wait_can_reach_window_without_changing_trade_mode(self):
        broker = Broker(datetime(2026, 10, 20, 15, 44, 30, tzinfo=NY))
        outcome, summary = self.run_prepare(broker, event_name="workflow_dispatch", wait_for_window=True)
        self.assertEqual(outcome, "ready")
        self.assertEqual(broker.timer.sleeps, [30])
        self.assertNotIn("dry_run", summary)
        self.assertNotIn("session_claimed", summary)

    def test_preflight_wakes_at_target_minus_five_and_runs_only_once(self):
        broker = Broker(datetime(2026, 10, 20, 15, 38, 30, tzinfo=NY))
        observed = []
        preflight = Mock(side_effect=lambda window: observed.append(
            (broker.start + timedelta(seconds=broker.timer.elapsed), window.target),
        ))
        outcome, summary = self.run_prepare(broker, wait_for_window=True, preflight=preflight)
        self.assertEqual(outcome, "ready")
        preflight.assert_called_once()
        self.assertEqual(observed[0][0], observed[0][1] - timedelta(minutes=5))
        self.assertEqual(broker.timer.sleeps[:2], [60, 30])
        self.assertTrue(all(0 < seconds <= 60 for seconds in broker.timer.sleeps))
        self.assertEqual(summary["preflight"], {
            "status": "completed", "observed_at": "2026-10-20T15:40:00-04:00",
        })
        self.assertNotIn("session_claimed", summary)

    def test_preflight_runs_immediately_if_wait_begins_within_its_window(self):
        broker = Broker(datetime(2026, 10, 20, 15, 43, 30, tzinfo=NY))
        preflight = Mock(side_effect=lambda window: self.assertEqual(broker.timer.elapsed, 0))
        outcome, _ = self.run_prepare(broker, wait_for_window=True, preflight=preflight)
        self.assertEqual(outcome, "ready")
        preflight.assert_called_once()

    def test_early_close_preflight_uses_calendar_target(self):
        broker = Broker(datetime(2026, 11, 27, 12, 39, 30, tzinfo=NY), close="13:00")
        preflight = Mock()
        outcome, summary = self.run_prepare(
            broker, event_name="schedule", scheduled_cron="17 12 * * 1-5",
            wait_for_window=True, preflight=preflight,
        )
        self.assertEqual(outcome, "ready")
        self.assertEqual(summary["preflight"]["observed_at"], "2026-11-27T12:40:00-05:00")
        self.assertEqual(preflight.call_args.args[0].target.hour, 12)

    def test_preflight_does_not_run_on_immediate_ready_late_or_nonwaiting_runs(self):
        for hour, minute, wait, expected in ((15, 45, True, "ready"),
                                             (15, 50, True, "missed_window"),
                                             (15, 42, False, "skipped")):
            with self.subTest(minute=minute):
                broker = Broker(datetime(2026, 10, 20, hour, minute, tzinfo=NY))
                preflight = Mock()
                outcome, summary = self.run_prepare(
                    broker, event_name="schedule", scheduled_cron="45 19 * * 1-5",
                    wait_for_window=wait, preflight=preflight,
                )
                self.assertEqual(outcome, expected)
                preflight.assert_not_called()
                self.assertNotIn("preflight", summary)

    def test_preflight_does_not_run_on_holiday_or_alternative_cron(self):
        for holiday in (False, True):
            with self.subTest(holiday=holiday):
                broker = Broker(datetime(2026, 10, 20, 15, 42, tzinfo=NY))
                broker.session_exists = not holiday
                preflight = Mock()
                outcome, _ = self.run_prepare(
                    broker, event_name="schedule", scheduled_cron="45 20 * * 1-5",
                    wait_for_window=True, preflight=preflight,
                )
                self.assertEqual(outcome, "skipped")
                preflight.assert_not_called()

    def test_preflight_does_not_run_when_clock_reports_market_closed(self):
        broker = Broker(datetime(2026, 10, 20, 15, 42, tzinfo=NY), is_open=False)
        preflight = Mock()
        with self.assertRaisesRegex(SafetyError, "closed inside"):
            self.run_prepare(broker, wait_for_window=True, preflight=preflight)
        preflight.assert_not_called()

    def test_preflight_failure_is_sanitized_nonfatal_and_not_retried(self):
        broker = Broker(datetime(2026, 10, 20, 15, 44, tzinfo=NY))
        preflight = Mock(side_effect=RuntimeError("private-token https://secret.example"))
        with self.assertLogs("trading_eod.scheduling", level="WARNING") as logs:
            outcome, summary = self.run_prepare(broker, wait_for_window=True, preflight=preflight)
        self.assertEqual(outcome, "ready")
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["preflight"]["status"], "failed")
        preflight.assert_called_once()
        for private in ("private-token", "https://secret.example"):
            self.assertNotIn(private, str(summary))
            self.assertNotIn(private, " ".join(logs.output))

    def test_clock_is_reread_after_slow_preflight_without_sleeping_past_target(self):
        broker = Broker(datetime(2026, 10, 20, 15, 44, tzinfo=NY))
        def slow_preflight(window):
            broker.timer.elapsed += 70
        outcome, summary = self.run_prepare(broker, wait_for_window=True, preflight=slow_preflight)
        self.assertEqual(outcome, "ready")
        self.assertEqual(broker.timer.sleeps, [])
        self.assertEqual(summary["observed_at"], "2026-10-20T15:45:10-04:00")
        self.assertEqual(broker.trace[-2:], ["clock", "calendar"])

    def test_preflight_that_overruns_start_deadline_cannot_authorize_run(self):
        broker = Broker(datetime(2026, 10, 20, 15, 44, tzinfo=NY))
        def too_slow(window):
            broker.timer.elapsed += 7 * 60
        preflight = Mock(side_effect=too_slow)
        outcome, _ = self.run_prepare(
            broker, event_name="schedule", scheduled_cron="45 19 * * 1-5",
            wait_for_window=True, preflight=preflight,
        )
        self.assertEqual(outcome, "missed_window")
        preflight.assert_called_once()
        self.assertEqual(broker.timer.sleeps, [])

    def test_clock_jump_over_preflight_window_never_calls_it_late(self):
        broker = Broker(datetime(2026, 10, 20, 15, 39, tzinfo=NY))
        broker.clock_transform = lambda value: value + timedelta(minutes=5) if broker.timer.elapsed else value
        preflight = Mock()
        outcome, _ = self.run_prepare(broker, wait_for_window=True, preflight=preflight)
        self.assertEqual(outcome, "ready")
        preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
