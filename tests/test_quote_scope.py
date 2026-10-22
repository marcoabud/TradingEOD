"""Quote necessity and evaluation-attempt integration, without network access."""

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from src.bot import Config, UNIVERSE, plan_buys, position_value, run
from src.safety import NY, SafetyError
from src.state import claim_missed_window, claim_session, record_evaluation_attempt
from src.strategy import CoreDecision, SWING_UNIVERSE, StrategyDataError
from tests.test_bot import FakeBroker, FakeData, indicators, position


WEDNESDAY = date(2026, 10, 21)
FRIDAY = date(2026, 10, 23)


def at_start(day):
    return datetime(day.year, day.month, day.day, 15, 45, tzinfo=NY)


class ScopedData(FakeData):
    """History deliberately has no quotes; fail only for requested bad symbols."""

    def __init__(self, values=None, invalid_quotes=()):
        super().__init__(values)
        self.history = {symbol: replace(value, price=None) for symbol, value in self.values.items()}
        self.invalid_quotes = set(invalid_quotes)
        self.quote_requests = []
        self.load = Mock(side_effect=self.historical_load)

    def historical_load(self, symbols, window, broker, *, quote_symbols=None):
        if quote_symbols != ():
            raise AssertionError("The engine must load history before selecting its quote scope")
        return {symbol: self.history[symbol] for symbol in symbols}

    def prices(self, symbols, now):
        symbols = tuple(symbols)
        self.quote_requests.append(symbols)
        self.price_calls += 1
        invalid = set(symbols) & self.invalid_quotes
        if invalid:
            raise SafetyError(f"{sorted(invalid)[0]}: synthetic invalid quote")
        return {symbol: self.values[symbol].price for symbol in symbols}


class ReconciledHoldingBroker(FakeBroker):
    """A position newly visible in the post-sale reconciliation needs a quote."""

    def __init__(self):
        super().__init__(now=at_start(WEDNESDAY))
        self.position_reads = 0

    def positions(self):
        self.position_reads += 1
        if self.position_reads == 2:
            self.holdings["SGOV"] = position("SGOV", 1)
            self.cash -= Decimal(100)
        return super().positions()


