"""Entrypoint routing and durable notification results; all services are mocked."""

from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import requests

from src import bot
from src.broker import Broker
from src.safety import NY, SafetyError
from src.state import StateError
from tests.test_bot import FakeBroker


REAL_RUN = bot.run


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.outcome = "ready"
        self.config = bot.Config("synthetic-key", "synthetic-secret", paper=True, dry_run=False, feed="iex")
        self.broker = Mock(spec_set=Broker)
        self.broker.account.return_value = SimpleNamespace(id="test-account")
        self.data = object()
        self.config_from_env = self.mock("src.bot.Config.from_env", return_value=self.config)
        self.broker_factory = self.mock("src.bot.Broker", return_value=self.broker)
        self.data_factory = self.mock("src.bot.MarketData", return_value=self.data)
        self.preflight = self.mock("src.bot.prepare_start", side_effect=self.prepare)
        self.engine = self.mock("src.bot.run", side_effect=self.evaluate)
        self.notice_claim = self.mock("src.bot.claim_missed_window", return_value=True)
        self.trading_claim = self.mock("src.bot.claim_session")
        self.notify = self.mock("src.bot.notify", return_value={"telegram": True})
        self.mock("src.bot.Path", side_effect=lambda path: self.root / path)
        self.mock("src.bot.logging.basicConfig")
        environment = patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "schedule",
            "EOD_SCHEDULED_CRON": "45 19 * * 1-5",
            "EOD_WAIT_FOR_WINDOW": "true",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def mock(self, name, **kwargs):
        patcher = patch(name, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def prepare(self, broker, summary, **kwargs):
        summary.update(
            status="skipped", session_date="2026-10-19",
            observed_at="2026-10-19T17:15:00-04:00",
            target_time="2026-10-19T15:45:00-04:00",
            start_deadline="2026-10-19T15:50:00-04:00",
            reason="Synthetic calendar preflight result",
        )
        return self.outcome

    def evaluate(self, config, broker, data, summary):
        summary.update(
            status="dry_run" if config.dry_run else "completed",
            equity="4000.00", pnl_day="0.00", reason="Synthetic evaluation complete",
        )

    def invoke(self):
        with self.assertLogs("trading_eod", level="INFO") as captured:
            code = bot.main()
        self.logs = "\n".join(captured.output)
        persisted = json.loads((self.root / "run_summary.json").read_text(encoding="utf-8"))
        return code, persisted

    def assert_no_engine_or_orders(self):
        self.data_factory.assert_not_called()
        self.engine.assert_not_called()
        self.broker.execute.assert_not_called()
        self.trading_claim.assert_not_called()

    def test_ready_preflight_constructs_data_then_calls_engine_once(self):
        code, summary = self.invoke()
        self.assertEqual(code, 0)
        self.preflight.assert_called_once()
        self.assertTrue(callable(self.preflight.call_args.kwargs["preflight"]))
        self.assertEqual({k:v for k,v in self.preflight.call_args.kwargs.items() if k != "preflight"}, {
            "event_name": "schedule", "scheduled_cron": "45 19 * * 1-5", "wait_for_window": True,
        })
        self.data_factory.assert_called_once_with("synthetic-key", "synthetic-secret", "iex")
        self.engine.assert_called_once()
        self.assertEqual(self.engine.call_args.args[:3], (self.config, self.broker, self.data))
        self.assertEqual(summary["status"], "completed")
        self.assertTrue(summary["prestart_approved"])
        self.assertEqual(summary["notification_delivery"], {"telegram": True})
        self.notice_claim.assert_not_called()
        self.notify.assert_called_once()

    def test_normal_skip_never_constructs_data_or_calls_account_engine_or_notify(self):
        self.outcome = "skipped"
        code, summary = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(self.broker.method_calls, [])
        self.assert_no_engine_or_orders()
        self.notice_claim.assert_not_called()
        self.notify.assert_not_called()
        self.assertNotIn("notification_delivery", summary)

    def test_missed_window_reads_only_account_reserves_alert_and_exits_nonzero(self):
        self.outcome = "missed_window"
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(self.broker.method_calls, [call.account()])
        self.notice_claim.assert_called_once_with("test-account", "2026-10-19", True)
        self.assert_no_engine_or_orders()
        self.assertEqual(summary["status"], "missed_window")
        self.assertEqual(summary["orders"], [])
        self.assertIn(summary["target_time"], summary["reason"])
        self.assertIn(summary["observed_at"], summary["reason"])
        self.assertIn("no envió órdenes", summary["reason"])
        self.notify.assert_called_once()

    def test_duplicate_or_already_traded_session_stays_quiet(self):
        self.outcome = "missed_window"
        self.notice_claim.return_value = False
        code, summary = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(self.broker.method_calls, [call.account()])
        self.assert_no_engine_or_orders()
        self.notify.assert_not_called()

    def test_four_late_attempts_issue_only_one_logical_notification(self):
        self.outcome = "missed_window"
        self.notice_claim.side_effect = [True, False, False, False]
        codes = [self.invoke()[0] for _ in range(4)]
        self.assertEqual(codes, [1, 0, 0, 0])
        self.assertEqual(self.notice_claim.call_count, 4)
        self.notify.assert_called_once()
        self.assert_no_engine_or_orders()

    def test_provider_failure_is_sanitized_persisted_and_never_retried(self):
        self.preflight.side_effect = requests.ConnectionError("private-token https://private.example")
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "error")
        self.assertIn("ConnectionError", summary["reason"])
        self.assertNotIn("private-token", json.dumps(summary) + self.logs)
        self.assertNotIn("https://private.example", json.dumps(summary) + self.logs)
        self.preflight.assert_called_once()
        self.assert_no_engine_or_orders()
        self.broker.account.assert_not_called()
        self.notice_claim.assert_not_called()
        self.notify.assert_called_once()

    def test_missed_window_account_failure_never_reserves_notice_or_trading(self):
        self.outcome = "missed_window"
        self.broker.account.side_effect = requests.Timeout("private-token")
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "error")
        self.assertNotIn("private-token", json.dumps(summary) + self.logs)
        self.broker.account.assert_called_once()
        self.notice_claim.assert_not_called()
        self.assert_no_engine_or_orders()

    def test_notice_persistence_failure_is_blocked_and_never_falls_back_to_trading(self):
        self.outcome = "missed_window"
        self.notice_claim.side_effect = StateError("Persistent state marker lookup failed (HTTP 503)")
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "blocked")
        self.notice_claim.assert_called_once()
        self.assert_no_engine_or_orders()
        self.notify.assert_called_once()

    def test_notification_false_preserves_result_and_exits_nonzero_without_engine_retry(self):
        def failed_delivery(payload):
            saved = json.loads((self.root / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "completed")
            self.assertNotIn("notification_delivery", saved)
            return {"telegram": False}

        self.notify.side_effect = failed_delivery
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["equity"], "4000.00")
        self.assertEqual(summary["notification_delivery"], {"telegram": False})
        self.engine.assert_called_once()
        self.notify.assert_called_once()
        self.notice_claim.assert_not_called()

    def test_unexpected_notification_exception_is_sanitized_and_persisted_without_retry(self):
        self.notify.side_effect = RuntimeError("private-token https://private.example")
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["notification_delivery"], {"internal_error": False})
        self.assertNotIn("private-token", json.dumps(summary) + self.logs)
        self.assertNotIn("https://private.example", json.dumps(summary) + self.logs)
        self.notify.assert_called_once()
        self.engine.assert_called_once()

    def test_engine_exception_preserves_existing_order_evidence_and_never_reenters(self):
        def interrupted_engine(config, broker, data, summary):
            summary["orders"].append({"symbol": "SPY", "side": "sell", "status": "unknown_submission"})
            raise RuntimeError("private-token")

        self.engine.side_effect = interrupted_engine
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["orders"], [{
            "symbol": "SPY", "side": "sell", "status": "unknown_submission",
        }])
        self.assertNotIn("private-token", json.dumps(summary) + self.logs)
        self.engine.assert_called_once()
        self.notify.assert_called_once()

    def test_unknown_preflight_result_fails_closed(self):
        self.outcome = "unexpected"
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "blocked")
        self.assert_no_engine_or_orders()
        self.notice_claim.assert_not_called()

    def test_missing_configuration_still_writes_blocked_summary_without_broker(self):
        self.config_from_env.side_effect = SafetyError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY")
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "blocked")
        self.broker_factory.assert_not_called()
        self.preflight.assert_not_called()
        self.assert_no_engine_or_orders()
        self.notify.assert_called_once()

    def test_ready_dry_run_is_notified_and_never_claims_missed_or_trading_session(self):
        self.config_from_env.return_value = replace(self.config, dry_run=True)
        code, summary = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "dry_run")
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["orders"], [])
        self.notice_claim.assert_not_called()
        self.trading_claim.assert_not_called()
        self.notify.assert_called_once()

    def test_late_dry_run_consumes_no_alert_marker_and_sends_no_notice(self):
        self.config_from_env.return_value = replace(self.config, dry_run=True)
        self.outcome = "missed_window"
        code, summary = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "skipped")
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["orders"], [])
        self.assertIn("Late simulation", summary["reason"])
        self.assertEqual(self.broker.method_calls, [])
        self.assert_no_engine_or_orders()
        self.notice_claim.assert_not_called()
        self.notify.assert_not_called()

    def test_expiry_between_preflight_and_engine_is_blocked_and_notified(self):
        expired = FakeBroker(now=datetime(2026, 10, 16, 15, 50, tzinfo=NY))
        self.broker_factory.return_value = expired
        self.engine.side_effect = REAL_RUN
        code, summary = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "blocked")
        self.assertTrue(summary["prestart_approved"])
        self.assertIn("expired after prestart approval", summary["reason"])
        self.assertEqual(expired.trace, ["clock", "calendar"])
        self.assertEqual(expired.executed, [])
        self.engine.assert_called_once()
        self.notice_claim.assert_not_called()
        self.trading_claim.assert_not_called()
        self.notify.assert_called_once()


