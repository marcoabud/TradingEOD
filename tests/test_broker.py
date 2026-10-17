"""Fail-closed order and fill-ledger checks, with all broker traffic mocked."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from src.broker import Broker, TimeoutSession
from src.safety import NY, SafetyError, Window


DAY = date(2026, 10, 16)


def fill(index=1, **changes):
    result = {
        "id": f"activity-{index:05d}", "symbol": "AAPL", "side": "buy",
        "qty": "0.125", "transaction_time": "2026-10-16T19:45:00Z",
        # The submitted date is intentionally irrelevant: this could be old GTC.
        "order_id": "prior-session-gtc-order",
    }
    result.update(changes)
    return result


def order(**changes):
    values = dict(
        id="order-id", symbol="AAPL", side="sell", status="filled",
        filled_qty="1.234567891", filled_avg_price="100.00",
    )
    values.update(changes)
    values.setdefault("client_order_id", f"eod-v1-20261016-{values['side'][0]}-{values['symbol'].replace('.', '')}")
    return SimpleNamespace(**values)


def broker():
    # Do not construct a real TradingClient or read any credentials.
    result = Broker.__new__(Broker)
    result.client = Mock()
    result.client.get.return_value = []
    result.client.get_orders.return_value = []
    result.client.get_clock.return_value = SimpleNamespace(
        is_open=True, timestamp=datetime(2026, 10, 16, 15, 45, tzinfo=NY)
    )
    result.client.get_all_positions.return_value = [SimpleNamespace(
        symbol="AAPL", side="long", qty="1.234567891", market_value="123.45"
    )]
    result.client.submit_order.return_value = order()
    return result


def window():
    return Window(
        day=DAY,
        opens=datetime(2026, 10, 16, 9, 30, tzinfo=NY),
        closes=datetime(2026, 10, 16, 16, 0, tzinfo=NY),
        weekly_rebalance=True,
    )


class FillLedgerTests(unittest.TestCase):
    def setUp(self):
        self.broker = broker()

    def test_old_gtc_partial_fill_today_bans_sell(self):
        self.broker.client.get.return_value = [fill(qty="0.000000001")]
        self.broker.client.get_orders.return_value = []
        with self.assertRaisesRegex(SafetyError, "ANTI_PDT"):
            self.broker.assert_can_sell("AAPL", DAY)
        self.broker.client.submit_order.assert_not_called()

    def test_pagination_over_one_hundred_does_not_drop_later_buys(self):
        first = [fill(i, symbol="MSFT", side="sell") for i in range(100)]
        last = fill(100, symbol="AAPL", qty="0.01")
        self.broker.client.get.side_effect = [first, [last]]
        self.assertEqual(self.broker.traded_symbols(DAY, "buy"), {"AAPL"})
        calls = self.broker.client.get.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[0], "/account/activities/FILL")
        self.assertNotIn("page_token", calls[0].args[1])
        self.assertEqual(calls[1].args[1]["page_token"], first[-1]["id"])
        self.assertEqual(calls[0].args[1]["after"], "2026-10-16T03:59:59+00:00")
        self.assertEqual(calls[0].args[1]["until"], "2026-10-17T04:00:00+00:00")

    def test_exact_hundred_records_requires_empty_next_page(self):
        self.broker.client.get.side_effect = [[fill(i) for i in range(100)], []]
        self.assertEqual(len(self.broker.fills_today(DAY)), 100)
        self.assertEqual(self.broker.client.get.call_count, 2)

    def test_repeated_pagination_and_duplicate_activity_ids_fail_closed(self):
        first = [fill(i) for i in range(100)]
        for pages in ([first, [first[-1]]], [[fill(1), fill(1)]]):
            with self.subTest(pages=len(pages)):
                self.broker.client.get.side_effect = pages
                with self.assertRaisesRegex(SafetyError, "Ambiguous"):
                    self.broker.fills_today(DAY)

    def test_unbounded_pagination_is_rejected(self):
        self.broker.client.get.side_effect = [
            [fill(page * 100 + offset) for offset in range(100)]
            for page in range(100)
        ]
        with self.assertRaisesRegex(SafetyError, "pagination exhausted"):
            self.broker.fills_today(DAY)
        self.assertEqual(self.broker.client.get.call_count, 100)

    def test_malformed_fill_responses_fail_closed(self):
        broken = fill()
        del broken["symbol"]
        cases = (
            {"error": "server response"}, [None], [broken], [fill(side="unknown")],
            [fill(qty="0")], [fill(qty="-1")], [fill(qty="NaN")],
            [fill(transaction_time="2026-10-16T15:45:00")],
        )
        for case in cases:
            with self.subTest(case=case):
                self.broker.client.get.return_value = case
                with self.assertRaises(SafetyError):
                    self.broker.fills_today(DAY)

    def test_empty_or_non_string_ledger_identifiers_are_rejected(self):
        for changes in ({"symbol": ""}, {"symbol": None}, {"id": ""}, {"id": None}):
            with self.subTest(changes=changes):
                self.broker.client.get.return_value = [fill(**changes)]
                with self.assertRaises(SafetyError):
                    self.broker.fills_today(DAY)

    def test_ledger_api_failure_cannot_authorize_a_sell(self):
        self.broker.client.get.side_effect = requests.Timeout("private-token")
        with self.assertRaises(requests.Timeout):
            self.broker.assert_can_sell("AAPL", DAY)
        self.broker.client.submit_order.assert_not_called()

    def test_calendar_day_is_new_york_not_utc(self):
        self.broker.client.get.return_value = [
            fill(1, symbol="AAPL", transaction_time="2026-10-16T03:59:59Z"),
            fill(2, symbol="MSFT", transaction_time="2026-10-17T03:59:59Z"),
            fill(3, symbol="NVDA", transaction_time="2026-10-17T04:00:00Z"),
        ]
        self.assertEqual(self.broker.traded_symbols(DAY, "buy"), {"MSFT"})

    def test_fill_exactly_at_new_york_midnight_is_included(self):
        self.broker.client.get.return_value = [fill(transaction_time="2026-10-16T04:00:00Z")]
        self.assertEqual(self.broker.traded_symbols(DAY, "buy"), {"AAPL"})
        # Alpaca's exclusive after boundary must be before midnight itself.
        self.assertEqual(self.broker.client.get.call_args.args[1]["after"], "2026-10-16T03:59:59+00:00")

    def test_order_crosscheck_detects_partial_fill_when_activity_lags(self):
        self.broker.client.get_orders.return_value = [
            order(symbol="AAPL", side="buy", status="partially_filled", filled_qty="0.001"),
            order(symbol="MSFT", side="buy", status="canceled", filled_qty="0.2"),
            order(symbol="NVDA", side="buy", status="canceled", filled_qty="0"),
        ]
        self.assertEqual(self.broker.traded_symbols(DAY, "buy"), {"AAPL", "MSFT"})
        query = self.broker.client.get_orders.call_args.args[0]
        self.assertEqual(query.after, datetime(2026, 10, 16, 4, tzinfo=timezone.utc))
        self.assertEqual(query.until, datetime(2026, 10, 17, 4, tzinfo=timezone.utc))

    def test_contradictory_fully_filled_order_does_not_pass_anti_pdt_check(self):
        for bad_qty in ("", "0", "-1", None):
            with self.subTest(qty=bad_qty):
                self.broker.client.get_orders.return_value = [
                    order(side="buy", status="filled", filled_qty=bad_qty)
                ]
                with self.assertRaises(SafetyError):
                    self.broker.traded_symbols(DAY, "buy")

    def test_saturated_order_query_fails_closed(self):
        self.broker.client.get_orders.return_value = [order()] * 500
        with self.assertRaisesRegex(SafetyError, "saturated"):
            self.broker.orders(DAY)


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.broker = broker()
        self.summary = {"orders": []}
        self.sleep = Mock()

    def execute(self, side="sell", amount=Decimal("1.234567891")):
        return self.broker.execute(
            symbol="AAPL", side=side, amount=amount, window=window(),
            summary=self.summary, sleeper=self.sleep,
        )

    def test_full_fractional_sell_records_confirmed_fill(self):
        result = self.execute()
        self.assertEqual(result.status, "filled")
        submitted = self.broker.client.submit_order.call_args.kwargs["order_data"]
        self.assertAlmostEqual(submitted.qty, 1.234567891, places=9)
        self.assertEqual(submitted.client_order_id, "eod-v1-20261016-s-AAPL")
        self.assertEqual(submitted.time_in_force.value, "day")
        self.assertFalse(submitted.extended_hours)
        self.assertEqual(self.summary["orders"][0]["filled_qty"], "1.234567891")

    def test_buy_is_notional_and_id_is_deterministic(self):
        self.broker.client.submit_order.return_value = order(side="buy")
        self.execute(side="buy", amount=Decimal("600.01"))
        request = self.broker.client.submit_order.call_args.kwargs["order_data"]
        self.assertEqual(request.notional, 600.01)
        self.assertIsNone(request.qty)
        self.assertEqual(request.client_order_id, "eod-v1-20261016-b-AAPL")

    def test_sdk_uuid_identity_and_enum_fields_are_supported(self):
        from uuid import UUID
        from alpaca.trading.enums import OrderSide, OrderStatus

        expected_id = UUID("11111111-2222-3333-4444-555555555555")
        self.broker.client.submit_order.return_value = order(
            id=expected_id, side=OrderSide.SELL, status=OrderStatus.FILLED,
        )
        self.assertEqual(self.execute().id, expected_id)
        self.assertEqual(self.summary["orders"][0]["id"], str(expected_id))

    def test_same_day_bought_symbol_never_reaches_sell_post(self):
        self.broker.client.get.return_value = [fill()]
        with self.assertRaisesRegex(SafetyError, "ANTI_PDT"):
            self.execute()
        self.broker.client.submit_order.assert_not_called()
        self.assertEqual(self.summary["orders"], [])

    def test_same_day_sell_blocks_reentry(self):
        self.broker.client.get.return_value = [fill(side="sell")]
        with self.assertRaisesRegex(SafetyError, "reentry"):
            self.execute(side="buy", amount=Decimal("600"))
        self.broker.client.submit_order.assert_not_called()

    def test_any_open_order_blocks_new_submission(self):
        self.broker.client.get_orders.return_value = [order(symbol="MSFT", status="new")]
        with self.assertRaisesRegex(SafetyError, "Open orders"):
            self.execute()
        self.broker.client.submit_order.assert_not_called()

    def test_oversell_and_short_position_are_rejected(self):
        with self.assertRaisesRegex(SafetyError, "exceeds"):
            self.execute(amount=Decimal("1.234567892"))
        self.broker.client.get_all_positions.return_value[0].side = "short"
        with self.assertRaisesRegex(SafetyError, "Short/invalid"):
            self.execute()
        self.broker.client.submit_order.assert_not_called()

    def test_market_closed_or_order_deadline_blocks_submission(self):
        self.broker.client.get_clock.return_value.is_open = False
        with self.assertRaisesRegex(SafetyError, "market closed"):
            self.execute()
        self.broker.client.get_clock.return_value.is_open = True
        self.broker.client.get_clock.return_value.timestamp = window().closes - timedelta(minutes=5)
        with self.assertRaisesRegex(SafetyError, "deadline"):
            self.execute()
        self.broker.client.submit_order.assert_not_called()

    def test_submission_timeout_reconciles_without_a_second_post(self):
        self.broker.client.submit_order.side_effect = requests.Timeout("private-token")
        self.broker.client.get_order_by_client_id.return_value = order()
        self.assertEqual(self.execute().status, "filled")
        self.assertEqual(self.broker.client.submit_order.call_count, 1)
        self.broker.client.get_order_by_client_id.assert_called_once_with("eod-v1-20261016-s-AAPL")

    def test_uncertain_submission_stops_pipeline_without_retrying_post(self):
        self.broker.client.submit_order.side_effect = requests.Timeout("private-token")
        self.broker.client.get_order_by_client_id.side_effect = requests.ConnectionError("private-token")
        with self.assertRaisesRegex(SafetyError, "Submission ambiguous") as raised:
            self.execute()
        self.assertNotIn("private-token", str(raised.exception))
        self.assertEqual(self.broker.client.submit_order.call_count, 1)
        self.assertEqual(self.summary["orders"][0]["status"], "unknown_submission")

    def test_canceled_partial_never_counts_as_success(self):
        self.broker.client.submit_order.return_value = order(status="canceled", filled_qty="0.2")
        with self.assertRaisesRegex(SafetyError, "did not fully fill"):
            self.execute()
        self.assertEqual(self.summary["orders"][0]["filled_qty"], "0.2")
        self.broker.client.cancel_order_by_id.assert_not_called()

    def test_partial_timeout_cancels_only_its_order_and_aborts(self):
        self.broker.client.submit_order.return_value = order(status="partially_filled", filled_qty="0.2")
        self.broker.client.get_order_by_id.return_value = order(status="canceled", filled_qty="0.2")
        with patch("src.broker.time.monotonic", side_effect=[100, 191]):
            with self.assertRaisesRegex(SafetyError, "timed out"):
                self.execute()
        self.broker.client.cancel_order_by_id.assert_called_once_with("order-id")
        self.assertEqual(self.summary["orders"][0]["status"], "canceled")
        self.assertEqual(self.broker.client.submit_order.call_count, 1)

    def test_partial_eventually_filled_must_be_confirmed(self):
        self.broker.client.submit_order.return_value = order(status="partially_filled", filled_qty="0.2")
        self.broker.client.get_order_by_id.return_value = order()
        self.assertEqual(self.execute().status, "filled")
        self.broker.client.get_order_by_id.assert_called_once_with("order-id")
        self.sleep.assert_called_once_with(2)

    def test_failed_cancellation_is_reported_and_never_authorizes_continue(self):
        self.broker.client.submit_order.return_value = order(status="new", filled_qty="0")
        self.broker.client.cancel_order_by_id.side_effect = requests.Timeout("secret")
        with patch("src.broker.time.monotonic", side_effect=[100, 191]):
            with self.assertRaises(SafetyError):
                self.execute()
        self.assertEqual(self.summary["orders"][0]["status"], "cancel_unconfirmed")
        self.assertEqual(self.broker.client.submit_order.call_count, 1)

    def test_cancel_races_with_full_fill_but_original_abort_is_preserved(self):
        self.broker.client.submit_order.return_value = order(status="partially_filled", filled_qty="0.2")
        self.broker.client.get_order_by_id.return_value = order()
        with patch("src.broker.time.monotonic", side_effect=[100, 191]):
            with self.assertRaisesRegex(SafetyError, "timed out"):
                self.execute()
        self.assertEqual(self.summary["orders"][0]["status"], "filled")

    def test_initial_identity_mismatch_never_cancels_external_order(self):
        for changes in (
            {"symbol": "MSFT"}, {"side": "buy"}, {"client_order_id": "someone-elses-order"},
            {"id": None}, {"id": ""}, {"id": []},
        ):
            with self.subTest(changes=changes):
                self.setUp()
                self.broker.client.submit_order.return_value = order(status="new", filled_qty="0", **changes)
                with self.assertRaisesRegex(SafetyError, "identity mismatch"):
                    self.execute()
                self.broker.client.cancel_order_by_id.assert_not_called()
                self.assertEqual(self.summary["orders"][0]["status"], "invalid_response")

    def test_timeout_reconciliation_rejects_wrong_client_order_id_without_repost(self):
        self.broker.client.submit_order.side_effect = requests.Timeout("private-token")
        self.broker.client.get_order_by_client_id.return_value = order(client_order_id="external-order")
        with self.assertRaisesRegex(SafetyError, "identity mismatch"):
            self.execute()
        self.assertEqual(self.broker.client.submit_order.call_count, 1)
        self.broker.client.cancel_order_by_id.assert_not_called()

    def test_poll_mismatch_cancels_only_previously_verified_order_id(self):
        for changes in (
            {"id": "external-id"}, {"symbol": "MSFT"}, {"side": "buy"},
            {"client_order_id": "external-order"},
        ):
            with self.subTest(changes=changes):
                self.setUp()
                self.broker.client.submit_order.return_value = order(status="new", filled_qty="0")
                self.broker.client.get_order_by_id.side_effect = [
                    order(status="new", filled_qty="0", **changes),
                    order(status="canceled", filled_qty="0"),
                ]
                with self.assertRaisesRegex(SafetyError, "identity mismatch"):
                    self.execute()
                self.broker.client.cancel_order_by_id.assert_called_once_with("order-id")
                self.assertEqual(self.summary["orders"][0]["status"], "canceled")
                self.assertEqual(self.summary["orders"][0]["reason"], "ORDER_IDENTITY_MISMATCH")

    def test_mismatching_cancellation_response_remains_unconfirmed(self):
        self.broker.client.submit_order.return_value = order(status="new", filled_qty="0")
        self.broker.client.get_order_by_id.return_value = order(status="canceled", id="external-id", filled_qty="0")
        with patch("src.broker.time.monotonic", side_effect=[100, 191]):
            with self.assertRaisesRegex(SafetyError, "timed out"):
                self.execute()
        self.broker.client.cancel_order_by_id.assert_called_once_with("order-id")
        self.assertEqual(self.summary["orders"][0]["status"], "cancel_unconfirmed")

    def test_filled_price_must_be_finite_positive_and_present(self):
        for price in (None, "", "0", "-1", "NaN", "Infinity", "not-a-price"):
            with self.subTest(price=price):
                self.setUp()
                self.broker.client.submit_order.return_value = order(filled_avg_price=price)
                with self.assertRaises(SafetyError):
                    self.execute()
                self.assertEqual(self.summary["orders"][0]["status"], "invalid_response")
                self.broker.client.cancel_order_by_id.assert_not_called()

    def test_filled_sell_quantity_must_exactly_equal_requested_shares(self):
        for qty in ("1.2", "1.234567892", "0", "-1", "NaN", None):
            with self.subTest(qty=qty):
                self.setUp()
                self.broker.client.submit_order.return_value = order(filled_qty=qty)
                with self.assertRaises(SafetyError):
                    self.execute()
                self.assertEqual(self.summary["orders"][0]["status"], "invalid_response")

    def test_partial_malformed_price_cancels_known_order_but_does_not_continue(self):
        self.broker.client.submit_order.return_value = order(status="partially_filled", filled_qty="0.2", filled_avg_price=None)
        self.broker.client.get_order_by_id.return_value = order(status="canceled", filled_qty="0.2")
        with self.assertRaisesRegex(SafetyError, "malformed fill values"):
            self.execute()
        self.broker.client.cancel_order_by_id.assert_called_once_with("order-id")
        self.assertEqual(self.summary["orders"][0]["status"], "canceled")


class TransportTests(unittest.TestCase):
    def test_transport_has_timeout_and_refuses_redirects(self):
        with patch.object(requests.Session, "request", return_value=Mock()) as request:
            session = TimeoutSession()
            session.request("GET", "https://example.invalid/never-called")
        self.assertEqual(request.call_args.kwargs["timeout"], (5, 15))
        self.assertFalse(request.call_args.kwargs["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
