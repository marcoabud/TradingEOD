"""Read-only stale-open-list recovery for this process's own confirmed fills."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import requests

from src.broker import OPEN_ORDER_RECHECK_POLLS
from src.safety import SafetyError
from tests.test_broker import broker, order, window


class OpenOrderReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.broker = broker()
        self.summary = {"orders": []}
        self.elapsed = 100.0
        self.monotonic = lambda: self.elapsed
        self.sleep = Mock(side_effect=self.advance)
        self.broker.client.get_account.return_value = NS(
            status="ACTIVE", trading_blocked=False, account_blocked=False,
            trade_suspended_by_user=False, equity="4000", cash="2000", multiplier="4",
        )
        with patch("src.broker.time.monotonic", side_effect=self.monotonic):
            self.broker.execute(symbol="AAPL", side="sell", amount=Decimal("1.234567891"),
                                window=window(), summary=self.summary, sleeper=self.sleep)
        self.broker.client.get_all_positions.return_value = []
        self.broker.client.get_order_by_id.return_value = order()
        self.original_posts = self.broker.client.submit_order.call_count

    def advance(self, seconds):
        self.elapsed += seconds

    def check(self):
        self.broker.assert_no_open_orders(sleeper=self.sleep, monotonic=self.monotonic)

    def assert_no_mutations(self):
        self.assertEqual(self.broker.client.submit_order.call_count, self.original_posts)
        self.broker.client.cancel_order_by_id.assert_not_called()

    def test_own_confirmed_fill_waits_for_actual_empty_list_and_reconciles(self):
        stale = order(status="new", filled_qty="0", filled_avg_price=None,
                      created_at="2026-10-16T19:45:00Z", updated_at="2026-10-16T19:45:01Z")
        self.broker.client.get_orders.side_effect = [[stale], [order()], []]
        self.check()
        check = self.summary["open_order_checks"][-1]
        self.assertEqual(check["status"], "resolved")
        self.assertEqual(check["polls"], 2)
        self.assertEqual(check["snapshots"][-1]["orders"], [])
        self.assertEqual(check["cash"], "2000")
        first = check["snapshots"][0]["orders"][0]
        self.assertEqual(first["id"], "order-id")
        self.assertEqual(first["symbol"], "AAPL")
        self.assertEqual(first["status"], "new")
        self.assertEqual(first["updated_at"], "2026-10-16T19:45:01+00:00")
        self.assertEqual(self.broker.client.get_order_by_id.call_count, 2)
        self.broker.client.get_account.assert_called_once()
        self.assert_no_mutations()

    def test_persistent_own_open_row_never_gets_filtered_away(self):
        self.broker.client.get_orders.return_value = [order()]
        with self.assertRaisesRegex(SafetyError, "Open orders"):
            self.check()
        self.assertEqual(self.sleep.call_count, OPEN_ORDER_RECHECK_POLLS)
        self.assertEqual(self.summary["open_order_checks"][-1]["status"], "blocked")
        self.broker.client.get_account.assert_not_called()
        self.assert_no_mutations()

    def test_unknown_order_even_next_to_own_receipt_blocks_without_wait(self):
        self.broker.client.get_orders.return_value = [order(id="external-id", symbol="MSFT"), order()]
        with self.assertRaisesRegex(SafetyError, "Open orders"):
            self.check()
        self.sleep.assert_not_called()
        self.broker.client.get_order_by_id.assert_not_called()
        self.assert_no_mutations()

    def test_partial_canceling_replaced_and_malformed_rows_never_qualify(self):
        cases = [
            order(status="partially_filled", filled_qty="0.1"),
            order(status="pending_cancel"), order(status="replaced"),
            order(status="filled", filled_qty="0.1"),
            order(status="new", filled_qty="0.1"), order(status="new", filled_qty=None),
            order(legs=[order(id="external-child")]),
            order(client_order_id="external-id"), order(side="buy"),
        ]
        for item in cases:
            with self.subTest(item=vars(item)):
                self.broker.client.get_orders.return_value = [item]
                with self.assertRaisesRegex(SafetyError, "Open orders"):
                    self.check()
                self.sleep.assert_not_called()
                self.broker.client.get_order_by_id.assert_not_called()
        self.assert_no_mutations()

    def test_individual_order_must_still_match_the_exact_verified_full_fill(self):
        self.broker.client.get_orders.return_value = [order()]
        for change in ({"status": "new"}, {"status": "partially_filled"}, {"id": "foreign"},
                       {"symbol": "MSFT"}, {"side": "buy"}, {"client_order_id": "foreign"},
                       {"filled_qty": "1.1"}, {"filled_avg_price": "99"}):
            with self.subTest(change=change):
                self.broker.client.get_order_by_id.return_value = order(**change)
                with self.assertRaisesRegex(SafetyError, "Open orders"):
                    self.check()
                self.sleep.assert_not_called()
        self.assert_no_mutations()

    def test_old_receipt_or_backwards_clock_never_qualifies(self):
        self.broker.client.get_orders.return_value = [order()]
        for moment in (131.0, 99.0, float("nan")):
            with self.subTest(moment=moment):
                self.elapsed = moment
                with self.assertRaises(SafetyError):
                    self.check()
                self.sleep.assert_not_called()
        self.assert_no_mutations()

    def test_receipts_are_not_imported_from_prior_run_summary(self):
        self.broker.bind_context(window(), {"orders": list(self.summary["orders"])})
        self.broker.client.get_orders.return_value = [order()]
        with self.assertRaisesRegex(SafetyError, "Open orders"):
            self.check()
        self.sleep.assert_not_called()
        self.assert_no_mutations()

    def test_bound_context_records_foreign_order_metadata_before_first_trade(self):
        untraded = broker()
        summary = {"orders": []}
        untraded.bind_context(window(), summary)
        untraded.client.get_orders.return_value = [order(id="foreign", symbol="MSFT", status="new")]
        with self.assertRaisesRegex(SafetyError, "Open orders"):
            untraded.assert_no_open_orders()
        check = summary["open_order_checks"][0]
        self.assertEqual(check["reason"], "NO_RECENT_OWN_FILL")
        self.assertEqual(check["snapshots"][0]["orders"][0]["id"], "foreign")
        self.assertTrue(check["snapshots"][0]["observed_at"])
        untraded.client.submit_order.assert_not_called()

    def test_positions_must_reconcile_after_open_list_disappears(self):
        self.broker.client.get_orders.side_effect = [[order()], []]
        self.broker.client.get_all_positions.return_value = [NS(symbol="AAPL", side="long", qty="0.1")]
        with self.assertRaisesRegex(SafetyError, "Position has not reconciled"):
            self.check()
        self.assertEqual(self.summary["open_order_checks"][-1]["status"], "blocked")
        self.assert_no_mutations()

    def test_account_restrictions_and_invalid_cash_still_block(self):
        for change in ({"cash": "-1"}, {"cash": "NaN"}, {"trading_blocked": True}):
            with self.subTest(change=change):
                self.broker.client.get_orders.side_effect = [[order()], []]
                account = self.broker.client.get_account.return_value
                old = {key: getattr(account, key) for key in change}
                for key, val in change.items():
                    setattr(account, key, val)
                with self.assertRaises(SafetyError):
                    self.check()
                for key, val in old.items():
                    setattr(account, key, val)
        self.assert_no_mutations()

    def test_clock_deadline_during_recheck_blocks_after_fill(self):
        self.broker.client.get_orders.return_value = [order()]
        def closing_sleep(seconds):
            self.advance(seconds)
            self.broker.client.get_clock.return_value.timestamp = window().closes-timedelta(minutes=5)
        self.sleep.side_effect = closing_sleep
        with self.assertRaisesRegex(SafetyError, "deadline"):
            self.check()
        self.assert_no_mutations()

    def test_slow_read_cannot_continue_after_reconciliation_budget(self):
        self.broker.client.get_orders.return_value = [order()]
        def slow_read(order_id):
            self.elapsed += 7
            return order()
        self.broker.client.get_order_by_id.side_effect = slow_read
        with self.assertRaisesRegex(SafetyError, "reconciliation deadline"):
            self.check()
        self.sleep.assert_not_called()
        self.assert_no_mutations()

    def test_read_error_does_not_mutate_orders_or_leak_exception_into_diagnostics(self):
        self.broker.client.get_orders.return_value = [order()]
        self.broker.client.get_order_by_id.side_effect = requests.Timeout("private-api-token")
        with self.assertRaises(requests.Timeout):
            self.check()
        self.assertNotIn("private-api-token", str(self.summary))
        self.assert_no_mutations()

    def test_empty_list_does_not_add_reads_or_waits(self):
        self.broker.client.get_orders.return_value = []
        self.check()
        self.sleep.assert_not_called()
        self.broker.client.get_order_by_id.assert_not_called()
        self.broker.client.get_account.assert_not_called()
        self.assertNotIn("open_order_checks", self.summary)

    def test_foreign_order_appearing_during_poll_is_recorded_and_blocks(self):
        foreign = order(id="manual-order", symbol="MSFT", client_order_id="manual", status="new")
        self.broker.client.get_orders.side_effect = [[order()], [foreign]]
        with self.assertRaisesRegex(SafetyError, "Open orders"):
            self.check()
        check = self.summary["open_order_checks"][-1]
        self.assertEqual(check["status"], "blocked")
        self.assertEqual(check["snapshots"][-1]["orders"][0]["id"], "manual-order")
        self.assertEqual(self.sleep.call_count, 1)
        self.assert_no_mutations()

    def test_duplicate_open_ids_are_not_treated_as_two_valid_receipts(self):
        self.broker.client.get_orders.return_value = [order(), order()]
        with self.assertRaisesRegex(SafetyError, "Open orders"):
            self.check()
        self.sleep.assert_not_called()
        self.assert_no_mutations()

    def test_fresh_buy_receipt_requires_position_increase_before_continuing(self):
        bought = broker()
        summary = {"orders": []}
        bought.client.submit_order.return_value = order(side="buy")
        bought.client.get_order_by_id.return_value = order(side="buy")
        bought.client.get_account.return_value = self.broker.client.get_account.return_value
        with patch("src.broker.time.monotonic", side_effect=self.monotonic):
            bought.execute(symbol="AAPL", side="buy", amount=Decimal("123.46"),
                           window=window(), summary=summary, sleeper=self.sleep)
        bought.client.get_orders.side_effect = [[order(side="buy")], []]
        bought.client.get_all_positions.return_value = [NS(symbol="AAPL", side="long", qty="2.469135782")]
        bought.assert_no_open_orders(sleeper=self.sleep, monotonic=self.monotonic)
        self.assertEqual(summary["open_order_checks"][-1]["status"], "resolved")
        self.assertEqual(bought.client.submit_order.call_count, 1)
        bought.client.cancel_order_by_id.assert_not_called()


if __name__ == "__main__":
    unittest.main()
