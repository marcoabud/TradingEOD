"""Same-session swing signal, history feed, weekly catch-up and wide schedule."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

import numpy as np
import pandas as pd

from src.bot import Config, run
from src.core_recovery import CoreRecoveryStore
from src.data import MarketData
from src.execution import ranked_candidates, select_slots
from src.safety import NY, SafetyError
from src.scheduling import KNOWN_CRONS, WIDE_FALLBACK_CRON
from src.strategy import (SWING_UNIVERSE, compute_indicators, entry_candidate, entry_eligible,
                          swing_entries, swing_exit_reasons, swing_signal)
from tests.test_bot import FakeBroker, FakeData, indicators
from src.scheduling import prepare_start
from tests.test_scheduling import Broker as ScheduleBroker


def frame(closes):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({"close": closes, "high": closes * 1.01, "low": closes * 0.99},
                        index=pd.date_range("2025-01-01", periods=len(closes)))


def trending(n=251, seed=3):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rng.normal(0.002, 0.01, n)))


class SameSessionSignalTests(unittest.TestCase):
    def test_rolled_signal_equals_recomputing_with_todays_close(self):
        closes = trending()
        live = compute_indicators("AAPL", frame(closes[:250]), current_price=closes[250])
        final = compute_indicators("AAPL", frame(closes[1:]))
        signal = swing_signal(live)
        self.assertAlmostEqual(signal.rsi2, final.rsi2, places=6)
        self.assertAlmostEqual(signal.sma5, final.sma5, places=9)
        self.assertAlmostEqual(signal.sma200, final.sma200, places=9)
        self.assertEqual(live.atr14, compute_indicators("AAPL", frame(closes[:250])).atr14)

    def test_yesterdays_oversold_reading_does_not_enter_after_todays_bounce(self):
        closes = trending(250)
        closes[-3:] = [closes[-4] * 0.98, closes[-4] * 0.96, closes[-4] * 0.94]
        base = compute_indicators("AAPL", frame(closes))
        self.assertLess(base.rsi2, 10)
        bounced = replace(base, price=closes[-1] * 1.04)
        self.assertGreater(swing_signal(bounced).rsi2, 10)
        self.assertFalse(entry_eligible(bounced))
        still_falling = replace(base, price=closes[-1] * 0.99)
        self.assertTrue(entry_eligible(still_falling))

    def test_todays_selloff_enters_even_if_yesterday_was_not_oversold(self):
        closes = trending(250)
        base = compute_indicators("AAPL", frame(closes))
        dropped = replace(base, price=closes[-1] * 0.95)
        if base.rsi2 >= 10 and dropped.price > swing_signal(dropped).sma200:
            self.assertTrue(entry_eligible(dropped))
        self.assertLess(swing_signal(dropped).rsi2, base.rsi2)

    def test_exit_uses_todays_rsi_and_average(self):
        closes = trending(250)
        closes[-3:] = [closes[-4] * 0.98, closes[-4] * 0.96, closes[-4] * 0.94]
        base = compute_indicators("AAPL", frame(closes))
        rallied = replace(base, price=closes[-1] * 1.06)
        reasons = swing_exit_reasons(rallied, entry_price=closes[-1])
        self.assertIn("RSI2_ABOVE_70", reasons)
        self.assertIn("PRICE_ABOVE_SMA5", reasons)

    def test_missing_quote_is_no_signal_with_roll_state_but_strict_for_fixtures(self):
        base = compute_indicators("AAPL", frame(trending(250)))
        self.assertFalse(entry_candidate(base))
        with self.assertRaises(Exception):
            swing_signal(base)
        fixture = replace(indicators()["AAPL"], rsi2=5, price=None)
        with self.assertRaises(Exception):
            entry_candidate(fixture)

    def test_swing_entries_rank_by_same_session_rsi(self):
        closes = trending(250)
        values = {}
        for offset, symbol in enumerate(SWING_UNIVERSE):
            base = compute_indicators(symbol, frame(closes))
            drop = 0.90 - 0.005 * offset if symbol in ("MSFT", "NVDA", "V") else 1.01
            values[symbol] = replace(base, price=closes[-1] * drop)
        chosen = swing_entries(values, ())
        ordered = sorted(("MSFT", "NVDA", "V"), key=lambda s: swing_signal(values[s]).rsi2)
        eligible = [s for s in ordered if entry_eligible(values[s])]
        self.assertEqual(chosen, eligible[:2])


class Quotes:
    def __init__(self, values, prices, failed=()):
        self.indicators = {s: replace(i, price=None) for s, i in values.items()}
        self.prices, self.failed, self.calls = prices, set(failed), []

    def fetch(self, symbols, phase, *, budget=None):
        self.calls.append((tuple(symbols), phase, budget))
        for symbol in symbols:
            price = None if symbol in self.failed else self.prices[symbol]
            self.indicators[symbol] = replace(self.indicators[symbol], price=price)
        return self.failed & set(symbols)


class SelectSlotsTests(unittest.TestCase):
    def setUp(self):
        closes = trending(250)
        self.values = {s: compute_indicators(s, frame(closes)) for s in SWING_UNIVERSE}
        self.prices = {s: closes[-1] * 1.01 for s in SWING_UNIVERSE}
        self.last = closes[-1]

    def test_single_scan_with_its_own_single_attempt_budget(self):
        self.prices.update(AAPL=self.last * 0.93, JPM=self.last * 0.90, V=self.last * 0.92)
        quotes = Quotes(self.values, self.prices)
        pool = ranked_candidates(self.values, {"MSFT"})
        self.assertNotIn("MSFT", pool)
        self.assertEqual(len(pool), len(SWING_UNIVERSE) - 1)
        selected, reserved = select_slots(quotes, pool, ())
        self.assertEqual(len(quotes.calls), 1)
        budget = quotes.calls[0][2]
        self.assertEqual((budget.max_attempts, budget.seconds), (1, 8))
        eligible = sorted((s for s in ("AAPL", "JPM", "V") if entry_eligible(quotes.indicators[s])),
                          key=lambda s: swing_signal(quotes.indicators[s]).rsi2)
        self.assertEqual(selected, eligible[:2])
        self.assertEqual(reserved, [])

    def test_failed_quote_is_skipped_without_reserving_or_blocking_others(self):
        self.prices.update(AAPL=self.last * 0.93, JPM=self.last * 0.90)
        quotes = Quotes(self.values, self.prices, failed={"JPM", "BRK.B"})
        selected, reserved = select_slots(quotes, ranked_candidates(self.values, ()), {"NVDA"})
        self.assertEqual(reserved, [])
        self.assertNotIn("JPM", selected)
        self.assertLessEqual(len(selected), 1)

    def test_full_slots_never_scan(self):
        quotes = Quotes(self.values, self.prices)
        self.assertEqual(select_slots(quotes, ranked_candidates(self.values, ()), {"AAPL", "V"}), ([], []))
        self.assertEqual(quotes.calls, [])


class EndToEndTests(unittest.TestCase):
    """Real roll-state indicators through the full engine with a fake broker."""
    WEDNESDAY = datetime(2026, 10, 14, 15, 45, tzinfo=NY)

    def values(self):
        closes = trending(250)
        oversold = closes.copy()
        oversold[-3:] = [closes[-4] * 0.98, closes[-4] * 0.96, closes[-4] * 0.94]
        result = {s: compute_indicators(s, frame(closes), current_price=closes[-1] * 1.005)
                  for s in ("SPY", "QQQ", "SMH", "GLD", "SGOV", *SWING_UNIVERSE)}
        # AAPL: oversold yesterday, bounced today. MSFT: sells off today.
        result["AAPL"] = compute_indicators("AAPL", frame(oversold), current_price=oversold[-1] * 1.05)
        result["MSFT"] = compute_indicators("MSFT", frame(closes), current_price=closes[-1] * 0.94)
        self.assertLess(result["AAPL"].rsi2, 10)
        self.assertGreater(result["MSFT"].rsi2, 10)
        return result

    def evaluate(self, dry):
        broker = FakeBroker(now=self.WEDNESDAY)
        store = Mock()
        store.pending.return_value = None
        store.latest_intent_date.return_value = None
        summary = {}
        run(Config("fake", "fake", True, dry), broker, FakeData(self.values()), summary,
            claim=Mock(return_value=True), observe=Mock(return_value=True), core_store=store)
        return summary, broker

    def test_buys_todays_selloff_not_yesterdays_bounced_reading(self):
        summary, broker = self.evaluate(dry=False)
        self.assertEqual([(o["symbol"], o["side"]) for o in broker.executed], [("MSFT", "buy")])
        self.assertEqual(broker.executed[0]["amount"], 600)
        self.assertEqual(summary["status"], "completed")
        self.assertLess(summary["indicators"]["MSFT"]["signal"]["rsi2"], 10)

    def test_dry_run_plans_the_same_entry(self):
        summary, broker = self.evaluate(dry=True)
        self.assertEqual(broker.executed, [])
        self.assertEqual([(o["symbol"], o["side"]) for o in summary["planned_orders"]], [("MSFT", "buy")])


class HistoryFeedTests(unittest.TestCase):
    def test_history_feed_is_independent_and_validated(self):
        data = MarketData("k", "s", "iex", quote_transport=Mock(), history_feed="sip")
        self.assertEqual((data.feed.value, data.history_feed.value), ("iex", "sip"))
        self.assertEqual(MarketData("k", "s", "iex", quote_transport=Mock()).history_feed.value, "iex")
        with self.assertRaises(SafetyError):
            MarketData("k", "s", "iex", quote_transport=Mock(), history_feed="otc")

    def test_config_reads_history_feed(self):
        env = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ALPACA_DATA_FEED": "iex",
               "ALPACA_HISTORY_FEED": "sip"}
        self.assertEqual(Config.from_env(env).history_feed, "sip")
        del env["ALPACA_HISTORY_FEED"]
        self.assertEqual(Config.from_env(env).history_feed, "")


class WeeklyCatchUpTests(unittest.TestCase):
    MONDAY = datetime(2026, 11, 9, 15, 45, tzinfo=NY)

    def evaluate(self, store, now=None):
        broker = FakeBroker(now=now or self.MONDAY)
        values = {s: replace(v, as_of=((now or self.MONDAY)-timedelta(days=1)).date().isoformat())
                  for s, v in indicators().items()}
        summary = {}
        run(Config("fake", "fake", True, False), broker, FakeData(values), summary,
            claim=Mock(return_value=True), observe=Mock(return_value=True), core_store=store)
        return summary, broker

    def store(self, latest):
        store = Mock()
        store.pending.return_value = None
        store.latest_intent_date.return_value = latest
        store.begin.side_effect = lambda day, *a, **k: {"session_date": day.isoformat()}
        store.finish.return_value = True
        return store

    def test_missed_weekly_session_is_reviewed_on_next_session(self):
        store = self.store(date(2026, 10, 30))
        summary, _ = self.evaluate(store)
        self.assertFalse(summary["weekly_rebalance"])
        self.assertTrue(summary["core_due"])
        self.assertEqual(summary["core_catch_up"]["missed_weekly_session"], "2026-11-06")
        self.assertNotEqual(summary["regime"], "HOLD_UNTIL_WEEK_END")
        store.begin.assert_called_once()

    def test_reviewed_week_or_new_account_is_not_caught_up(self):
        for latest in (date(2026, 11, 6), None):
            store = self.store(latest)
            summary, _ = self.evaluate(store)
            self.assertFalse(summary["core_due"])
            self.assertEqual(summary["regime"], "HOLD_UNTIL_WEEK_END")
            store.begin.assert_not_called()

    def test_real_store_reports_latest_intent(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CoreRecoveryStore("fake-account", True, environ={"EOD_STATE_DIR": folder})
            self.assertIsNone(store.latest_intent_date())


class WideScheduleTests(unittest.TestCase):
    def run_prepare(self, broker, **kwargs):
        summary = {}
        outcome = prepare_start(broker, summary, sleeper=broker.timer.sleep,
                                monotonic=broker.timer.monotonic, **kwargs)
        broker.account.assert_not_called()
        broker.execute.assert_not_called()
        return outcome, summary

    def test_wide_fallback_family_is_known_and_can_wait(self):
        self.assertIn(WIDE_FALLBACK_CRON, KNOWN_CRONS)
        broker = ScheduleBroker(datetime(2026, 11, 3, 18, 41, tzinfo=timezone.utc))
        outcome, summary = self.run_prepare(broker, event_name="schedule",
                                            scheduled_cron=WIDE_FALLBACK_CRON, wait_for_window=True)
        self.assertEqual(outcome, "ready")
        self.assertEqual(summary["schedule_role"], "fallback")

    def test_late_wide_fallback_stays_quiet(self):
        broker = ScheduleBroker(datetime(2026, 11, 3, 21, 27, tzinfo=timezone.utc))
        outcome, _ = self.run_prepare(broker, event_name="schedule",
                                      scheduled_cron=WIDE_FALLBACK_CRON, wait_for_window=True)
        self.assertEqual(outcome, "skipped")


if __name__ == "__main__":
    unittest.main()
