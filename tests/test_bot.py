"""Order-phase and allocation integration tests with an in-memory broker."""
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from src.bot import Config, CORE, UNIVERSE, plan_buys, plan_sells, run
from src.safety import NY, SafetyError
from src.strategy import CoreDecision, Indicators, core_targets


def indicators():
    return {s: Indicators(s, 100, 110, 110, 90, 50, 4, .1, .2, .2, "2026-10-15") for s in UNIVERSE}


def position(symbol, qty, entry=100):
    return NS(symbol=symbol, qty=D(str(qty)), avg_entry_price=D(str(entry)))


class FakeBroker:
    def __init__(self, now=None, holdings=None, cash=4000):
        self.now = now or datetime(2026, 10, 16, 15, 45, tzinfo=NY)
        self.is_open = True
        self.holdings = holdings or {}
        self.cash = D(str(cash))
        self.equity = D(4000)
        self.trace = []
        self.executed = []
        self.bought = set()
        self.sold = set()
        self.history = []
        self.fail_sell = False
        self.leave_position_on_sell = False
        self.cash_after_sell = None
        self.buy_account_cash = None
        self.account_calls = 0

    def clock(self):
        self.trace.append("clock")
        return NS(timestamp=self.now, is_open=self.is_open)

    def calendar(self, start, end):
        self.trace.append("calendar")
        return [NS(date=start+timedelta(days=i), open="09:30", close="16:00")
                for i in range((end-start).days+1) if (start+timedelta(days=i)).weekday() < 5]

    def account(self):
        self.trace.append("account")
        self.account_calls += 1
        if self.buy_account_cash is not None and self.account_calls >= 3:
            self.cash = D(str(self.buy_account_cash))
        return NS(id="fake-account", equity=self.equity, cash=self.cash,
                  buying_power=self.cash, non_marginable_buying_power=self.cash,
                  last_equity="3900", status="ACTIVE", multiplier="2",
                  trading_blocked=False, account_blocked=False)

    def positions(self):
        self.trace.append("positions")
        return dict(self.holdings)

    def orders(self, day):
        self.trace.append("orders")
        return self.history

    def traded_symbols(self, day, side):
        self.trace.append(f"traded_{side}")
        return set(self.bought if side == "buy" else self.sold)

    def assert_no_open_orders(self):
        self.trace.append("no_open_orders")

    def assert_assets(self, universe):
        self.trace.append("assets")

    def assert_time(self, window):
        self.trace.append("time")

    def execute(self, *, symbol, side, amount, window, summary):
        self.trace.append(f"execute_{side}_{symbol}")
        if side == "sell" and self.fail_sell:
            raise SafetyError("Unconfirmed/partial sell")
        if side == "sell":
            self.cash += amount * 100
            if self.cash_after_sell is not None:
                self.cash = D(str(self.cash_after_sell))
            if not self.leave_position_on_sell:
                left = self.holdings[symbol].qty - amount
                if left <= 0:
                    del self.holdings[symbol]
                else:
                    self.holdings[symbol] = position(symbol, left)
            self.sold.add(symbol)
        else:
            self.cash -= amount
            old_qty = self.holdings[symbol].qty if symbol in self.holdings else D(0)
            self.holdings[symbol] = position(symbol, old_qty + amount / 100)
            self.bought.add(symbol)
        order = dict(symbol=symbol, side=side, amount=amount)
        self.executed.append(order)
        summary["orders"].append(order)


