"""Market diagnostics use GET-only mocks; credentials never leave the process."""
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import Mock, patch

import requests

from src.market_diagnostics import DATA, PAPER, SYMBOLS, TIMEOUT, market_check, main, quote_summary

ENV = {"ALPACA_API_KEY": "fake-key", "ALPACA_SECRET_KEY": "fake-secret", "ALPACA_PAPER": "true"}
TARGET = datetime(2026, 10, 21, 19, 45, tzinfo=timezone.utc)


def quote(**changes):
    result = {"t": "2026-10-21T19:44:50Z", "bp": 100, "ap": 100.01, "bs": 2, "as": 3}
    result.update(changes)
    return result


def response(data, status=200):
    return Mock(status_code=status, json=Mock(return_value=data))


class MarketDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.clock = {"timestamp": "2026-10-21T21:00:00Z", "is_open": False}
        self.calendar = [{"date": "2026-10-21", "open": "09:30", "close": "16:00"}]
        self.history = {"quotes": [quote()], "next_page_token": "never-follow-this-token"}
        self.session = Mock(spec_set=["get"])
        self.session.get.side_effect = self.get

    def get(self, url, **kwargs):
        if url == PAPER+"/clock":
            return response(self.clock)
        if url == PAPER+"/calendar":
            return response(self.calendar)
        if url.endswith("/quotes/latest"):
            return response({"message": "private-error-body-fake-secret"}, 403)
        return response(self.history)

    def check(self, env=None):
        return market_check(ENV if env is None else env, session=self.session)

    def test_all_17_symbols_get_one_latest_historical_quote_and_sip_denial_is_evidence(self):
        report, code = self.check()
        self.assertEqual(len(SYMBOLS), 17)
        self.assertEqual(set(report["historical_iex"]), set(SYMBOLS))
        self.assertEqual(report["status"], "completed")
        self.assertTrue(report["quote_checks_passed"])
        self.assertEqual(report["sip_latest"], {"http_status": 403})
        self.assertEqual(report["sip_historical"]["age_seconds"], 13)
        self.assertEqual(report["reconstruction_time"], "2026-10-21T15:45:03-04:00")
        self.assertEqual(code, 0)
        self.assertEqual(self.session.get.call_count, 21)
        calls = self.session.get.call_args_list
        for call in calls:
            self.assertEqual(call.kwargs["timeout"], TIMEOUT)
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertIn(call.args[0].split("/v2/")[0],
                          ("https://paper-api.alpaca.markets", "https://data.alpaca.markets"))
        for call in calls[2:-1]:
            params = call.kwargs["params"]
            self.assertEqual(params["limit"], 1)
            self.assertEqual(params["sort"], "desc")
            self.assertEqual(params["start"], "2026-10-21T13:30:00+00:00")
            self.assertEqual(params["end"], "2026-10-21T19:45:03+00:00")
            self.assertNotIn("page_token", params)
        rendered = json.dumps(report)
        for forbidden in ("fake-key", "fake-secret", "private-error-body", "never-follow-this-token"):
            self.assertNotIn(forbidden, rendered)

    def test_invalid_quote_reports_specific_reason_without_failing_evidence_collection(self):
        self.history = {"quotes": [quote(bp=0)]}
        report, code = self.check()
        self.assertFalse(report["quote_checks_passed"])
        self.assertIn("nonpositive_price", report["historical_iex"]["SGOV"]["validation_reasons"])
        self.assertEqual(code, 0)

    def test_empty_quote_and_bad_payload_are_distinguished(self):
        self.history = {"quotes": []}
        report, code = self.check()
        self.assertEqual(report["historical_iex"]["SGOV"]["validation_reasons"], ["missing_quote"])
        self.assertEqual(code, 0)
        self.history = {"quotes": None}
        report, code = self.check()
        self.assertEqual(report["historical_iex"]["SGOV"]["validation_reasons"], ["missing_quote"])
        self.assertEqual(code, 0)
        self.history = {"quotes": "private-body"}
        report, code = self.check()
        self.assertEqual(report["historical_iex"]["SGOV"]["error"], "invalid_quotes_shape")
        self.assertEqual(code, 1)

    def test_missing_credentials_and_nonpaper_rejected_before_get(self):
        for env in ({}, {**ENV, "ALPACA_API_KEY": " "}, {**ENV, "ALPACA_SECRET_KEY": ""},
                    {**ENV, "ALPACA_PAPER": "false"}, {**ENV, "ALPACA_PAPER": "bad"}):
            with self.subTest(env=env):
                report, code = self.check(env)
                self.assertEqual(code, 1)
                self.assertEqual(report["status"], "error")
        self.session.get.assert_not_called()

    def test_blank_paper_still_uses_fixed_paper_endpoint(self):
        report, code = self.check({**ENV, "ALPACA_PAPER": ""})
        self.assertEqual(code, 0)
        self.assertEqual(self.session.get.call_args_list[0].args[0], PAPER+"/clock")

    def test_holiday_and_before_target_do_not_request_market_data(self):
        self.calendar = []
        report, code = self.check()
        self.assertEqual(report["reason"], "no_market_session_today")
        self.assertEqual(code, 0)
        self.assertEqual(self.session.get.call_count, 2)
        self.setUp()
        self.clock["timestamp"] = "2026-10-21T19:30:00Z"
        report, code = self.check()
        self.assertEqual(report["reason"], "target_not_reached_yet")
        self.assertEqual(self.session.get.call_count, 2)

    def test_sip_history_waits_15_minutes_but_latest_entitlement_probe_remains(self):
        self.clock["timestamp"] = "2026-10-21T19:50:00Z"
        report, code = self.check()
        self.assertEqual(report["sip_historical"], {"skipped": "target_not_15_minutes_old"})
        self.assertEqual(report["sip_latest"], {"http_status": 403})
        self.assertEqual(self.session.get.call_count, 20)

    def test_early_close_changes_target(self):
        self.calendar[0]["close"] = "13:00"
        report, code = self.check()
        self.assertEqual(report["target_time"], "2026-10-21T12:45:00-04:00")
        self.assertEqual(self.session.get.call_args_list[2].kwargs["params"]["end"],
                         "2026-10-21T16:45:03+00:00")

    def test_timeout_redacts_details(self):
        self.session.get.side_effect = requests.Timeout("fake-secret https://private.invalid")
        report, code = self.check()
        self.assertEqual(report["clock"], {"error": "request_timeout"})
        self.assertEqual(code, 1)
        self.assertNotIn("fake-secret", json.dumps(report))

    def test_invalid_clock_and_calendar_fail_safely(self):
        for timestamp in ("2026-10-21T21:00:00", "fake-secret"):
            self.clock["timestamp"] = timestamp
            report, code = self.check()
            self.assertEqual(report["reason"], "invalid_clock")
            self.assertEqual(code, 1)
        self.setUp()
        self.calendar[0]["date"] = "2026-10-20"
        report, code = self.check()
        self.assertEqual(report["reason"], "invalid_calendar")
        self.assertEqual(code, 1)

    def test_non200_never_reads_body_and_does_not_follow_redirect(self):
        denied = response({"secret": "fake-secret"}, 302)
        self.session.get.side_effect = None
        self.session.get.return_value = denied
        report, code = self.check()
        denied.json.assert_not_called()
        self.assertEqual(report["clock"], {"http_status": 302})
        self.assertEqual(code, 1)

    def test_latest_sip_success_reports_safe_fields(self):
        original = self.get
        self.session.get.side_effect = lambda url, **kw: (
            response({"quote": quote(), "private": "fake-secret"})
            if url.endswith("/quotes/latest") else original(url, **kw))
        report, code = self.check()
        self.assertEqual(report["sip_latest"]["http_status"], 200)
        self.assertIn("timestamp", report["sip_latest"])
        self.assertNotIn("fake-secret", json.dumps(report))

    def test_main_saves_report_and_no_notification(self):
        report, code = self.check()
        with patch("src.market_diagnostics.market_check", return_value=(report, code)), \
             patch("src.market_diagnostics.Path.write_text") as save:
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(save.call_args.args[0]), report)


