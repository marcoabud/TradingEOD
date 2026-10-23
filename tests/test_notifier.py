"""Notification network calls are mocked; these tests never send messages."""

import unittest
import json
from decimal import Decimal
from unittest.mock import Mock

import requests

from src.notifier import notify


def response(status=200, body=None):
    result = Mock()
    result.status_code = status
    result.json.return_value = {"ok": True} if body is None else body
    return result


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.post.return_value = response()
        self.sleeper = Mock()
        self.summary = {
            "status": "completed", "regime": "RISK-ON", "equity": Decimal("4000.12"),
            "pnl_day": Decimal("-12.50"),
            "orders": [{"symbol": "SPY", "side": "buy", "status": "filled", "qty": "1.2"}],
        }

    def send(self, environ):
        return notify(self.summary, environ=environ, session=self.session, sleeper=self.sleeper)

    def test_no_channels_is_noop(self):
        self.assertEqual(self.send({}), {})
        self.session.post.assert_not_called()

    def test_webhook_json_contains_summary_and_bounded_timeout(self):
        result = self.send({"WEBHOOK_URL": "https://example.com/hook"})
        self.assertEqual(result, {"webhook": True})
        kwargs = self.session.post.call_args.kwargs
        self.assertEqual(kwargs["json"]["equity"], "4000.12")
        self.assertEqual(kwargs["json"]["pnl_day"], "-12.50")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (3.05, 8.0))

    def test_allowlist_and_redaction(self):
        self.summary["ALPACA_API_KEY"] = "secret-key"
        self.summary["reason"] = "failed secret-key https://example.com/secret"
        self.summary["orders"][0]["raw_error"] = "private-data"
        self.send({"ALPACA_API_KEY": "secret-key", "WEBHOOK_URL": "https://example.com/secret"})
        payload = self.session.post.call_args.kwargs["json"]
        self.assertNotIn("ALPACA_API_KEY", payload)
        self.assertNotIn("raw_error", payload["orders"][0])
        self.assertEqual(payload["reason"], "failed [REDACTED] [REDACTED]")

    def test_transient_retries_are_bounded(self):
        self.session.post.side_effect = [response(503), requests.Timeout("secret-token"), response()]
        with self.assertLogs("src.notifier", level="WARNING") as logs:
            self.assertEqual(self.send({"WEBHOOK_URL": "https://example.com/hook"}), {"webhook": True})
        self.assertEqual(self.session.post.call_count, 3)
        self.assertEqual(self.sleeper.call_count, 2)
        self.assertNotIn("secret-token", " ".join(logs.output))

    def test_auth_failure_not_retried(self):
        self.session.post.return_value = response(401)
        with self.assertLogs("src.notifier", level="WARNING"):
            self.assertEqual(self.send({"WEBHOOK_URL": "https://example.com/hook"}), {"webhook": False})
        self.assertEqual(self.session.post.call_count, 1)

    def test_redirect_not_followed_or_retried(self):
        self.session.post.return_value = response(302)
        with self.assertLogs("src.notifier", level="WARNING"):
            self.assertFalse(self.send({"WEBHOOK_URL": "https://example.com/hook"})["webhook"])
        self.assertEqual(self.session.post.call_count, 1)

    def test_invalid_urls_are_rejected(self):
        for url in ("http://example.com/hook", "https://user:password@example.com", "not-a-url"):
            with self.subTest(url=url), self.assertLogs("src.notifier", level="WARNING"):
                self.assertFalse(self.send({"WEBHOOK_URL": url})["webhook"])
        self.session.post.assert_not_called()

    def test_telegram_checks_api_result(self):
        self.session.post.return_value = response(body={"ok": False, "description": "secret-token"})
        with self.assertLogs("src.notifier", level="WARNING") as logs:
            result = self.send({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "456"})
        self.assertEqual(result, {"telegram": False})
        self.assertNotIn("secret-token", " ".join(logs.output))

    def test_telegram_summary_contains_required_fields(self):
        self.assertEqual(self.send({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "456"}), {"telegram": True})
        payload = self.session.post.call_args.kwargs["json"]
        for expected in ("4000.12", "-12.50", "RISK-ON", "SPY", "filled"):
            self.assertIn(expected, payload["text"])
        self.assertEqual(payload["chat_id"], "456")

    def test_all_retries_fail_without_raising_or_logging_secrets(self):
        self.session.post.side_effect = requests.ConnectionError("https://example.com/private-token")
        with self.assertLogs("src.notifier", level="WARNING") as logs:
            self.assertFalse(self.send({"WEBHOOK_URL": "https://example.com/private-token"})["webhook"])
        self.assertEqual(self.session.post.call_count, 3)
        self.assertNotIn("private-token", " ".join(logs.output))

    def test_degradation_fields_and_issues_are_allowlisted_and_redacted(self):
        secret = "123:private-telegram-token"
        self.summary.update(
            status="degraded", reserved_cash="600.00", reserved_slots=1,
            core_status="held " + secret, buy_blocked=True,
            evaluation_outcome="data_limited " + secret,
            data_issues=[{
                "symbol": "JPM", "phase": "entry", "reason": "stale " + secret,
                "attempts": 3, "response": "private response", "url": "private URL",
            }],
            raw_quotes={"JPM": "private quote"},
        )
        self.send({"WEBHOOK_URL": "https://example.com/hook", "TELEGRAM_TRADINGEOD": " " + secret + " "})
        payload = self.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["reserved_cash"], "600.00")
        self.assertEqual(payload["reserved_slots"], 1)
        self.assertIs(payload["buy_blocked"], True)
        self.assertEqual(payload["data_issues"], [{
            "symbol": "JPM", "phase": "entry", "reason": "stale [REDACTED]", "attempts": 3,
        }])
        serialized = json.dumps(payload)
        for private in (secret, "private response", "private URL", "private quote"):
            self.assertNotIn(private, serialized)
        self.assertIn("[REDACTED]", payload["core_status"])
        self.assertIn("[REDACTED]", payload["evaluation_outcome"])

    def test_nested_untrusted_issue_data_is_not_serialized(self):
        self.summary["data_issues"] = ["not an issue", {
            "symbol": "JPM", "phase": {"raw": "private-data"},
            "reason": RuntimeError("private-data"), "attempts": 10**100,
        }]
        self.send({"WEBHOOK_URL": "https://example.com/hook"})
        payload = self.session.post.call_args.kwargs["json"]
        self.assertEqual(payload["data_issues"], [{
            "symbol": "JPM", "phase": None, "reason": None, "attempts": None,
        }])
        self.assertNotIn("private-data", json.dumps(payload))

    def test_issue_lists_and_strings_are_bounded(self):
        self.summary["data_issues"] = [{"symbol": "JPM", "reason": "x" * 1000}] * 100
        self.summary["orders"] *= 100
        self.send({"WEBHOOK_URL": "https://example.com/hook"})
        payload = self.session.post.call_args.kwargs["json"]
        self.assertEqual(len(payload["data_issues"]), 50)
        self.assertEqual(len(payload["orders"]), 50)
        self.assertEqual(len(payload["data_issues"][0]["reason"]), 500)

    def test_telegram_degradation_summary_is_readable_and_preserves_order_status(self):
        self.summary.update(
            status="degraded", reserved_cash="600.00", reserved_slots=1,
            buy_blocked=False, core_status="unchanged", evaluation_outcome="completed",
            data_issues=[{"symbol": "JPM", "phase": "entry", "reason": "stale", "attempts": 3}],
        )
        self.send({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "456"})
        text = self.session.post.call_args.kwargs["json"]["text"]
        for expected in ("Ejecución limitada por datos", "Evaluación completada", "Estado Core: unchanged",
                         "Efectivo reservado USD: 600.00", "Slots reservados: 1",
                         "Compras bloqueadas por datos: no", "JPM [entry]: stale (intentos: 3)", "filled"):
            self.assertIn(expected, text)

    def test_no_signals_has_distinct_evaluation_text(self):
        self.summary.update(status="completed", evaluation_outcome="no_signals", orders=[])
        self.send({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "456"})
        text = self.session.post.call_args.kwargs["json"]["text"]
        self.assertIn("Sin señales operables", text)
        self.assertIn("Sin órdenes ejecutadas", text)

    def test_order_failure_is_not_relabelled_as_degraded_when_data_issues_exist(self):
        self.summary.update(status="blocked", buy_blocked=True, data_issues=[{"symbol": "JPM", "reason": "stale"}])
        self.summary["orders"][0]["status"] = "partially_filled"
        self.send({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "456"})
        text = self.session.post.call_args.kwargs["json"]["text"]
        self.assertIn("TradingEOD | Bloqueado", text)
        self.assertIn("partially_filled", text)
        self.assertNotIn("Ejecución limitada por datos", text)

    def test_telegram_issue_details_remain_within_message_limit(self):
        self.summary.update(orders=[], data_issues=[{
            "symbol": "JPM", "phase": "entry", "reason": "x" * 1000, "attempts": 3,
        }] * 100)
        self.send({"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "456"})
        text = self.session.post.call_args.kwargs["json"]["text"]
        self.assertLessEqual(len(text), 3900)
        self.assertEqual(text.count("JPM [entry]"), 8)
        self.assertIn("Otras 42 incidencias", text)


if __name__ == "__main__":
    unittest.main()
