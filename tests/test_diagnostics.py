"""Paper connectivity checks use a five-method read-only mock; no network calls."""

from datetime import date, datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import requests

from src.diagnostics import _paper_client, connection_check, main
from src.safety import NY


ENV = {
    "ALPACA_API_KEY": "fake-key-never-sent",
    "ALPACA_SECRET_KEY": "fake-secret-never-sent",
    "ALPACA_PAPER": "true",
    "TELEGRAM_BOT_TOKEN": "123:fake-token-never-sent",
    "TELEGRAM_CHAT_ID": "fake-chat-never-sent",
}
READ_METHODS = (
    "get_clock", "get_calendar", "get_account", "get_all_positions", "get_orders",
)


def read_only_client():
    client = Mock(spec_set=READ_METHODS)
    client.get_clock.return_value = SimpleNamespace(
        timestamp=datetime(2026, 10, 20, 17, 0, tzinfo=NY), is_open=False,
    )
    client.get_calendar.return_value = [SimpleNamespace(date=date(2026, 10, 20))]
    client.get_account.return_value = SimpleNamespace(
        id="private-account-id-never-output", account_number="private-number-never-output",
        equity="4001.25", last_equity="4000.00", cash="3500.00",
    )
    client.get_all_positions.return_value = [SimpleNamespace(symbol="SPY", qty="1")]
    client.get_orders.return_value = []
    return client


class ConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.client = read_only_client()
        self.factory = Mock(return_value=self.client)
        self.notifier = Mock(return_value={"telegram": True})

    def check(self, env=None):
        return connection_check(
            ENV if env is None else env,
            client_factory=self.factory, notifier=self.notifier,
        )

    def test_closed_market_still_checks_all_read_only_endpoints_and_telegram(self):
        summary, code = self.check()
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "connection_check")
        self.assertEqual(summary["mode"], "paper")
        self.assertTrue(summary["read_only"])
        self.assertTrue(summary["alpaca_connected"])
        self.assertTrue(summary["telegram_confirmed"])
        self.assertFalse(summary["market_open"])
        self.assertEqual(summary["equity"], "4001.25")
        self.assertEqual(summary["pnl_day"], "1.25")
        self.assertEqual(summary["positions_count"], 1)
        self.assertEqual(summary["open_orders_count"], 0)
        self.assertEqual(summary["orders"], [])
        self.assertEqual([entry[0] for entry in self.client.mock_calls], list(READ_METHODS))
        query = self.client.get_orders.call_args.args[0]
        self.assertEqual(query.status.value, "open")
        self.assertEqual(query.limit, 500)
        calendar = self.client.get_calendar.call_args.args[0]
        self.assertEqual(calendar.start, date(2026, 10, 20))
        self.assertEqual(calendar.end, date(2026, 10, 20))

    def test_output_does_not_include_account_identity_credentials_or_order_details(self):
        self.client.get_orders.return_value = [SimpleNamespace(id="private-order-id", symbol="MSFT")]
        summary, code = self.check()
        self.assertEqual(code, 0)
        rendered = json.dumps(summary)
        for private in (*ENV.values(), "private-account-id-never-output", "private-number-never-output", "private-order-id"):
            # The literal paper setting is public, not a credential.
            if private == "true":
                continue
            self.assertNotIn(private, rendered)
        self.assertEqual(summary["open_orders_count"], 1)
        self.assertNotIn("MSFT", rendered)

    def test_optional_webhook_is_never_used_by_diagnostic(self):
        self.check({**ENV, "WEBHOOK_URL": "https://example.invalid/private"})
        self.assertEqual(set(self.notifier.call_args.kwargs["environ"]), {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"})

    def test_false_missing_or_malformed_telegram_result_fails_connected_check(self):
        for result in ({"telegram": False}, {}, None, {"telegram": "true"}):
            with self.subTest(result=result):
                self.notifier.return_value = result
                summary, code = self.check()
                self.assertEqual(code, 1)
                self.assertTrue(summary["alpaca_connected"])
                self.assertFalse(summary["telegram_confirmed"])
                self.assertEqual(summary["failures"], ["telegram_delivery"])

    def test_missing_telegram_configuration_cannot_report_success(self):
        self.notifier.return_value = {}
        summary, code = self.check({key: value for key, value in ENV.items() if not key.startswith("TELEGRAM_")})
        self.assertEqual(code, 1)
        self.assertTrue(summary["alpaca_connected"])
        self.assertIn("telegram_delivery", summary["failures"])

    def test_unexpected_notification_exception_is_sanitized(self):
        self.notifier.side_effect = RuntimeError("private-token https://private.example")
        summary, code = self.check()
        self.assertEqual(code, 1)
        self.assertNotIn("private-token", json.dumps(summary))
        self.assertNotIn("https://", json.dumps(summary))

    def test_every_read_failure_fails_check_without_exposing_provider_error(self):
        for method in READ_METHODS:
            with self.subTest(method=method):
                self.client = read_only_client()
                self.factory.return_value = self.client
                getattr(self.client, method).side_effect = requests.Timeout("secret-token private-account-id https://private.example")
                summary, code = self.check()
                self.assertEqual(code, 1)
                self.assertFalse(summary["alpaca_connected"])
                self.assertIn("alpaca_read", summary["failures"])
                self.assertIn("Timeout", summary["reason"])
                rendered = json.dumps(summary)
                for private in ("secret-token", "private-account-id", "https://private.example"):
                    self.assertNotIn(private, rendered)
                methods = [entry[0] for entry in self.client.mock_calls]
                self.assertEqual(methods, list(READ_METHODS[:READ_METHODS.index(method)+1]))

    def test_missing_or_blank_credentials_never_construct_client(self):
        for key in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
            for value in (None, "", "  "):
                with self.subTest(key=key, value=value):
                    self.factory.reset_mock()
                    env = dict(ENV)
                    if value is None:
                        env.pop(key)
                    else:
                        env[key] = value
                    summary, code = self.check(env)
                    self.assertEqual(code, 1)
                    self.assertFalse(summary["alpaca_connected"])
                    self.factory.assert_not_called()

    def test_live_and_invalid_paper_configuration_are_rejected_before_api(self):
        for value in ("false", "FALSE", "0", "yes", "not-a-mode"):
            with self.subTest(value=value):
                self.factory.reset_mock()
                summary, code = self.check({**ENV, "ALPACA_PAPER": value})
                self.assertEqual(code, 1)
                self.assertEqual(summary["mode"], "paper")
                self.assertIn("configuration", summary["failures"])
                self.factory.assert_not_called()

    def test_missing_blank_and_true_paper_flags_are_safe_defaults(self):
        for value in (None, "", "  ", "true", " TRUE "):
            with self.subTest(value=value):
                env = dict(ENV)
                if value is None:
                    env.pop("ALPACA_PAPER")
                else:
                    env["ALPACA_PAPER"] = value
                summary, code = self.check(env)
                self.assertEqual(code, 0)
                self.assertEqual(summary["mode"], "paper")

    def test_factory_hardcodes_paper_endpoint(self):
        with patch("src.diagnostics.TradingClient") as constructor, patch("src.diagnostics.bound_client", side_effect=lambda client: client):
            self.assertIs(_paper_client("fake-key", "fake-secret"), constructor.return_value)
            constructor.assert_called_once_with("fake-key", "fake-secret", paper=True)

    def test_holiday_calendar_is_valid_read_only_connectivity(self):
        self.client.get_calendar.return_value = []
        summary, code = self.check()
        self.assertEqual(code, 0)
        self.assertEqual(summary["calendar_sessions"], 0)

    def test_open_order_count_reports_api_cap_without_claiming_total(self):
        self.client.get_orders.return_value = [SimpleNamespace()] * 500
        summary, code = self.check()
        self.assertEqual(code, 0)
        self.assertTrue(summary["open_orders_count_capped"])

    def test_main_saves_safe_report_even_when_telegram_fails(self):
        self.notifier.return_value = {"telegram": False}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "connectivity_summary.json"
            code = main(ENV, output_path=target, client_factory=self.factory, notifier=self.notifier)
            summary = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertTrue(summary["alpaca_connected"])
        self.assertFalse(summary["telegram_confirmed"])

    def test_main_saves_missing_configuration_failure_without_credentials_in_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "connectivity_summary.json"
            with self.assertLogs("trading_eod.diagnostics", level="INFO") as logs:
                code = main({}, output_path=target, client_factory=self.factory, notifier=self.notifier)
            report = target.read_text(encoding="utf-8")
        self.assertEqual(code, 1)
        self.factory.assert_not_called()
        for credential in (ENV["ALPACA_API_KEY"], ENV["ALPACA_SECRET_KEY"], ENV["TELEGRAM_BOT_TOKEN"]):
            self.assertNotIn(credential, report + " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
