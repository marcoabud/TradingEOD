"""Historical reconstruction never needs account or order access."""
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from src.signal_diagnostics import reconstruct_signals
from tests.test_bot import FakeBroker, indicators
from tests.test_quote_scope import WEDNESDAY, at_start


class SignalDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.env = {"ALPACA_PAPER": "true", "ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}
        self.report = {"status": "completed", "session_date": "2026-10-21",
                       "reconstruction_time": "2026-10-21T15:45:03-04:00", "historical_iex": {}}
        self.broker = FakeBroker(now=at_start(WEDNESDAY))
        self.broker_factory = Mock(return_value=self.broker)
        self.values = {s: replace(v, price=None) for s, v in indicators().items()}
        self.data = Mock()
        self.data.load.side_effect = lambda *a, **kw: dict(self.values)
        self.data_factory = Mock(return_value=self.data)

    def invoke(self):
        result = reconstruct_signals(self.report, self.env, broker_factory=self.broker_factory,
                                     data_factory=self.data_factory)
        self.assertEqual(self.broker.account_calls, 0)
        self.assertEqual(self.broker.executed, [])
        self.assertFalse(set(self.broker.trace) & {"positions", "orders", "traded_buy", "traded_sell"})
        self.data.prices.assert_not_called()
        self.data.quote_batch.assert_not_called()
        return result

    def quote(self, symbol, price=100):
        self.report["historical_iex"][symbol] = {
            "timestamp": self.report["reconstruction_time"],
            "bid_price": price-.1, "ask_price": price+.1, "bid_size": 10, "ask_size": 10,
        }