class QuoteScopeTests(unittest.TestCase):
    def run_bot(self, broker, data, *, dry_run=False, observe=None, claim=None):
        observe = Mock(return_value=True) if observe is None else observe
        claim = Mock(return_value=True) if claim is None else claim
        summary = {}
        run(Config("synthetic-key", "synthetic-secret", True, dry_run, "iex"),
            broker, data, summary, claim=claim, observe=observe)
        return summary, observe, claim

    def test_wednesday_without_sgov_holding_never_quotes_it_even_during_refresh(self):
        broker = FakeBroker(now=at_start(WEDNESDAY))
        data = ScopedData(invalid_quotes={"SGOV"})
        summary, _, claim = self.run_bot(broker, data)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["regime"], "HOLD_UNTIL_WEEK_END")
        self.assertEqual(summary["orders"], [])
        self.assertEqual(broker.executed, [])
        self.assertGreaterEqual(len(data.quote_requests), 2)
        for requested in data.quote_requests:
            self.assertEqual(set(requested), set())
        self.assertIsNone(summary["indicators"]["SGOV"]["price"])
        self.assertEqual(data.load.call_args.kwargs, {"quote_symbols": ()})
        self.assertEqual(set(data.load.call_args.args[0]), set(UNIVERSE))
        claim.assert_called_once()

    def test_irrelevant_wide_quotes_do_not_block_when_rsi_excludes_entries(self):
        broker = FakeBroker(now=at_start(WEDNESDAY))
        data = ScopedData(invalid_quotes={"SGOV", "LLY", "JPM", "BRK.B"})
        summary, _, _ = self.run_bot(broker, data)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["quoted_symbols"], [])
        self.assertEqual(summary["orders"], [])

    def test_oversold_candidate_requires_quote_even_when_not_held(self):
        values = indicators()
        values["JPM"] = replace(values["JPM"], rsi2=5)
        data = ScopedData(values, invalid_quotes={"JPM"})
        broker, claim = FakeBroker(now=at_start(WEDNESDAY)), Mock(return_value=True)
        with self.assertRaisesRegex(SafetyError, "JPM: synthetic invalid quote"):
            self.run_bot(broker, data, claim=claim)
        self.assertEqual(data.quote_requests, [("JPM",)])
        claim.assert_not_called()
        self.assertEqual(broker.executed, [])

    def test_wednesday_existing_sgov_holding_requires_valid_quote_before_claim(self):
        broker = FakeBroker(now=at_start(WEDNESDAY), holdings={"SGOV": position("SGOV", 1)})
        data = ScopedData(invalid_quotes={"SGOV"})
        observe, claim = Mock(return_value=True), Mock(return_value=True)
        with self.assertRaisesRegex(SafetyError, "SGOV: synthetic invalid quote"):
            self.run_bot(broker, data, observe=observe, claim=claim)
        self.assertIn("SGOV", data.quote_requests[0])
        observe.assert_called_once()
        claim.assert_not_called()
        self.assertEqual(broker.executed, [])

    def test_friday_risk_on_uses_sgov_history_without_quote_and_still_buys_core(self):
        broker = FakeBroker(now=at_start(FRIDAY))
        data = ScopedData(invalid_quotes={"SGOV"})
        summary, _, _ = self.run_bot(broker, data)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["regime"], "RISK-ON")
        self.assertEqual(summary["core_targets"], {"SPY": .35, "QQQ": .35})
        self.assertEqual([(order["side"], order["symbol"]) for order in broker.executed], [
            ("buy", "SPY"), ("buy", "QQQ"),
        ])
        self.assertEqual(sum(order["amount"] for order in broker.executed), Decimal("2800.00"))
        self.assertIsNone(data.history["SGOV"].price)
        self.assertIsNone(summary["indicators"]["SGOV"]["price"])
        self.assertEqual(summary["indicators"]["SGOV"]["r60"], data.values["SGOV"].r60)
        for requested in data.quote_requests:
            self.assertNotIn("SGOV", requested)
            self.assertEqual(set(requested), {"SPY", "QQQ"})
        self.assertGreaterEqual(len(data.quote_requests), 4)

    def test_friday_risk_off_requires_sgov_quote_and_blocks_before_orders_if_invalid(self):
        values = indicators()
        values["SPY"] = replace(values["SPY"], close=80)
        broker = FakeBroker(now=at_start(FRIDAY))
        data = ScopedData(values, invalid_quotes={"SGOV"})
        claim = Mock(return_value=True)
        with self.assertRaisesRegex(SafetyError, "SGOV: synthetic invalid quote"):
            self.run_bot(broker, data, claim=claim)
        self.assertEqual(set(data.quote_requests[0]), {"SGOV"})
        claim.assert_not_called()
        self.assertEqual(broker.executed, [])

    def test_friday_risk_off_valid_quote_allows_defensive_target(self):
        values = indicators()
        values["SPY"] = replace(values["SPY"], close=80)
        broker = FakeBroker(now=at_start(FRIDAY))
        data = ScopedData(values)
        summary, _, _ = self.run_bot(broker, data)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["regime"], "RISK-OFF")
        self.assertEqual([(order["symbol"], order["amount"]) for order in broker.executed], [
            ("SGOV", Decimal("2800.00")),
        ])
        self.assertTrue(all("SGOV" in requested for requested in data.quote_requests))
        self.assertIsNone(summary["indicators"]["SPY"]["price"])

    def test_newly_reconciled_holding_expands_quote_scope(self):
        broker, data = ReconciledHoldingBroker(), ScopedData()
        summary, _, _ = self.run_bot(broker, data)
        self.assertEqual(summary["status"], "completed")
        self.assertNotIn("SGOV", data.quote_requests[0])
        self.assertIn("SGOV", data.quote_requests[1])
        self.assertEqual(summary["quoted_symbols"], ["SGOV"])
        self.assertEqual(summary["indicators"]["SGOV"]["price"], data.values["SGOV"].price)
        self.assertEqual(broker.executed, [])

    def test_summary_removes_quote_after_position_is_fully_sold(self):
        values = indicators()
        values["AAPL"] = replace(values["AAPL"], rsi2=80)
        broker = FakeBroker(now=at_start(WEDNESDAY), holdings={"AAPL": position("AAPL", 1)})
        data = ScopedData(values)
        summary, _, _ = self.run_bot(broker, data)
        self.assertEqual(data.quote_requests, [("AAPL",), ()])
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["quoted_symbols"], [])
        self.assertIsNone(summary["indicators"]["AAPL"]["price"])
        self.assertEqual([(order["side"], order["symbol"]) for order in broker.executed], [("sell", "AAPL")])

    def test_summary_uses_latest_prices_from_each_pre_buy_refresh(self):
        class ChangingData(ScopedData):
            def prices(self, symbols, now):
                current = super().prices(symbols, now)
                return {symbol: price + self.price_calls for symbol, price in current.items()}

        broker, data = FakeBroker(now=at_start(FRIDAY)), ChangingData()
        summary, _, _ = self.run_bot(broker, data)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(len(broker.executed), 2)
        self.assertEqual(data.price_calls, 4)
        self.assertEqual(summary["quoted_symbols"], ["SPY", "QQQ"])
        for symbol in ("SPY", "QQQ"):
            self.assertEqual(summary["indicators"][symbol]["price"], data.values[symbol].price + 4)
        self.assertIsNone(summary["indicators"]["SGOV"]["price"])

    def test_invalid_quote_for_newly_reconciled_holding_blocks_subsequent_actions(self):
        broker = ReconciledHoldingBroker()
        data = ScopedData(invalid_quotes={"SGOV"})
        claim = Mock(return_value=True)
        with self.assertRaisesRegex(SafetyError, "SGOV: synthetic invalid quote"):
            self.run_bot(broker, data, claim=claim)
        self.assertNotIn("SGOV", data.quote_requests[0])
        self.assertIn("SGOV", data.quote_requests[1])
        claim.assert_called_once()  # The order barrier had been reached before reconciliation.
        self.assertEqual(broker.executed, [])

    def test_absent_quote_cannot_value_held_position_using_historical_close(self):
        values = indicators()
        values["SGOV"] = replace(values["SGOV"], price=None)
        with self.assertRaisesRegex(StrategyDataError, "SGOV: current quote required"):
            position_value(position("SGOV", 1), values)

    def test_absent_quote_cannot_create_unheld_core_buy_target(self):
        values = indicators()
        values["SPY"] = replace(values["SPY"], price=None)
        decision = CoreDecision("RISK-ON", {"SPY": .35, "QQQ": .35}, {}, ())
        with self.assertRaisesRegex(StrategyDataError, "SPY: current quote required"):
            plan_buys({}, values, Decimal(4000), Decimal(4000), decision, {"SPY", "QQQ"}, set())


