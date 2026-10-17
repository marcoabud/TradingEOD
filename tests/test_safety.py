"""Calendar and monetary safety fixtures require no broker or market data."""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest

from src.safety import (
    NY, SafetyError, Window, aware, decimal, midnight, money, quantity,
    resolve_window, session_time, usable_cash, validate_account,
)


def calendar_row(day, close="16:00:00"):
    return SimpleNamespace(date=day, open="09:30:00", close=close)


def account(**changes):
    values = dict(
        status="ACTIVE", equity="4000", cash="1000", buying_power="8000",
        non_marginable_buying_power="2000", multiplier="2",
        trading_blocked=False, account_blocked=False, trade_suspended_by_user=False,
    )
    values.update(changes)
    return SimpleNamespace(**values)


class CalendarTests(unittest.TestCase):
    def test_dst_changes_utc_start_but_keeps_new_york_1545(self):
        for day, utc_hour in ((date(2026, 3, 6), 20), (date(2026, 3, 9), 19)):
            with self.subTest(day=day):
                clock = SimpleNamespace(is_open=True, timestamp=datetime.combine(day, time(utc_hour, 45), timezone.utc))
                resolved = resolve_window(clock, [calendar_row(day)])
                self.assertTrue(resolved.accepts_start(clock.timestamp))
                self.assertEqual(resolved.target.hour, 15)
                self.assertEqual(resolved.target.astimezone(timezone.utc).hour, utc_hour)
                self.assertFalse(resolved.accepts_start(clock.timestamp + timedelta(hours=1)))
                self.assertFalse(resolved.accepts_start(clock.timestamp - timedelta(hours=1)))

    def test_early_close_uses_calendar_1245_target(self):
        day = date(2026, 11, 27)
        clock = SimpleNamespace(is_open=True, timestamp=datetime(2026, 11, 27, 17, 45, tzinfo=timezone.utc))
        resolved = resolve_window(clock, [calendar_row(day, close="13:00:00")])
        self.assertEqual(resolved.target, datetime(2026, 11, 27, 12, 45, tzinfo=NY))
        self.assertTrue(resolved.accepts_start(clock.timestamp))
        self.assertFalse(resolved.accepts_start(datetime(2026, 11, 27, 15, 45, tzinfo=NY)))

    def test_closed_market_or_absent_trading_day_is_clean_skip(self):
        day = date(2026, 10, 16)
        clock = SimpleNamespace(is_open=False, timestamp=datetime(2026, 10, 16, 15, 45, tzinfo=NY))
        self.assertIsNone(resolve_window(clock, [calendar_row(day)]))
        clock.is_open = True
        self.assertIsNone(resolve_window(clock, []))

    def test_friday_holiday_makes_thursday_weekly_rebalance(self):
        # Calendar deliberately contains only the exchange's open sessions.
        thursday = date(2026, 4, 2)
        clock = SimpleNamespace(is_open=True, timestamp=datetime(2026, 4, 2, 15, 45, tzinfo=NY))
        rows = [calendar_row(thursday - timedelta(days=i)) for i in range(4)]
        self.assertTrue(resolve_window(clock, rows).weekly_rebalance)
        rows.append(calendar_row(thursday + timedelta(days=1)))
        self.assertFalse(resolve_window(clock, rows).weekly_rebalance)

    def test_normal_friday_is_weekly_even_with_next_week_in_calendar(self):
        friday = date(2026, 10, 16)
        rows = [calendar_row(friday), calendar_row(friday + timedelta(days=3))]
        clock = SimpleNamespace(is_open=True, timestamp=datetime(2026, 10, 16, 15, 45, tzinfo=NY))
        self.assertTrue(resolve_window(clock, rows).weekly_rebalance)

    def test_start_and_order_deadlines_are_exclusive_at_end(self):
        day = date(2026, 10, 16)
        window = Window(day, datetime(2026, 10, 16, 9, 30, tzinfo=NY), datetime(2026, 10, 16, 16, tzinfo=NY), True)
        target = window.target
        self.assertFalse(window.accepts_start(target - timedelta(microseconds=1)))
        self.assertTrue(window.accepts_start(target))
        self.assertTrue(window.accepts_start(target + timedelta(minutes=5, microseconds=-1)))
        self.assertFalse(window.accepts_start(target + timedelta(minutes=5)))
        self.assertTrue(window.accepts_order(target + timedelta(minutes=10, microseconds=-1)))
        self.assertFalse(window.accepts_order(target + timedelta(minutes=10)))

    def test_midnight_uses_session_timezone_across_dst_boundary(self):
        self.assertEqual(midnight(date(2026, 3, 8)), datetime(2026, 3, 8, 5, tzinfo=timezone.utc))
        self.assertEqual(midnight(date(2026, 3, 9)), datetime(2026, 3, 9, 4, tzinfo=timezone.utc))

    def test_naive_clock_timestamp_is_rejected(self):
        with self.assertRaises(SafetyError):
            aware(datetime(2026, 10, 16, 15, 45))
        self.assertEqual(aware("2026-10-16T19:45:00Z").tzinfo, timezone.utc)

    def test_session_time_handles_sdk_datetimes_and_text_times(self):
        day = date(2026, 10, 16)
        expected = datetime(2026, 10, 16, 16, tzinfo=NY)
        self.assertEqual(session_time("16:00:00", day), expected)
        self.assertEqual(session_time(datetime(2026, 10, 16, 16), day), expected)
        self.assertEqual(session_time(datetime(2026, 10, 16, 20, tzinfo=timezone.utc), day), expected)


class MonetaryTests(unittest.TestCase):
    def test_notional_is_floored_to_cents_and_shares_to_nine_places(self):
        self.assertEqual(money("599.999999"), Decimal("599.99"))
        self.assertEqual(money("0.009"), Decimal("0.00"))
        self.assertEqual(quantity("1.123456789999"), Decimal("1.123456789"))
        self.assertEqual(quantity("0.0000000009"), Decimal("0.000000000"))
        self.assertEqual(decimal(0.1), Decimal("0.1"))

    def test_nonfinite_values_fail_closed(self):
        for value in ("NaN", "Infinity", "-Infinity", float("nan")):
            with self.subTest(value=value), self.assertRaises(SafetyError):
                decimal(value)

    def test_cash_cap_never_uses_leverage_and_respects_broker_restrictions(self):
        self.assertEqual(usable_cash(account(cash="100.009")), Decimal("100.00"))
        self.assertEqual(usable_cash(account(cash="100", buying_power="40.129")), Decimal("40.12"))
        self.assertEqual(usable_cash(account(cash="100", non_marginable_buying_power="12.349")), Decimal("12.34"))
        self.assertEqual(usable_cash(account(cash="-0.01")), Decimal("0.00"))
        self.assertEqual(usable_cash(account(buying_power="0")), Decimal("0.00"))

    def test_invalid_cash_capacity_is_rejected(self):
        for key in ("cash", "buying_power", "non_marginable_buying_power"):
            with self.subTest(key=key), self.assertRaises(SafetyError):
                usable_cash(account(**{key: "NaN"}))

    def test_healthy_account_is_accepted(self):
        self.assertIsNone(validate_account(account()))

    def test_blocked_debtor_or_uncertified_cash_account_is_rejected(self):
        cases = (
            {"status": "INACTIVE"}, {"trading_blocked": True}, {"account_blocked": True},
            {"trade_suspended_by_user": True}, {"equity": "0"}, {"cash": "-1"},
            {"multiplier": "1"},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(SafetyError):
                validate_account(account(**changes))


if __name__ == "__main__":
    unittest.main()
