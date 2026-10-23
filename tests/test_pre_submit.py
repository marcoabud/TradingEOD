"""Final local quote checks use the broker clock; no actual broker traffic."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import requests

from src.data import quote_price
from src.safety import SafetyError
from tests.test_broker import broker, fill, order, window


class QuoteSafetyError(SafetyError):
    """Stand-in for the execution coordinator's distinguishable quote failure."""


class PreSubmitTests(unittest.TestCase):
    def setUp(self):
        self.broker = broker()
        self.summary = {"orders": []}
        self.start = window().target

    def execute(self, callback=None, side="sell"):
        return self.broker.execute(
            symbol="AAPL", side=side, amount=Decimal("1.234567891") if side == "sell" else Decimal("600"),
            window=window(), summary=self.summary, sleeper=Mock(), pre_submit=callback,
        )

    def test_assert_time_returns_verified_broker_timestamp(self):
        self.assertEqual(self.broker.assert_time(window()), self.start)

    def test_quote_ages_during_antipdt_checks_and_blocks_before_order_record(self):
        self.broker.client.get_clock.side_effect = [
            SimpleNamespace(is_open=True, timestamp=self.start),
            SimpleNamespace(is_open=True, timestamp=self.start+timedelta(seconds=91)),
        ]
        quote = SimpleNamespace(timestamp=self.start, bid_price=100, ask_price=100.01,
                                bid_size=2, ask_size=2)
        callback = Mock(side_effect=lambda stamp: quote_price(quote, stamp, "AAPL"))
        with self.assertRaises(SafetyError):
            self.execute(callback)
        callback.assert_called_once_with(self.start+timedelta(seconds=91))
        self.broker.client.submit_order.assert_not_called()
        self.broker.client.get_order_by_client_id.assert_not_called()
        self.broker.client.cancel_order_by_id.assert_not_called()
        self.assertEqual(self.summary["orders"], [])
        self.broker.client.get.assert_called()  # Anti-PDT still ran before callback.

    def test_callback_follows_last_clock_and_immediately_precedes_post(self):
        timeline = Mock()
        timeline.attach_mock(self.broker.client.get_clock, "clock")
        timeline.attach_mock(self.broker.client.submit_order, "post")
        callback = Mock()
        timeline.attach_mock(callback, "validate")
        result = self.execute(callback)
        self.assertEqual(result.status, "filled")
        callback.assert_called_once_with(self.start)
        self.assertEqual([call[0] for call in timeline.mock_calls], ["clock", "clock", "validate", "post"])

    def test_controlled_quote_failure_preserves_subclass_and_blocks_core_group(self):
        failure = QuoteSafetyError("Core destination QQQ lost its verified quote")
        callback = Mock(side_effect=failure)
        with self.assertRaises(QuoteSafetyError) as raised:
            self.execute(callback)
        self.assertIs(raised.exception, failure)
        self.broker.client.submit_order.assert_not_called()
        self.assertEqual(self.summary["orders"], [])

    def test_unexpected_callback_failure_is_sanitized(self):
        callback = Mock(side_effect=ValueError("private-token https://secret.invalid"))
        with self.assertRaisesRegex(SafetyError, "Pre-submission validation failed") as raised:
            self.execute(callback)
        self.assertNotIn("private-token", str(raised.exception))
        self.broker.client.submit_order.assert_not_called()
        self.assertEqual(self.summary["orders"], [])

    def test_submission_timeout_does_not_repeat_callback_or_post(self):
        callback = Mock()
        self.broker.client.submit_order.side_effect = requests.Timeout("private-token")
        self.broker.client.get_order_by_client_id.return_value = order()
        self.assertEqual(self.execute(callback).status, "filled")
        callback.assert_called_once_with(self.start)
        self.broker.client.submit_order.assert_called_once()
        self.broker.client.get_order_by_client_id.assert_called_once()

    def test_antipdt_failure_precedes_callback_and_post(self):
        callback = Mock()
        self.broker.client.get.return_value = [fill()]
        with self.assertRaisesRegex(SafetyError, "ANTI_PDT"):
            self.execute(callback)
        callback.assert_not_called()
        self.broker.client.submit_order.assert_not_called()

    def test_buy_receives_same_final_timestamp_guard(self):
        self.broker.client.submit_order.return_value = order(side="buy")
        callback = Mock(side_effect=QuoteSafetyError("Candidate quote expired"))
        with self.assertRaises(QuoteSafetyError):
            self.execute(callback, side="buy")
        callback.assert_called_once_with(self.start)
        self.broker.client.submit_order.assert_not_called()
        self.assertEqual(self.summary["orders"], [])


if __name__ == "__main__":
    unittest.main()
