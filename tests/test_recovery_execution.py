"""Cross-session execution tests with durable state and broker-shaped receipts."""
from dataclasses import replace
from datetime import date, datetime, timedelta
from functools import partial
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from src.bot import Config, run
from src.core_recovery import CoreRecoveryStore
from src.safety import NY, SafetyError
from src.state import claim_session
from tests.test_bot import FakeBroker, FakeData, indicators, position


FRIDAY = datetime(2026, 10, 30, 15, 45, tzinfo=NY)
MONDAY = datetime(2026, 11, 2, 15, 45, tzinfo=NY)


def data_for(day, risk_off=True):
    values = {s: replace(v, as_of=(day-timedelta(days=1)).date().isoformat())
              for s, v in indicators().items()}
    if risk_off:
        values["SPY"] = replace(values["SPY"], close=80)
    values["COST"] = replace(values["COST"], sma5=90)
    return FakeData(values)


class ReceiptBroker(FakeBroker):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.receipts, self.activities = [], []
        self.fail_cost = True

    def orders(self, day):
        return [r for r in self.receipts if r.submitted_at.date() == day]

    def fills_today(self, day):
        return [r for r in self.activities if r["transaction_time"].date() == day]

    def assert_time(self, window):
        if not window.accepts_order(self.now):
            raise SafetyError("Order deadline reached")
        return self.now

    def execute(self, **kwargs):
        if self.fail_cost and kwargs["symbol"] == "COST":
            raise SafetyError("Open orders exist; dedicated account must be quiescent")
        super().execute(**kwargs)
        symbol, side = kwargs["symbol"], kwargs["side"]
        qty = kwargs["amount"] if side == "sell" else kwargs["amount"]/100
        oid = f"receipt-{len(self.receipts)+1}"
        self.receipts.append(NS(id=oid, symbol=symbol, side=side, status="filled", qty=str(qty),
            client_order_id=f"eod-v1-{self.now:%Y%m%d}-{side[0]}-{symbol.replace('.', '')}",
            filled_qty=str(qty), filled_avg_price="100", submitted_at=self.now, filled_at=self.now))
        self.activities.append(dict(id=f"activity-{len(self.activities)+1}", order_id=oid,
                                    symbol=symbol, side=side, qty=str(qty), transaction_time=self.now))


class RecoveryExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.env = {"EOD_STATE_DIR": temp.name}
        self.store = CoreRecoveryStore("fake-account", True, environ=self.env)
        self.claim = partial(claim_session, environ=self.env)
        self.broker = ReceiptBroker(now=FRIDAY, cash=600, holdings={
            "SPY": position("SPY", 14), "QQQ": position("QQQ", 14), "COST": position("COST", 6)})

    def evaluate(self, data=None, dry=False):
        summary = {}
        run(Config("fake", "fake", True, dry), self.broker, data or data_for(self.broker.now),
            summary, claim=self.claim, observe=Mock(return_value=True), core_store=self.store)
        return summary

    def interrupt_friday(self):
        with self.assertRaisesRegex(SafetyError, "Open orders"):
            self.evaluate()
        pending = self.store.pending(FRIDAY.date())
        self.assertEqual(pending["initial_core_qty"]["SPY"], "14")
        self.assertEqual(pending["targets"], {"SGOV": "0.7"})
        self.assertEqual([o["symbol"] for o in self.broker.executed], ["SPY", "QQQ"])
        return pending

    def monday(self):
        self.broker.now = MONDAY
        self.broker.bought.clear()
        self.broker.sold.clear()
        self.broker.fail_cost = False

    def test_friday_partial_monday_reconciles_then_sells_before_recovery_buy(self):
        self.interrupt_friday()
        self.monday()
        summary = self.evaluate()
        self.assertFalse(summary["weekly_rebalance"])
        self.assertEqual(summary["core_recovery"]["status"], "completed")
        self.assertEqual(summary["core_status"], "completed")
        self.assertEqual([o["symbol"] for o in self.broker.executed], ["SPY", "QQQ", "COST", "SGOV"])
        self.assertEqual([o["side"] for o in self.broker.executed], ["sell", "sell", "sell", "buy"])
        self.assertIsNone(self.store.pending(MONDAY.date()))
        # A queued backup sees same-day order receipts and cannot submit twice.
        self.assertEqual(self.evaluate()["status"], "skipped")
        self.assertEqual(len(self.broker.executed), 4)

    def test_changed_signal_keeps_core_pending_without_new_offweek_allocation(self):
        self.interrupt_friday()
        self.monday()
        summary = self.evaluate(data_for(MONDAY, risk_off=False))
        self.assertEqual(summary["core_recovery"]["status"], "held_signal_changed")
        self.assertEqual(summary["core_status"], "held_signal_changed")
        self.assertEqual(summary["status"], "degraded")
        self.assertTrue(summary["core_recovery"]["pending"])
        self.assertEqual([o["symbol"] for o in self.broker.executed], ["SPY", "QQQ", "COST"])
        self.assertIsNotNone(self.store.pending(MONDAY.date()))

    def delayed_recovery_buy(self):
        self.interrupt_friday()
        self.monday()
        execute = self.broker.execute
        def delayed(**kwargs):
            execute(**kwargs)
            if kwargs["symbol"] == "SGOV":
                self.broker.now += timedelta(seconds=91)
        self.broker.execute = delayed

    def test_delayed_fill_requires_new_completion_prices(self):
        self.delayed_recovery_buy()
        data = data_for(MONDAY)
        prices = data.prices
        def changed_prices(symbols, now):
            result = prices(symbols, now)
            if "SGOV" in self.broker.holdings:
                result["SGOV"] = 120
            return result
        data.prices = changed_prices
        summary = self.evaluate(data)
        self.assertEqual(summary["evaluation_outcome"], "core_recovery_pending")
        self.assertTrue(summary["core_recovery"]["pending"])
        self.assertIsNotNone(self.store.pending(MONDAY.date()))
        checks = [c for c in summary["quote_checks"] if c["phase"] == "core_completion"]
        self.assertEqual(checks[-1]["checked_at"], self.broker.now.isoformat())

    def test_completion_quote_omission_retains_pending_intent(self):
        from src.quotes import QuoteBatch
        self.delayed_recovery_buy()
        data = data_for(MONDAY)
        quote_batch = data.quote_batch
        def missing(symbols, now, **kwargs):
            if "SGOV" in self.broker.holdings:
                return QuoteBatch(failures={s: "budget_exhausted" for s in symbols})
            return quote_batch(symbols, now, **kwargs)
        data.quote_batch = missing
        summary = self.evaluate(data)
        self.assertEqual(summary["core_status"], "incomplete")
        self.assertTrue(summary["core_recovery"]["pending"])
        self.assertIsNotNone(self.store.pending(MONDAY.date()))

    def test_completion_prices_expiring_during_account_refresh_do_not_complete(self):
        self.delayed_recovery_buy()
        data = data_for(MONDAY)
        quote_batch = data.quote_batch
        def expires(symbols, now, **kwargs):
            result = quote_batch(symbols, now, **kwargs)
            if "SGOV" in self.broker.holdings:
                self.broker.now += timedelta(seconds=91)
            return result
        data.quote_batch = expires
        summary = self.evaluate(data)
        self.assertTrue(summary["core_recovery"]["pending"])
        self.assertIsNotNone(self.store.pending(MONDAY.date()))
        self.assertTrue(any(i["phase"] == "core_completion_guard" for i in summary["data_issues"]))

    def test_mismatched_previous_fill_blocks_before_any_new_order(self):
        self.interrupt_friday()
        self.monday()
        self.broker.activities[0]["qty"] = "13"
        self.assert_exits_only("disagree")

    def test_unexpected_core_holding_blocks_recovery(self):
        self.interrupt_friday()
        self.monday()
        self.broker.holdings["GLD"] = position("GLD", 1)
        self.assert_exits_only("positions do not reconcile")

    def assert_exits_only(self, reason):
        """Uncertain core state blocks core orders and all buys, never a swing exit."""
        summary = self.evaluate()
        self.assertEqual(summary["core_recovery"]["status"], "blocked")
        self.assertRegex(summary["core_recovery"]["reason"], reason)
        self.assertEqual(summary["status"], "degraded")
        self.assertTrue(summary["exits_only"] and summary["buy_blocked"])
        self.assertEqual(summary["regime"], "CORE_BLOCKED")
        self.assertEqual([(o["symbol"], o["side"]) for o in self.broker.executed[2:]], [("COST", "sell")])
        self.assertIsNotNone(self.store.pending(MONDAY.date()))

    def test_after_window_never_reads_pending_intent_or_orders(self):
        self.interrupt_friday()
        self.monday()
        self.broker.now += timedelta(minutes=6)
        self.store.pending = Mock(side_effect=AssertionError("Late run read recovery state"))
        self.assertEqual(self.evaluate()["status"], "skipped")
        self.store.pending.assert_not_called()

    def test_dry_run_does_not_read_or_mutate_recovery_journal(self):
        self.interrupt_friday()
        self.monday()
        self.store.pending = Mock(side_effect=AssertionError("Dry run read recovery state"))
        self.assertEqual(self.evaluate(dry=True)["status"], "dry_run")
        self.store.pending.assert_not_called()
        self.assertEqual(len(self.broker.executed), 2)

    def test_same_day_intent_with_no_orders_is_not_replayed(self):
        decision = NS(regime="RISK-OFF", targets={"SGOV": .7})
        self.store.begin(FRIDAY.date(), decision, self.broker.holdings, 4000,
                         started_at=FRIDAY, signal_as_of="2026-10-29")
        summary = self.evaluate()
        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(self.broker.executed, [])

    def test_next_friday_can_supersede_changed_signal_after_reconciliation(self):
        pending = self.interrupt_friday()
        self.monday()
        self.broker.now = datetime(2026, 11, 6, 15, 45, tzinfo=NY)
        summary = self.evaluate(data_for(self.broker.now, risk_off=False))
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["core_targets"], {"SPY": .35, "QQQ": .35})
        self.assertEqual(summary["core_recovery"]["intent_date"], "2026-11-06")
        self.assertIsNone(self.store.pending(self.broker.now.date()))


if __name__ == "__main__":
    unittest.main()