class ApprovedStartRecheckTests(unittest.TestCase):
    def assert_recheck_blocks_before_account(self, broker, expected_reason):
        data, claim = Mock(), Mock()
        summary = {"prestart_approved": True}
        config = bot.Config("synthetic-key", "synthetic-secret", paper=True, dry_run=False, feed="iex")
        with self.assertRaisesRegex(SafetyError, expected_reason):
            REAL_RUN(config, broker, data, summary, claim=claim)
        self.assertEqual(broker.trace, ["clock", "calendar"])
        self.assertEqual(broker.account_calls, 0)
        self.assertEqual(broker.executed, [])
        self.assertEqual(summary["orders"], [])
        data.load.assert_not_called()
        claim.assert_not_called()

    def test_approved_start_expiring_at_exact_deadline_raises_before_account(self):
        broker = FakeBroker(now=datetime(2026, 10, 16, 15, 50, tzinfo=NY))
        self.assert_recheck_blocks_before_account(broker, "expired after prestart approval")

    def test_market_closing_after_approval_raises_before_account(self):
        broker = FakeBroker(now=datetime(2026, 10, 16, 15, 45, tzinfo=NY))
        broker.is_open = False
        self.assert_recheck_blocks_before_account(broker, "became unavailable after prestart approval")


if __name__ == "__main__":
    unittest.main()