    def test_no_oversold_signals_need_no_quote_and_do_not_become_orders(self):
        result = self.invoke()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["required_quotes"], [])
        self.assertEqual(result["hypothetical_swing_entries"], [])
        self.assertEqual(result["hypothetical_core_targets"], {})
        self.assertEqual(self.data.load.call_args.kwargs, {"quote_symbols": ()})
        self.broker_factory.assert_called_once_with("test", "test", True)

    def test_qualified_candidate_uses_reconstructed_fresh_quote(self):
        self.values["AAPL"] = replace(self.values["AAPL"], rsi2=5)
        self.report["historical_iex"]["AAPL"] = {
            "timestamp": "2026-10-21T15:45:02-04:00", "bid_price": 99.9, "ask_price": 100.1,
            "bid_size": 10, "ask_size": 10,
        }
        result = self.invoke()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["required_quotes"], ["AAPL"])
        self.assertEqual(result["hypothetical_swing_entries"], ["AAPL"])

    def test_missing_candidate_quote_reserves_slot_without_using_daily_close(self):
        self.values["JPM"] = replace(self.values["JPM"], rsi2=5)
        result = self.invoke()
        self.assertEqual(result["status"], "degraded")
        self.assertIn("JPM", result["quote_failures"])
        self.assertEqual(result["hypothetical_swing_entries"], [])
        self.assertEqual(result["reserved_symbols"], ["JPM"])
        self.assertEqual(result["reserved_slots"], 1)
        self.assertEqual(result["reserved_equity_weight"], .15)

    def test_invalid_top_candidate_reserves_slot_and_keeps_independent_second(self):
        for rsi, symbol in enumerate(("AAPL", "MSFT", "NVDA"), 1):
            self.values[symbol] = replace(self.values[symbol], rsi2=rsi)
        self.quote("MSFT")
        self.quote("NVDA")
        result = self.invoke()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["hypothetical_swing_entries"], ["MSFT"])
        self.assertEqual(result["reserved_symbols"], ["AAPL"])
        self.assertEqual(result["required_quotes"], ["AAPL", "MSFT"])

    def test_confirmed_below_sma_candidate_releases_slot_for_next_rank(self):
        for rsi, symbol in enumerate(("AAPL", "MSFT", "NVDA"), 1):
            self.values[symbol] = replace(self.values[symbol], rsi2=rsi)
        self.quote("AAPL", 80)
        self.quote("MSFT")
        self.quote("NVDA")
        result = self.invoke()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["hypothetical_swing_entries"], ["MSFT", "NVDA"])
        self.assertEqual(result["reserved_slots"], 0)
        self.assertEqual(result["required_quotes"], ["AAPL", "MSFT", "NVDA"])

    def test_unused_quote_failure_does_not_degrade_hypothetical_plan(self):
        self.report["historical_iex"]["SGOV"] = {"http_status": 403}
        self.assertEqual(self.invoke()["status"], "completed")

    def test_core_quote_failure_freezes_core_but_keeps_independent_swing(self):
        self.report.update(session_date="2026-10-23", reconstruction_time="2026-10-23T15:45:03-04:00")
        self.values["META"] = replace(self.values["META"], rsi2=5)
        self.quote("SPY")
        self.quote("META")
        result = self.invoke()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["core_status"], "frozen")
        self.assertEqual(result["hypothetical_core_targets"], {})
        self.assertEqual(result["hypothetical_core_signal_targets"], {"SPY": .35, "QQQ": .35})
        self.assertEqual(result["hypothetical_swing_entries"], ["META"])
        self.assertEqual(result["reserved_slots"], 0)

    def test_candidate_failure_does_not_freeze_valid_core(self):
        self.report.update(session_date="2026-10-23", reconstruction_time="2026-10-23T15:45:03-04:00")
        self.values["META"] = replace(self.values["META"], rsi2=5)
        self.quote("SPY")
        self.quote("QQQ")
        result = self.invoke()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["core_status"], "ready")
        self.assertEqual(result["hypothetical_core_targets"], {"SPY": .35, "QQQ": .35})
        self.assertEqual(result["reserved_symbols"], ["META"])

    def test_required_provider_or_shape_failures_still_block_replay(self):
        self.values["META"] = replace(self.values["META"], rsi2=5)
        for quote in ({"http_status": 403}, {"error": "request_timeout"}, []):
            with self.subTest(quote=quote):
                self.report["historical_iex"]["META"] = quote
                result = self.invoke()
                self.assertEqual(result["status"], "blocked")
                self.assertNotIn("hypothetical_swing_entries", result)

    def test_live_setting_rejected_before_creating_clients(self):
        self.env["ALPACA_PAPER"] = "false"
        result = self.invoke()
        self.assertEqual(result["status"], "blocked")
        self.broker_factory.assert_not_called()

    def test_provider_error_is_sanitized(self):
        self.data.load.side_effect = RuntimeError("private token body")
        result = self.invoke()
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("private token", str(result))

    def test_wrong_new_york_session_date_blocks_before_creating_clients(self):
        for timestamp in ("2026-10-22T15:45:03-04:00", "2026-10-21T01:45:03+05:00"):
            with self.subTest(timestamp=timestamp):
                self.report["reconstruction_time"] = timestamp
                result = self.invoke()
                self.assertEqual(result["status"], "blocked")
                self.assertIn("New York session date", result["reason"])
                self.broker_factory.assert_not_called()
                self.data_factory.assert_not_called()
                self.data.load.assert_not_called()

    def test_session_date_uses_new_york_conversion_not_timestamp_printed_date(self):
        # October 22 in +05:00 is still October 21 at 15:45 in New York.
        self.report["reconstruction_time"] = "2026-10-22T00:45:03+05:00"
        result = self.invoke()
        self.assertEqual(result["status"], "completed")
        self.data.load.assert_called_once()

    def test_normal_start_window_includes_target_and_excludes_its_end(self):
        for timestamp in ("2026-10-21T15:45:00-04:00", "2026-10-21T15:49:59.999999-04:00"):
            with self.subTest(timestamp=timestamp):
                self.report["reconstruction_time"] = timestamp
                self.assertEqual(self.invoke()["status"], "completed")

    def test_outside_start_window_blocks_before_loading_history(self):
        for timestamp in ("2026-10-21T15:44:59.999999-04:00", "2026-10-21T15:50:00-04:00",
                          "2026-10-21T18:45:03-04:00"):
            with self.subTest(timestamp=timestamp):
                self.report["reconstruction_time"] = timestamp
                result = self.invoke()
                self.assertEqual(result["status"], "blocked")
                self.assertIn("outside calendar start window", result["reason"])
                self.data_factory.assert_not_called()
                self.data.load.assert_not_called()

    def test_early_close_uses_calendar_window_instead_of_normal_1545(self):
        self.report.update(session_date="2026-11-27", reconstruction_time="2026-11-27T12:45:03-05:00")
        self.broker.calendar = Mock(return_value=[SimpleNamespace(
            date=date(2026, 11, 27), open="09:30", close="13:00",
        )])
        self.report["historical_iex"] = {symbol: {
            "timestamp": "2026-11-27T12:45:02-05:00", "bid_price": 99.9, "ask_price": 100.1,
            "bid_size": 10, "ask_size": 10,
        } for symbol in ("SPY", "QQQ")}
        self.assertEqual(self.invoke()["status"], "completed")
        self.data_factory.reset_mock()
        self.data.load.reset_mock()
        self.report["reconstruction_time"] = "2026-11-27T15:45:03-05:00"
        result = self.invoke()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("outside calendar start window", result["reason"])
        self.data_factory.assert_not_called()
        self.data.load.assert_not_called()

    def test_standard_time_accepts_2045_utc_and_rejects_summer_hour(self):
        self.report.update(session_date="2026-11-03", reconstruction_time="2026-11-03T20:45:03Z")
        self.assertEqual(self.invoke()["status"], "completed")
        self.data_factory.reset_mock()
        self.data.load.reset_mock()
        self.report["reconstruction_time"] = "2026-11-03T19:45:03Z"
        self.assertEqual(self.invoke()["status"], "blocked")
        self.data_factory.assert_not_called()
        self.data.load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