class EvaluationAttemptIntegrationTests(unittest.TestCase):
    def test_quote_block_records_attempt_suppresses_false_missed_notice_and_leaves_claim_available(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            observe = Mock(side_effect=lambda account, day, paper: record_evaluation_attempt(
                account, day, paper, environ=env,
            ))
            claim = Mock(side_effect=lambda account, day, paper: claim_session(
                account, day, paper, environ=env,
            ))
            broker = FakeBroker(now=at_start(WEDNESDAY), holdings={"SGOV": position("SGOV", 1)})
            data = ScopedData(invalid_quotes={"SGOV"})
            summary = {}
            with self.assertRaisesRegex(SafetyError, "SGOV: synthetic invalid quote"):
                run(Config("synthetic-key", "synthetic-secret", True, False, "iex"),
                    broker, data, summary, claim=claim, observe=observe)
            self.assertTrue(summary["evaluation_recorded"])
            self.assertEqual(len(list((Path(directory) / "evaluations").rglob("*.json"))), 1)
            self.assertFalse((Path(directory) / "sessions").exists())
            self.assertFalse(claim_missed_window("fake-account", WEDNESDAY, True, environ=env))
            self.assertFalse((Path(directory) / "notifications").exists())
            self.assertTrue(claim_session("fake-account", WEDNESDAY, True, environ=env))
            claim.assert_not_called()
            self.assertEqual(broker.executed, [])

    def test_existing_attempt_false_does_not_skip_engine_or_consume_its_order_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            self.assertTrue(record_evaluation_attempt("fake-account", WEDNESDAY, True, environ=env))
            observed_results = []

            def observe(account, day, paper):
                result = record_evaluation_attempt(account, day, paper, environ=env)
                observed_results.append(result)
                return result

            claim = Mock(side_effect=lambda account, day, paper: claim_session(
                account, day, paper, environ=env,
            ))
            values = indicators()
            values["AAPL"] = replace(values["AAPL"], rsi2=1)
            broker, data, summary = FakeBroker(now=at_start(WEDNESDAY)), ScopedData(values), {}
            run(Config("synthetic-key", "synthetic-secret", True, False, "iex"),
                broker, data, summary, claim=claim, observe=observe)
            self.assertEqual(observed_results, [False])
            self.assertEqual(summary["status"], "completed")
            self.assertTrue(summary["evaluation_recorded"])
            self.assertTrue(summary["session_claimed"])
            self.assertEqual([(order["side"], order["symbol"]) for order in broker.executed], [("buy", "AAPL")])
            claim.assert_called_once()
            data.load.assert_called_once()

    def test_invalid_account_is_observed_before_validation_blocks_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            broker = FakeBroker(now=at_start(WEDNESDAY))
            account = broker.account()
            account.trading_blocked = True
            broker.account = Mock(return_value=account)
            observe = Mock(side_effect=lambda account, day, paper: record_evaluation_attempt(
                account, day, paper, environ=env,
            ))
            data, claim, summary = ScopedData(), Mock(), {}
            with self.assertRaisesRegex(SafetyError, "Account inactive or blocked"):
                run(Config("synthetic-key", "synthetic-secret", True, False, "iex"),
                    broker, data, summary, claim=claim, observe=observe)
            self.assertTrue(summary["evaluation_recorded"])
            self.assertFalse(claim_missed_window("fake-account", WEDNESDAY, True, environ=env))
            data.load.assert_not_called()
            claim.assert_not_called()
            self.assertEqual(broker.executed, [])

    def test_dry_run_does_not_record_attempt_or_claim_trading(self):
        observe = Mock(side_effect=AssertionError("Dry runs must not record an execution attempt"))
        claim = Mock(side_effect=AssertionError("Dry runs must not claim trading"))
        broker, data, summary = FakeBroker(now=at_start(WEDNESDAY)), ScopedData(), {}
        run(Config("synthetic-key", "synthetic-secret", True, True, "iex"),
            broker, data, summary, claim=claim, observe=observe)
        self.assertEqual(summary["status"], "dry_run")
        self.assertNotIn("evaluation_recorded", summary)
        observe.assert_not_called()
        claim.assert_not_called()
        self.assertEqual(broker.executed, [])


if __name__ == "__main__":
    unittest.main()