class FakeData:
    def __init__(self, values=None):
        self.values = values or indicators()
        self.load = Mock(return_value=self.values)
        self.price_calls = 0

    def prices(self, symbols, now):
        self.price_calls += 1
        return {s: self.values[s].price for s in symbols}


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.ind = indicators()
        self.decision = CoreDecision("RISK-ON", {"SPY": .35, "QQQ": .35}, {}, ())

    def test_three_percentage_point_band_is_absolute_and_strict(self):
        positions = {"SPY": position("SPY", "12.81")}
        sells, buys, _ = plan_sells(positions, self.ind, D(4000), self.decision, set())
        self.assertNotIn("SPY", buys)  # $119 difference < 3% of $4,000.
        positions["SPY"] = position("SPY", "12.8")
        _, buys, _ = plan_sells(positions, self.ind, D(4000), self.decision, set())
        self.assertIn("SPY", buys)  # $120 exactly must rebalance.
        positions["SPY"] = position("SPY", "15.2")
        sells, _, _ = plan_sells(positions, self.ind, D(4000), self.decision, set())
        self.assertEqual(sells["SPY"]["qty"], D("1.2"))

    def test_target_zero_residual_below_band_is_retained(self):
        positions = {"GLD": position("GLD", "1.19")}
        sells, _, _ = plan_sells(positions, self.ind, D(4000), self.decision, set())
        self.assertNotIn("GLD", sells)
        positions["GLD"] = position("GLD", "1.2")
        sells, _, _ = plan_sells(positions, self.ind, D(4000), self.decision, set())
        self.assertEqual(sells["GLD"]["qty"], D("1.2"))

    def test_bought_today_core_cannot_be_sold(self):
        positions = {"GLD": position("GLD", 10)}
        sells, _, protected = plan_sells(positions, self.ind, D(4000), self.decision, {"GLD"})
        self.assertNotIn("GLD", sells)
        self.assertEqual(protected[0]["reason"], "ANTI_PDT_CORE")

    def test_allocations_respect_block_limits_and_one_dollar_cash_buffer(self):
        self.ind["AAPL"] = replace(self.ind["AAPL"], rsi2=1)
        self.ind["MSFT"] = replace(self.ind["MSFT"], rsi2=2)
        plan = plan_buys({}, self.ind, D(4000), D(4000), self.decision, {"SPY", "QQQ"}, set())
        core = sum(p["notional"] for p in plan if p["block"] == "core")
        swing = sum(p["notional"] for p in plan if p["block"] == "swing")
        self.assertEqual(core, D(2800))
        self.assertEqual(swing, D(1199))
        self.assertEqual(sum(p["notional"] for p in plan), D(3999))
        self.assertTrue(all(p["notional"] <= 600 for p in plan if p["block"] == "swing"))

    def test_existing_residual_consumes_core_budget_and_no_swing_signal_keeps_cash(self):
        plan = plan_buys({"GLD": position("GLD", 1)}, self.ind, D(4000), D(3900),
                         self.decision, {"SPY", "QQQ"}, set())
        self.assertEqual(sum(p["notional"] for p in plan), D(2700))
        self.assertTrue(all(p["block"] == "core" for p in plan))

    def test_no_same_day_reentry_and_minimum_notional(self):
        self.ind["AAPL"] = replace(self.ind["AAPL"], rsi2=1)
        self.ind["MSFT"] = replace(self.ind["MSFT"], rsi2=2)
        plan = plan_buys({}, self.ind, D(4000), D(1200), None, set(), {"AAPL"})
        self.assertEqual([p["symbol"] for p in plan], ["MSFT"])
        self.assertEqual(plan_buys({}, self.ind, D(4000), D("1.99"), None, set(), set()), [])


