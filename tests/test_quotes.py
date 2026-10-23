"""Deterministic quote recovery and transport tests; no credentials or network."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

import requests

from src.quotes import (LatestQuoteTransport, QuoteBudget, QuoteFatalError, QuoteSnapshot,
                        collect_quotes, inspect_quote, quote_snapshot)
from src.safety import NY, SafetyError


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.base = datetime(2026, 10, 22, 15, 45, tzinfo=NY)
        self.sleeps = []
        self.reads = 0

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.seconds += seconds

    def now(self):
        self.reads += 1
        return self.base + timedelta(seconds=self.seconds)

    def quote(self, *, age=0, **changes):
        result = {"t": (self.base + timedelta(seconds=self.seconds-age)).isoformat(),
                  "bp": 99.9, "ap": 100.1, "bs": 10, "as": 20}
        result.update(changes)
        return result

    def budget(self, **changes):
        return QuoteBudget(monotonic=self.monotonic, sleeper=self.sleep, **changes)


class QuoteSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()

    def test_rest_and_sdk_quotes_have_the_same_strict_validation(self):
        raw = self.clock.quote()
        sdk = NS(timestamp=raw["t"], bid_price=99.9, ask_price=100.1, bid_size=10, ask_size=20)
        for quote in (raw, sdk, quote_snapshot(raw)):
            with self.subTest(quote_type=type(quote)):
                price, cause, diagnostic = inspect_quote(quote, self.clock.now(), feed="iex")
                self.assertEqual(price, 100)
                self.assertIsNone(cause)
                self.assertEqual(diagnostic["checked_at"], "2026-10-22T19:45:00+00:00")
                self.assertEqual(diagnostic["timestamp"], diagnostic["checked_at"])
                self.assertAlmostEqual(diagnostic["spread"], .002)

    def test_snapshot_contains_only_allowlisted_numeric_fields_and_utc(self):
        raw = self.clock.quote(bp="secret-do-not-log", secret="private-token")
        snapshot = quote_snapshot(raw)
        self.assertIsInstance(snapshot, QuoteSnapshot)
        self.assertIsNone(snapshot.bid_price)
        self.assertEqual(snapshot.timestamp.tzinfo, timezone.utc)
        self.assertNotIn("secret", repr(snapshot))
        self.assertEqual(set(vars(snapshot)), {"timestamp", "bid_price", "ask_price", "bid_size", "ask_size"})

    def test_replay_needs_no_transport_and_uses_original_observation_time(self):
        quote = self.clock.quote(age=80)
        price, cause, _ = inspect_quote(quote, self.clock.now(), feed="sip")
        self.assertEqual(price, 100)
        self.assertIsNone(cause)
        price, cause, _ = inspect_quote(quote, self.clock.now()+timedelta(minutes=20), feed="sip")
        self.assertIsNone(price)
        self.assertEqual(cause, "stale")

    def test_exact_validation_boundaries_remain_unchanged(self):
        cases = [(self.clock.quote(age=90), None), (self.clock.quote(age=90.001), "stale"),
                 (self.clock.quote(age=-5), None), (self.clock.quote(age=-5.001), "future_timestamp"),
                 (self.clock.quote(bp=99.75, ap=100.25), None),
                 (self.clock.quote(bp=99.74, ap=100.26), "excessive_spread"),
                 (self.clock.quote(bs=0), "empty_size"), (None, "missing_quote")]
        for quote, expected in cases:
            with self.subTest(expected=expected):
                _, cause, _ = inspect_quote(quote, self.clock.now(), feed="iex")
                self.assertEqual(cause, expected)


class QuoteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.budget = self.clock.budget()
        self.deadline = self.clock.base + timedelta(minutes=10)

    def collect(self, transport, symbols=("SPY",), *, budget=None, deadline=None, now=None):
        return collect_quotes(symbols, now or self.clock.now, feed="iex", transport=transport,
                              budget=budget or self.budget, deadline=deadline or self.deadline)

    def test_first_success_needs_no_wait_and_exposes_revalidatable_snapshot(self):
        transport = Mock(return_value={"SPY": self.clock.quote()})
        batch = self.collect(transport)
        self.assertEqual(batch.prices, {"SPY": 100})
        self.assertEqual(batch.failures, {})
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(batch.diagnostics["SPY"]["attempts"], 1)
        self.assertFalse(batch.diagnostics["SPY"]["recovered"])
        self.assertEqual(batch.diagnostics["SPY"]["first"], batch.diagnostics["SPY"]["last"])
        price, cause, _ = inspect_quote(batch.quotes["SPY"], self.clock.now()+timedelta(seconds=91), feed="iex")
        self.assertIsNone(price)
        self.assertEqual(cause, "stale")

    def test_only_failed_symbol_is_retried_and_first_last_times_are_preserved(self):
        calls = []
        def transport(symbols, **kwargs):
            calls.append(tuple(symbols))
            if len(calls) == 1:
                return {"SPY": self.clock.quote(age=173), "QQQ": self.clock.quote()}
            return {"SPY": self.clock.quote()}
        batch = self.collect(transport, ("SPY", "QQQ"))
        self.assertEqual(calls, [("SPY", "QQQ"), ("SPY",)])
        self.assertEqual(batch.prices, {"SPY": 100, "QQQ": 100})
        diagnostic = batch.diagnostics["SPY"]
        self.assertTrue(diagnostic["recovered"])
        self.assertEqual(diagnostic["attempts"], 2)
        self.assertEqual(diagnostic["first"]["cause"], "stale")
        self.assertEqual(diagnostic["first"]["age_seconds"], 173)
        self.assertEqual(diagnostic["first"]["checked_at"], "2026-10-22T19:45:00+00:00")
        self.assertEqual(diagnostic["last"]["checked_at"], "2026-10-22T19:45:03+00:00")
        self.assertEqual(batch.diagnostics["QQQ"]["last"]["checked_at"], "2026-10-22T19:45:00+00:00")
        self.assertEqual(batch.diagnostics["QQQ"]["age_seconds"], 3)

    def test_four_attempts_finish_with_partial_failure_at_18_seconds(self):
        calls = []
        def transport(symbols, **kwargs):
            calls.append((tuple(symbols), self.clock.seconds))
            return {s: self.clock.quote(age=173 if s == "SGOV" else 0) for s in symbols}
        batch = self.collect(transport, ("SGOV", "SPY"))
        self.assertEqual([t for _, t in calls], [0, 3, 8, 18])
        self.assertEqual(batch.prices, {"SPY": 100})
        self.assertEqual(batch.failures, {"SGOV": "stale"})
        self.assertEqual(batch.diagnostics["SGOV"]["attempts"], 4)
        self.assertEqual(self.budget.used_seconds, 18)
        self.assertEqual(self.budget.remaining_seconds, 12)

    def test_previously_valid_quote_that_ages_during_wait_is_refetched(self):
        calls = []
        def transport(symbols, **kwargs):
            calls.append(tuple(symbols))
            if len(calls) == 1:
                return {"SPY": self.clock.quote(age=89), "QQQ": self.clock.quote(age=173)}
            return {s: self.clock.quote() for s in symbols}
        batch = self.collect(transport, ("SPY", "QQQ"))
        self.assertEqual(calls, [("SPY", "QQQ"), ("SPY", "QQQ")])
        self.assertEqual(batch.diagnostics["SPY"]["attempts"], 2)
        self.assertTrue(batch.diagnostics["SPY"]["recovered"])
        self.assertEqual(batch.diagnostics["SPY"]["age_seconds"], 0)

    def test_missing_or_invalid_individual_quotes_do_not_discard_good_symbols(self):
        transport = Mock(return_value={"SPY": self.clock.quote(), "QQQ": "private-value"})
        batch = self.collect(transport, ("SPY", "QQQ", "SGOV"), budget=self.clock.budget(max_attempts=1))
        self.assertEqual(batch.prices, {"SPY": 100})
        self.assertEqual(batch.failures, {"QQQ": "invalid_timestamp", "SGOV": "missing_quote"})
        self.assertNotIn("private-value", json.dumps(batch.diagnostics))

    def test_exhausted_shared_budget_does_not_call_transport_or_clock(self):
        budget = self.clock.budget(seconds=0)
        transport = Mock(side_effect=AssertionError("No HTTP permitted"))
        now = Mock(side_effect=AssertionError("No Clock permitted"))
        batch = self.collect(transport, ("SPY", "QQQ"), budget=budget, now=now)
        self.assertEqual(batch.failures, {"SPY": "budget_exhausted", "QQQ": "budget_exhausted"})
        transport.assert_not_called()
        now.assert_not_called()

    def test_active_time_shared_between_batches_but_idle_fill_time_is_not_charged(self):
        def transport(symbols, **kwargs):
            self.clock.seconds += 2
            return {s: self.clock.quote() for s in symbols}
        self.collect(transport)
        self.assertEqual(self.budget.used_seconds, 2)
        self.clock.seconds += 60  # Time outside quote collection, e.g. confirmation.
        self.collect(transport)
        self.assertEqual(self.budget.used_seconds, 4)

    def test_exhaustion_during_request_rejects_response_and_next_phase_has_no_io(self):
        def transport(symbols, **kwargs):
            self.assertEqual(kwargs["timeout_seconds"], 2)
            self.clock.seconds += 3  # Simulates a socket/DNS timeout overrun.
            return {"SPY": self.clock.quote()}
        budget = self.clock.budget(seconds=2)
        batch = self.collect(transport, budget=budget)
        self.assertEqual(batch.prices, {})
        self.assertEqual(batch.failures, {"SPY": "budget_exhausted"})
        forbidden = Mock(side_effect=AssertionError("Budget spent"))
        self.collect(forbidden, budget=budget, now=forbidden)
        forbidden.assert_not_called()

    def test_clock_time_is_charged_and_cannot_exceed_budget_then_publish_price(self):
        def now():
            self.clock.seconds += 2
            return self.clock.now()
        transport = Mock(return_value={"SPY": self.clock.quote()})
        batch = self.collect(transport, now=now, budget=self.clock.budget(seconds=3))
        self.assertEqual(batch.prices, {})
        self.assertEqual(batch.failures["SPY"], "budget_exhausted")

    def test_exhaustion_while_waiting_does_not_start_a_late_request(self):
        transport = Mock(return_value={"SPY": self.clock.quote(age=173)})
        batch = self.collect(transport, budget=self.clock.budget(seconds=2))
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(batch.failures["SPY"], "budget_exhausted")
        self.assertEqual(batch.diagnostics["SPY"]["last_cause"], "stale")

    def test_api_authentication_failure_is_immediate_and_globally_fatal(self):
        transport = Mock(side_effect=QuoteFatalError("permission"))
        with self.assertRaisesRegex(QuoteFatalError, "permission"):
            self.collect(transport)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_transport_failure_can_recover_but_exhaustion_is_globally_fatal(self):
        calls = []
        def transport(symbols, **kwargs):
            calls.append(symbols)
            if len(calls) == 1:
                raise requests.Timeout("private-url-token")
            return {"SPY": self.clock.quote()}
        batch = self.collect(transport)
        self.assertTrue(batch.diagnostics["SPY"]["recovered"])
        self.assertEqual(batch.diagnostics["SPY"]["first"]["cause"], "transport")
        self.assertNotIn("private-url-token", json.dumps(batch.diagnostics))
        failed = Mock(side_effect=requests.Timeout("private-url-token"))
        with self.assertRaisesRegex(QuoteFatalError, "transport_exhausted") as error:
            self.collect(failed)
        self.assertNotIn("private-url-token", str(error.exception))
        self.assertLessEqual(failed.call_count, 4)

    def test_malformed_global_payload_fails_without_retries(self):
        transport = Mock(return_value=[{"secret": "do-not-print"}])
        with self.assertRaisesRegex(QuoteFatalError, "malformed_response"):
            self.collect(transport)
        self.assertEqual(transport.call_count, 1)

    def test_deadline_is_checked_before_io_and_after_a_delayed_response(self):
        transport = Mock()
        with self.assertRaisesRegex(QuoteFatalError, "deadline_reached"):
            self.collect(transport, deadline=self.clock.base)
        transport.assert_not_called()
        def delayed(symbols, **kwargs):
            self.assertEqual(kwargs["timeout_seconds"], 2)
            self.clock.seconds += 3
            return {"SPY": self.clock.quote()}
        with self.assertRaisesRegex(QuoteFatalError, "deadline_reached"):
            self.collect(delayed, deadline=self.clock.base+timedelta(seconds=2))

    def test_naive_deadline_and_clock_regression_fail_closed(self):
        with self.assertRaises(SafetyError):
            self.collect(Mock(), deadline=self.deadline.replace(tzinfo=None))
        now = Mock(side_effect=[self.clock.base, self.clock.base-timedelta(seconds=1)])
        with self.assertRaisesRegex(QuoteFatalError, "clock_regressed"):
            self.collect(Mock(return_value={"SPY": self.clock.quote()}), now=now)

    def test_invalid_budget_and_symbols_fail_before_network(self):
        for kwargs in ({"seconds": 31}, {"seconds": -1}, {"seconds": float("nan")},
                       {"max_attempts": 5}, {"max_attempts": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SafetyError):
                self.clock.budget(**kwargs)
        for symbols in (("SPY", "SPY"), ("bad/secret",), (None,)):
            with self.subTest(symbols=symbols), self.assertRaises(SafetyError):
                self.collect(Mock(), symbols)

    def test_empty_scope_has_no_io_or_clock(self):
        forbidden = Mock(side_effect=AssertionError("No I/O needed"))
        batch = self.collect(forbidden, (), now=forbidden)
        self.assertEqual(batch.prices, {})
        self.assertEqual(batch.failures, {})
        forbidden.assert_not_called()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.session = Mock()
        self.transport = LatestQuoteTransport("test-key", "test-secret", session=self.session)

    def response(self, status=200, payload=None, headers=None):
        response = Mock(status_code=status, headers=headers or {})
        response.json.return_value = {"quotes": {"SPY": self.clock.quote()}} if payload is None else payload
        return response

    def collect(self):
        return collect_quotes(("SPY",), self.clock.now, feed="iex", transport=self.transport,
                              budget=self.clock.budget(), deadline=self.clock.base+timedelta(minutes=10))

    def test_fixed_https_get_feed_timeout_no_redirects_and_no_adapter_retries(self):
        response = self.response()
        self.session.get.return_value = response
        result = self.transport(("SPY",), feed="iex", timeout_seconds=4)
        self.assertIn("SPY", result)
        args, kwargs = self.session.get.call_args
        self.assertEqual(args, (LatestQuoteTransport.URL,))
        self.assertEqual(kwargs["params"], {"symbols": "SPY", "feed": "iex"})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"].total, 4)
        self.assertLessEqual(kwargs["timeout"].connect_timeout, 4)
        adapter = self.session.mount.call_args.args[1]
        self.assertEqual(adapter.max_retries.total, 0)
        response.close.assert_called_once()

    def test_auth_redirect_invalid_and_malformed_responses_are_safe_fatal_errors(self):
        for status, cause in ((401, "authentication"), (403, "permission"),
                              (302, "redirect_refused"), (400, "invalid_request")):
            with self.subTest(status=status):
                self.session.get.return_value = self.response(status, payload={"message": "secret"})
                with self.assertRaisesRegex(QuoteFatalError, cause) as error:
                    self.collect()
                self.assertNotIn("secret", str(error.exception))
        self.session.get.return_value = self.response(payload={"secret": "do-not-print"})
        with self.assertRaisesRegex(QuoteFatalError, "malformed_response"):
            self.collect()

    def test_retry_after_seconds_is_respected_before_recovery(self):
        self.session.get.side_effect = [self.response(429, headers={"Retry-After": "12"}), self.response()]
        batch = self.collect()
        self.assertEqual(self.clock.sleeps, [12])
        self.assertEqual(batch.prices, {"SPY": 100})
        self.assertEqual(batch.diagnostics["SPY"]["first"]["cause"], "rate_limit")

    def test_retry_after_http_date_is_respected(self):
        headers = {"Date": "Thu, 22 Oct 2026 19:45:00 GMT", "Retry-After": "Thu, 22 Oct 2026 19:45:10 GMT"}
        self.session.get.side_effect = [self.response(429, headers=headers), self.response()]
        self.collect()
        self.assertEqual(self.clock.sleeps, [10])

    def test_retry_after_outside_budget_stops_without_extra_request_or_sleep(self):
        self.session.get.return_value = self.response(429, headers={"Retry-After": "120"})
        with self.assertRaisesRegex(QuoteFatalError, "rate_limit_exhausted"):
            self.collect()
        self.assertEqual(self.session.get.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_server_failure_exhaustion_is_global_and_safe(self):
        self.session.get.return_value = self.response(503, payload={"private": "do-not-log"})
        with self.assertRaisesRegex(QuoteFatalError, "server_exhausted") as error:
            self.collect()
        self.assertEqual(self.session.get.call_count, 4)
        self.assertNotIn("do-not-log", str(error.exception))


if __name__ == "__main__":
    unittest.main()
