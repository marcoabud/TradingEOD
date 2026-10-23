"""Independent review regressions for safe, truthful degraded execution."""
import unittest
from datetime import timedelta
from unittest.mock import Mock

from src.bot import Config, run
from src.safety import SafetyError
from tests.test_bot import position
from tests.test_degraded_execution import FRIDAY, GuardedBroker, QuoteData


class ResilienceRegressions(unittest.TestCase):
    def test_partial_first_core_order_reports_incomplete_and_stops_buys(self):
        class PartialBroker(GuardedBroker):
            def execute(self, *, symbol, side, amount, window, summary, pre_submit=None):
                if pre_submit:
                    pre_submit(self.now)
                summary["orders"].append({"symbol": symbol, "side": side,
                                          "status": "canceled", "filled_qty": "1"})
                raise SafetyError("Synthetic partial fill canceled")

        broker = PartialBroker(now=FRIDAY, holdings={"SGOV": position("SGOV", 28)}, cash=1200)
        summary = {}
        with self.assertRaisesRegex(SafetyError, "partial fill"):
            run(Config("fake", "fake", True, False, "iex"), broker, QuoteData(), summary,
                claim=Mock(return_value=True), observe=Mock(return_value=True))
        self.assertEqual([o["side"] for o in summary["orders"]], ["sell"])
        self.assertEqual(summary["core_status"], "incomplete")

    def test_first_core_buy_unknown_or_rejected_reports_incomplete_and_stops(self):
        for order_status in ("unknown_submission", "rejected"):
            with self.subTest(order_status=order_status):
                class UncertainBroker(GuardedBroker):
                    def execute(self, *, symbol, side, amount, window, summary, pre_submit=None):
                        if pre_submit:
                            pre_submit(self.now)
                        summary["orders"].append({"symbol": symbol, "side": side,
                                                  "status": order_status, "filled_qty": "0"})
                        raise SafetyError("Synthetic unconfirmed buy")

                broker = UncertainBroker(now=FRIDAY)
                summary = {}
                with self.assertRaisesRegex(SafetyError, "unconfirmed buy"):
                    run(Config("fake", "fake", True, False, "iex"), broker, QuoteData(), summary,
                        claim=Mock(return_value=True), observe=Mock(return_value=True))
                self.assertEqual([o["side"] for o in summary["orders"]], ["buy"])
                self.assertEqual(summary["orders"][0]["status"], order_status)
                self.assertEqual(summary["core_status"], "incomplete")

    def test_first_core_buy_local_quote_expiry_is_frozen_without_order_attempt(self):
        broker = GuardedBroker(now=FRIDAY)

        def expire_quote(broker, symbol, side):
            broker.now += timedelta(seconds=91)

        broker.before_submission = expire_quote
        summary = {}
        run(Config("fake", "fake", True, False, "iex"), broker, QuoteData(), summary,
            claim=Mock(return_value=True), observe=Mock(return_value=True))
        self.assertEqual(summary["orders"], [])
        self.assertEqual(summary["core_status"], "frozen")


if __name__ == "__main__":
    unittest.main()
