"""Acceptance scenarios for quote omissions, with no external I/O or orders.

Tests assert allocation and execution invariants independently of coordinator
helpers. The quote fixture returns the same public QuoteBatch as MarketData.
"""
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from unittest.mock import Mock
import unittest

from src.bot import Config, run
from src.quotes import QuoteBatch, QuoteFatalError, QuoteSnapshot, inspect_quote
from src.safety import NY, SafetyError
from tests.test_bot import FakeBroker, indicators, position


WEDNESDAY = datetime(2026, 10, 21, 15, 45, tzinfo=NY)
FRIDAY = datetime(2026, 10, 23, 15, 45, tzinfo=NY)


class GuardedBroker(FakeBroker):
    """Preserve the real execution API's local guard and anti-PDT barrier."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.before_submission = None

    def assert_time(self, window):
        super().assert_time(window)
        if not self.is_open or not window.accepts_order(self.now):
            raise SafetyError("Synthetic order deadline")
        return self.now

    def execute(self, *, symbol, side, amount, window, summary, pre_submit=None):
        if self.before_submission is not None:
            self.before_submission(self, symbol, side)
        self.assert_time(window)
        if side == "sell" and symbol in self.bought:
            raise SafetyError("ANTI_PDT: synthetic same-day fill before submission")
        if side == "buy" and symbol in self.sold:
            raise SafetyError("Synthetic same-day reentry")
        if pre_submit is not None:
            pre_submit(self.now)
        return super().execute(symbol=symbol, side=side, amount=amount,
                               window=window, summary=summary)


class QuoteData:
    """History has no price; invalid snapshots are never converted to a price."""
    def __init__(self, values=None, invalid=()):
        self.values = indicators() if values is None else values
        self.invalid = set(invalid)
        self.quote_requests = []
        self.budgets = []
        self.before_batch = None
        self.fatal = None
        self.quote_age = {}
        self.load = Mock(side_effect=self._history)

    def _history(self, symbols, window, broker, *, quote_symbols=None):
        if quote_symbols != ():
            raise AssertionError("Quotes must be selected after completed historical indicators")
        return {s: replace(self.values[s], price=None) for s in symbols}

    def prices(self, symbols, now):
        raise AssertionError("The coordinator must consume per-symbol QuoteBatch results")

    def quote_batch(self, symbols, now, *, budget, deadline):
        symbols = tuple(symbols)
        self.quote_requests.append(symbols)
        self.budgets.append(budget)
        if self.before_batch is not None:
            self.before_batch(self, symbols)
        if self.fatal is not None and symbols:
            raise self.fatal
        observed = now() if callable(now) else now
        result = QuoteBatch()
        for symbol in symbols:
            price = self.values[symbol].price
            age = 91 if symbol in self.invalid else self.quote_age.get(symbol, 0)
            stamp = observed-timedelta(seconds=age)
            snap = QuoteSnapshot(stamp, price-.005, price+.005, 100, 100)
            checked_price, cause, diagnostic = inspect_quote(snap, observed, feed="iex")
            diagnostic.update(attempts=4 if cause else 1, recovered=False,
                              checked_at=observed.isoformat())
            result.quotes[symbol] = snap
            result.diagnostics[symbol] = diagnostic
            if cause:
                result.failures[symbol] = cause
            else:
                result.prices[symbol] = checked_price
        return result


def candidates(*symbols):
    result = indicators()
    for rank, symbol in enumerate(symbols, 1):
        result[symbol] = replace(result[symbol], rsi2=float(rank))
    return result


class DegradedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.claim = Mock(return_value=True)
        self.observe = Mock(return_value=True)
        self.summary = {}

    def run_bot(self, broker, data, *, dry_run=False):
        run(Config("fake-key", "fake-secret", True, dry_run, "iex"),
            broker, data, self.summary, claim=self.claim, observe=self.observe)
        return self.summary

    @staticmethod
    def buys(broker):
        return [(row["symbol"], row["amount"]) for row in broker.executed if row["side"] == "buy"]

    def test_first_invalid_candidate_reserves_slot_and_does_not_promote_third(self):
        broker = GuardedBroker(now=WEDNESDAY)
        data = QuoteData(candidates("AAPL", "MSFT", "NVDA"), invalid={"AAPL"})
        self.run_bot(broker, data)
        self.assertEqual(self.buys(broker), [("MSFT", D("600"))])
        self.assertEqual(set(broker.holdings), {"MSFT"})
        self.assertEqual(broker.cash, D("3400"))
        self.assertEqual(self.summary["reserved_symbols"], ["AAPL"])
        self.assertEqual(self.summary["reserved_slots"], 1)
        self.assertEqual(D(self.summary["reserved_cash"]), D(600))
        self.claim.assert_called_once()

    def test_reserved_cash_is_not_spent_by_second_candidate_when_cash_is_low(self):
        broker = GuardedBroker(now=WEDNESDAY, holdings={"SPY": position("SPY", "33.5")}, cash=650)
        data = QuoteData(candidates("AAPL", "MSFT", "NVDA"), invalid={"AAPL"})
        self.run_bot(broker, data)
        buys = self.buys(broker)
        self.assertEqual([symbol for symbol, amount in buys], ["MSFT"])
        self.assertLessEqual(buys[0][1], D("49"))
        self.assertGreaterEqual(broker.cash, D("601"))

    def test_valid_below_sma_candidate_releases_slot_and_allows_next_rank(self):
        values = candidates("AAPL", "MSFT", "NVDA")
        values["AAPL"] = replace(values["AAPL"], price=80, sma200=90)
        broker = GuardedBroker(now=WEDNESDAY)
        self.run_bot(broker, QuoteData(values))
        self.assertEqual(self.buys(broker), [("MSFT", D("600")), ("NVDA", D("600"))])

    def test_unpriced_position_keeps_slot_allows_valid_exit_and_blocks_all_buys(self):
        values = candidates("META")
        values["MSFT"] = replace(values["MSFT"], rsi2=80)
        broker = GuardedBroker(now=WEDNESDAY,
                               holdings={"AAPL": position("AAPL", 6), "MSFT": position("MSFT", 6)}, cash=2800)
        data = QuoteData(values, invalid={"AAPL"})
        self.run_bot(broker, data)
        self.assertEqual([(o["symbol"], o["side"]) for o in broker.executed], [("MSFT", "sell")])
        self.assertEqual(set(broker.holdings), {"AAPL"})
        self.assertEqual(self.buys(broker), [])
        self.assertTrue(self.summary["buy_blocked"])

    def test_invalid_core_destination_freezes_group_but_preserves_swing_exit_and_entry(self):
        values = candidates("META")
        values["AAPL"] = replace(values["AAPL"], rsi2=80)
        broker = GuardedBroker(now=FRIDAY,
                               holdings={"SGOV": position("SGOV", 28), "AAPL": position("AAPL", 6)}, cash=600)
        data = QuoteData(values, invalid={"QQQ"})
        self.run_bot(broker, data)
        self.assertEqual([(o["symbol"], o["side"]) for o in broker.executed],
                         [("AAPL", "sell"), ("META", "buy")])
        self.assertEqual(broker.holdings["SGOV"].qty, D(28))
        self.assertEqual(self.buys(broker), [("META", D("600"))])
        self.assertEqual(self.summary["core_status"], "frozen")

    def test_core_failure_after_confirmed_sale_never_buys_remaining_core_destinations(self):
        broker = GuardedBroker(now=FRIDAY, holdings={"SGOV": position("SGOV", 28)}, cash=1200)
        data = QuoteData()

        def invalidate_after_sale(data, symbols):
            if broker.executed:
                data.invalid.add("QQQ")
        data.before_batch = invalidate_after_sale
        self.run_bot(broker, data)
        self.assertEqual([(o["symbol"], o["side"]) for o in broker.executed], [("SGOV", "sell")])
        self.assertEqual(broker.cash, D(4000))
        self.assertEqual(self.buys(broker), [])
        self.assertEqual(self.summary["core_status"], "incomplete")

    def test_first_candidate_quote_expiry_reserves_it_and_allows_independent_second(self):
        broker = GuardedBroker(now=WEDNESDAY)
        data = QuoteData(candidates("AAPL", "MSFT", "NVDA"))
        data.quote_age["AAPL"] = 89

        def expire_first(b, symbol, side):
            if symbol == "AAPL" and side == "buy":
                b.now += timedelta(seconds=2)
        broker.before_submission = expire_first
        self.run_bot(broker, data)
        self.assertEqual(self.buys(broker), [("MSFT", D("600"))])
        self.assertEqual(self.summary["reserved_symbols"], ["AAPL"])
        self.assertFalse(self.summary["buy_blocked"])

    def test_core_destination_expires_at_first_sell_but_independent_swing_can_continue(self):
        broker = GuardedBroker(now=FRIDAY, holdings={"SGOV": position("SGOV", 28)}, cash=1200)
        data = QuoteData(candidates("META"))
        data.quote_age["QQQ"] = 89

        def expire_destination(b, symbol, side):
            if symbol == "SGOV" and side == "sell":
                b.now += timedelta(seconds=2)
        broker.before_submission = expire_destination
        self.run_bot(broker, data)
        self.assertEqual([(o["symbol"], o["side"]) for o in broker.executed], [("META", "buy")])
        self.assertEqual(self.summary["core_status"], "frozen")
        self.assertFalse(self.summary["buy_blocked"])
        self.assertEqual(broker.holdings["SGOV"].qty, D(28))

    def test_reported_cash_reservation_is_capped_by_remaining_swing_budget(self):
        broker = GuardedBroker(now=WEDNESDAY, holdings={"AAPL": position("AAPL", 9)}, cash=3100)
        data = QuoteData(candidates("MSFT"), invalid={"MSFT"})
        self.run_bot(broker, data)
        self.assertEqual(broker.executed, [])
        self.assertEqual(self.summary["reserved_slots"], 1)
        self.assertEqual(D(self.summary["reserved_cash"]), D(300))

    def test_two_held_slots_without_exits_never_request_new_candidate_prices(self):
        broker = GuardedBroker(now=WEDNESDAY,
                               holdings={"AAPL": position("AAPL", 6), "MSFT": position("MSFT", 6)}, cash=2800)
        data = QuoteData(candidates("NVDA", "META"), invalid={"NVDA", "META"})
        self.run_bot(broker, data)
        self.assertEqual(broker.executed, [])
        for requested in data.quote_requests:
            self.assertTrue(set(requested).issubset({"AAPL", "MSFT"}))
        # A completed real evaluation consumes the daily permission even when
        # no order is needed, preserving the conservative same-day no-retry rule.
        self.claim.assert_called_once()

    def test_same_day_held_purchase_never_sells_or_releases_its_slot(self):
        values = candidates("META", "NVDA")
        values["AAPL"] = replace(values["AAPL"], rsi2=80)
        broker = GuardedBroker(now=WEDNESDAY, holdings={"AAPL": position("AAPL", 6)}, cash=3400)
        broker.bought.add("AAPL")
        self.run_bot(broker, QuoteData(values))
        self.assertEqual(self.buys(broker), [("META", D("600"))])
        self.assertIn("AAPL", broker.holdings)
        self.assertFalse(any(o["side"] == "sell" for o in broker.executed))

    def test_new_same_day_buy_before_submission_aborts_instead_of_degrading(self):
        values = candidates("META")
        values["AAPL"] = replace(values["AAPL"], rsi2=80)
        broker = GuardedBroker(now=WEDNESDAY, holdings={"AAPL": position("AAPL", 6)}, cash=3400)
        broker.before_submission = lambda b, symbol, side: b.bought.add(symbol) if side == "sell" else None
        with self.assertRaisesRegex(SafetyError, "ANTI_PDT"):
            self.run_bot(broker, QuoteData(values))
        self.assertEqual(broker.executed, [])
        self.assertEqual(self.buys(broker), [])

    def test_order_failure_is_global_even_when_another_candidate_quote_is_invalid(self):
        values = candidates("META", "NVDA")
        values["AAPL"] = replace(values["AAPL"], rsi2=80)
        broker = GuardedBroker(now=WEDNESDAY, holdings={"AAPL": position("AAPL", 6)}, cash=3400)
        broker.fail_sell = True
        with self.assertRaises(SafetyError):
            self.run_bot(broker, QuoteData(values, invalid={"META"}))
        self.assertEqual(self.buys(broker), [])

    def test_quote_auth_failure_after_exit_phase_is_global_without_order_retry(self):
        broker = GuardedBroker(now=WEDNESDAY)
        data = QuoteData(candidates("META"))
        data.fatal = QuoteFatalError("permission")
        with self.assertRaises(QuoteFatalError):
            self.run_bot(broker, data)
        self.assertEqual(broker.executed, [])
        # Candidate quotes are queried after the sell phase. The consumed
        # permission is retained on a later fatal provider error.
        self.claim.assert_called_once()

    def test_final_quote_failure_does_not_promote_unselected_candidate_after_a_buy(self):
        broker = GuardedBroker(now=WEDNESDAY)
        data = QuoteData(candidates("AAPL", "MSFT", "NVDA"))

        def expire_second(b, symbol, side):
            if symbol == "MSFT" and side == "buy":
                b.now += timedelta(seconds=91)
        broker.before_submission = expire_second
        self.run_bot(broker, data)
        self.assertEqual(self.buys(broker), [("AAPL", D("600"))])
        self.assertNotIn("NVDA", broker.holdings)

    def test_denied_daily_claim_prevents_all_submissions(self):
        self.claim.return_value = False
        broker = GuardedBroker(now=WEDNESDAY)
        self.run_bot(broker, QuoteData(candidates("META")))
        self.assertEqual(broker.executed, [])
        self.claim.assert_called_once()

    def test_dry_run_keeps_omission_policy_and_never_claims_or_executes(self):
        broker = GuardedBroker(now=WEDNESDAY)
        self.run_bot(broker, QuoteData(candidates("AAPL", "MSFT", "NVDA"), invalid={"AAPL"}), dry_run=True)
        self.assertEqual(broker.executed, [])
        self.claim.assert_not_called()
        self.observe.assert_not_called()
        buys = [o["symbol"] for o in self.summary["planned_orders"] if o["side"] == "buy"]
        self.assertEqual(buys, ["MSFT"])

    def test_oct21_irrelevant_sgov_and_wide_symbols_do_not_block_meta(self):
        broker = GuardedBroker(now=WEDNESDAY)
        data = QuoteData(candidates("META"), invalid={"SGOV", "LLY", "JPM", "BRK.B"})
        self.run_bot(broker, data)
        self.assertEqual(self.buys(broker), [("META", D("600"))])
        for requested in data.quote_requests:
            self.assertTrue(set(requested).issubset({"META"}))

    def test_all_quote_phases_share_one_recovery_budget(self):
        broker = GuardedBroker(now=WEDNESDAY)
        data = QuoteData(candidates("META", "NVDA"))
        self.run_bot(broker, data)
        self.assertGreaterEqual(len(data.budgets), 2)
        self.assertTrue(all(b is data.budgets[0] for b in data.budgets))
        self.assertEqual(len(self.buys(broker)), 2)


if __name__ == "__main__":
    unittest.main()