class RunTests(unittest.TestCase):
    def run_bot(self, broker=None, data=None, dry=False, claim=None):
        broker = broker or FakeBroker()
        data = data or FakeData()
        claim = claim or Mock(return_value=True)
        summary = {}
        run(Config("fake-key", "fake-secret", True, dry), broker, data, summary,
            claim=claim, observe=Mock(return_value=True))
        return summary, broker, data, claim

    def test_closed_market_skips_before_account_or_data(self):
        broker = FakeBroker()
        broker.is_open = False
        summary, broker, data, claim = self.run_bot(broker)
        self.assertEqual(broker.trace, ["clock", "calendar"])
        self.assertEqual(summary["status"], "skipped")
        data.load.assert_not_called()
        claim.assert_not_called()

    def test_wrong_dst_cron_window_skips_before_account(self):
        broker = FakeBroker(now=datetime(2026, 10, 16, 16, 45, tzinfo=NY))
        summary, broker, _, claim = self.run_bot(broker)
        self.assertEqual(broker.trace, ["clock", "calendar"])
        self.assertEqual(summary["status"], "skipped")
        claim.assert_not_called()

    def test_core_is_evaluated_only_on_last_session_of_week(self):
        with patch("src.bot.core_targets", wraps=core_targets) as core:
            summary, broker, _, _ = self.run_bot(FakeBroker(now=datetime(2026, 10, 14, 15, 45, tzinfo=NY)), dry=True)
            core.assert_not_called()
            self.assertEqual(summary["regime"], "HOLD_UNTIL_WEEK_END")
            self.assertFalse(summary["weekly_rebalance"])
            summary, _, _, _ = self.run_bot(dry=True)
            self.assertEqual(core.call_count, 1)
            self.assertTrue(summary["weekly_rebalance"])

    def test_dry_run_claims_no_session_and_submits_nothing(self):
        summary, broker, _, claim = self.run_bot(dry=True)
        self.assertEqual(summary["status"], "dry_run")
        self.assertEqual(summary["orders"], [])
        self.assertTrue(summary["planned_orders"])
        self.assertFalse(broker.executed)
        claim.assert_not_called()

    def test_thursday_is_weekly_rebalance_when_friday_is_a_holiday(self):
        broker = FakeBroker(now=datetime(2026, 4, 2, 15, 45, tzinfo=NY))
        regular_calendar = broker.calendar
        broker.calendar = lambda start, end: [
            row for row in regular_calendar(start, end) if row.date != date(2026, 4, 3)
        ]
        summary, _, _, _ = self.run_bot(broker, dry=True)
        self.assertTrue(summary["weekly_rebalance"])
        self.assertEqual(summary["regime"], "RISK-ON")

    def test_existing_persistent_claim_skips_orders(self):
        claim = Mock(return_value=False)
        summary, broker, _, _ = self.run_bot(claim=claim)
        self.assertEqual(summary["status"], "skipped")
        self.assertIn("claim", summary["reason"])
        self.assertEqual(broker.executed, [])
        claim.assert_called_once_with("fake-account", date(2026, 10, 16), True)

    def test_existing_bot_order_skips_before_data_and_claim(self):
        broker = FakeBroker()
        broker.history = [NS(client_order_id="eod-v1-20261016-buy-SPY")]
        summary, broker, data, claim = self.run_bot(broker)
        self.assertEqual(summary["status"], "skipped")
        data.load.assert_not_called()
        claim.assert_not_called()
        self.assertEqual(broker.executed, [])

    def test_today_bought_exit_stays_in_portfolio_and_occupies_slot(self):
        broker = FakeBroker(now=datetime(2026, 10, 14, 15, 45, tzinfo=NY), holdings={
            "AAPL": position("AAPL", 6), "MSFT": position("MSFT", 6),
        }, cash=2800)
        broker.bought = {"AAPL"}
        values = indicators()
        values["AAPL"] = replace(values["AAPL"], rsi2=80)
        values["NVDA"] = replace(values["NVDA"], rsi2=1)
        summary, broker, _, _ = self.run_bot(broker, FakeData(values))
        self.assertEqual(summary["protected"][0]["symbol"], "AAPL")
        self.assertEqual(broker.executed, [])
        self.assertEqual(set(broker.holdings), {"AAPL", "MSFT"})

    def test_any_sell_failure_stops_all_buys(self):
        broker = FakeBroker(holdings={"GLD": position("GLD", 10)}, cash=3000)
        broker.fail_sell = True
        with self.assertRaisesRegex(SafetyError, "Unconfirmed"):
            self.run_bot(broker)
        self.assertFalse(any("execute_buy" in event for event in broker.trace))
        self.assertEqual(broker.executed, [])

    def test_all_sells_precede_buys_and_actual_positions_cash_are_refreshed(self):
        broker = FakeBroker(holdings={"GLD": position("GLD", 10), "SGOV": position("SGOV", 10)}, cash=2000)
        summary, broker, data, _ = self.run_bot(broker)
        self.assertEqual([o["side"] for o in broker.executed], ["sell", "sell", "buy", "buy"])
        final_sell = max(i for i, event in enumerate(broker.trace) if event.startswith("execute_sell"))
        first_buy = next(i for i, event in enumerate(broker.trace) if event.startswith("execute_buy"))
        between = broker.trace[final_sell+1:first_buy]
        self.assertGreaterEqual(between.count("positions"), 2)
        self.assertGreaterEqual(between.count("account"), 2)
        self.assertGreaterEqual(data.price_calls, 3)
        self.assertEqual(summary["status"], "completed")

    def test_buy_uses_actual_post_sale_cash_and_rechecks_before_submission(self):
        broker = FakeBroker(holdings={"GLD": position("GLD", 10)}, cash=3000)
        broker.cash_after_sell = 101
        broker.buy_account_cash = 51  # Drops again immediately before the buy.
        summary, broker, _, _ = self.run_bot(broker)
        buys = [o for o in broker.executed if o["side"] == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["amount"], D(50))
        self.assertEqual(summary["status"], "completed")

    def test_swing_slot_not_reused_when_broker_still_reports_exited_holding(self):
        broker = FakeBroker(now=datetime(2026, 10, 14, 15, 45, tzinfo=NY), holdings={"AAPL": position("AAPL", 6)}, cash=3400)
        broker.leave_position_on_sell = True
        values = indicators()
        values["AAPL"] = replace(values["AAPL"], rsi2=80)
        values["NVDA"] = replace(values["NVDA"], rsi2=1)
        with self.assertRaises(SafetyError):
            self.run_bot(broker, FakeData(values))
        self.assertFalse(any(o["side"] == "buy" for o in broker.executed))

    def test_core_sale_requires_position_reconciliation_before_any_buy(self):
        broker = FakeBroker(holdings={"GLD": position("GLD", 10)}, cash=3000)
        broker.leave_position_on_sell = True
        with self.assertRaises(SafetyError):
            self.run_bot(broker)
        self.assertFalse(any(o["side"] == "buy" for o in broker.executed))

    def test_actual_sale_symbol_cannot_be_reentered_even_if_signal_is_still_low_rsi(self):
        broker = FakeBroker(now=datetime(2026, 10, 14, 15, 45, tzinfo=NY), holdings={"AAPL": position("AAPL", 6)}, cash=3400)
        values = indicators()
        values["AAPL"] = replace(values["AAPL"], rsi2=1, sma5=99)
        values["MSFT"] = replace(values["MSFT"], rsi2=2)
        _, broker, _, _ = self.run_bot(broker, FakeData(values))
        self.assertEqual([(o["symbol"], o["side"]) for o in broker.executed], [("AAPL", "sell"), ("MSFT", "buy")])


if __name__ == "__main__":
    unittest.main()
