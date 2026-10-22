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
        self.data.prices.assert_not_called()
        return result

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

    def test_missing_required_quote_blocks_without_falling_back_to_daily_close(self):
        self.values["JPM"] = replace(self.values["JPM"], rsi2=5)
        result = self.invoke()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("JPM", result["quote_failures"])
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