class QuoteSummaryTests(unittest.TestCase):
    def test_reason_boundaries(self):
        self.assertTrue(quote_summary(quote(), TARGET)["valid"])
        cases = (
            ({"t": (TARGET-timedelta(seconds=91)).isoformat()}, "stale_over_90_seconds"),
            ({"t": (TARGET+timedelta(seconds=6)).isoformat()}, "future_timestamp"),
            ({"bp": 102}, "crossed_quote"),
            ({"ap": 0}, "nonpositive_price"),
            ({"ap": 102}, "spread_over_0_5_percent"),
            ({"bs": 0}, "empty_quote_size"),
            ({"as": float("nan")}, "invalid_ask_size"),
            ({"bp": float("inf")}, "invalid_bid_price"),
            ({"bp": "fake-secret"}, "invalid_bid_price"),
            ({"t": "fake-secret"}, "invalid_timestamp"),
        )
        for changes, reason in cases:
            with self.subTest(reason=reason):
                result = quote_summary(quote(**changes), TARGET)
                self.assertFalse(result["valid"])
                self.assertIn(reason, result["validation_reasons"])
                self.assertNotIn("fake-secret", json.dumps(result, allow_nan=False))
        for seconds in (-5, 90):
            self.assertTrue(quote_summary(quote(t=(TARGET-timedelta(seconds=seconds)).isoformat()), TARGET)["valid"])


if __name__ == "__main__":
    unittest.main()
