"""Notification network calls are mocked; these tests never send messages."""

import unittest
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


if __name__ == "__main__":
    unittest.main()
