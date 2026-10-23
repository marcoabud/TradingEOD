"""Read-only observer tests: every HTTP call and clock is synthetic."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import requests

from src.observation import (MAX_BYTES, ObservationError, PaperData, aggregate, automatic_campaign,
                             fetch_observations, fetch_summary,
                             instant_ns, observe, parse_summary_zip, samples_from_summary,
                             wait_for_delayed_sip)


CHECKED = "2026-10-23T19:45:03.123456Z"
REPO = "marcoabud/TradingEOD"


def summary():
    return dict(mode="paper", dry_run=False, status="completed", session_date="2026-10-23",
                feed="iex", orders=[], quote_checks=[dict(phase="entries", checked_at=CHECKED,
                diagnostics={"JPM": dict(checked_at=CHECKED, timestamp="2026-10-23T19:45:02Z",
                                         bid=100, ask=100.1, spread=.001, attempts=1)})])


def archive(value, filename="run_summary.json"):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr(filename, json.dumps(value))
    return output.getvalue()


def response(value=None, *, status=200, headers=None, raw=None):
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    result.iter_content.return_value = [raw if raw is not None else json.dumps(value).encode()]
    return result


def run_metadata(**changes):
    return dict(dict(id=1234, run_attempt=2, name="Daily EOD Trading", status="completed",
                     event="schedule", head_branch="main", head_repository={"full_name": REPO},
                     path=".github/workflows/daily_trading.yml"), **changes)


def artifact_session(value=None):
    raw = archive(summary() if value is None else value)
    artifact = dict(id=888, name="eod-summary-1234-2", expired=False,
                    size_in_bytes=len(raw), digest="sha256:" + sha256(raw).hexdigest())
    session = Mock()
    session.get.side_effect = [response(run_metadata()), response({"artifacts": [artifact]}),
                              response(status=302, headers={"Location": "https://example.blob.core.windows.net/summary?signature=private"}),
                              response(raw=raw)]
    return session


class ArtifactTests(unittest.TestCase):
    def test_downloads_only_exact_run_and_attempt_and_never_forwards_github_token(self):
        session = artifact_session()
        result, identity = fetch_summary(session, REPO, "1234", "private-token", "2")
        self.assertEqual(result, summary())
        self.assertEqual(identity, {"run_id": "1234", "run_attempt": 2, "artifact_id": 888})
        calls = session.get.call_args_list
        self.assertTrue(calls[0].args[0].endswith("/runs/1234/attempts/2"))
        self.assertTrue(calls[1].args[0].endswith("/runs/1234/artifacts"))
        self.assertTrue(calls[2].args[0].endswith("/artifacts/888/zip"))
        self.assertEqual(calls[3].kwargs["headers"], {})
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in calls))
        session.post.assert_not_called()

    def test_foreign_repo_branch_event_or_workflow_skip_before_artifact_download(self):
        for changes in ({"head_repository": {"full_name": "attacker/fork"}}, {"head_branch": "other"},
                        {"event": "pull_request"}, {"path": ".github/workflows/other.yml"}, {"id": 999}):
            with self.subTest(changes=changes):
                session = Mock()
                session.get.return_value = response(run_metadata(**changes))
                result, _ = fetch_summary(session, REPO, "1234", "token")
                self.assertIsNone(result)
                self.assertEqual(session.get.call_count, 1)

    def test_mismatched_attempt_is_rejected(self):
        session = Mock()
        session.get.return_value = response(run_metadata())
        with self.assertRaisesRegex(ObservationError, "attempt"):
            fetch_summary(session, REPO, "1234", "token", "1")

    def test_unsafe_redirect_is_rejected_without_sending_request_or_credentials(self):
        session = artifact_session()
        replies = list(session.get.side_effect)
        replies[2] = response(status=302, headers={"Location": "https://attacker.example/steal"})
        session.get.side_effect = replies
        with self.assertRaisesRegex(ObservationError, "host"):
            fetch_summary(session, REPO, "1234", "token")
        self.assertEqual(session.get.call_count, 3)

    def test_digest_mismatch_is_rejected(self):
        session = artifact_session()
        replies = list(session.get.side_effect)
        replies[-1] = response(raw=archive({"changed": True}))
        session.get.side_effect = replies
        with self.assertRaisesRegex(ObservationError, "digest"):
            fetch_summary(session, REPO, "1234", "token")

    def test_zip_paths_extra_files_and_symlinks_are_rejected(self):
        for name in ("../run_summary.json", "/run_summary.json", "x/run_summary.json", "x\\run_summary.json"):
            with self.subTest(name=name), self.assertRaises(ObservationError):
                parse_summary_zip(archive(summary(), name))
        for symlink in (False, True):
            raw = BytesIO()
            with zipfile.ZipFile(raw, "w") as zipped:
                entry = zipfile.ZipInfo("run_summary.json")
                if symlink:
                    entry.create_system = 3
                    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                zipped.writestr(entry, json.dumps(summary()))
                if not symlink:
                    zipped.writestr("script.py", "never execute")
            with self.assertRaises(ObservationError):
                parse_summary_zip(raw.getvalue())

    def test_oversized_or_nonobject_json_is_rejected(self):
        for raw in (b"x" * (MAX_BYTES + 1), archive(["unexpected"]), archive({"x": "x" * MAX_BYTES})):
            with self.subTest(size=len(raw)), self.assertRaises(ObservationError):
                parse_summary_zip(raw)

    def test_http_errors_do_not_expose_provider_message(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("private-token https://secret.example")
        with self.assertRaises(ObservationError) as raised:
            fetch_summary(session, REPO, "1234", "private-token")
        self.assertNotIn("private-token", str(raised.exception))
        self.assertNotIn("secret.example", str(raised.exception))


class TimestampAndSipTests(unittest.TestCase):
    def test_nanosecond_order_and_timezone_are_preserved(self):
        self.assertEqual(instant_ns("2026-10-23T15:45:03.123456-04:00"), instant_ns(CHECKED))
        self.assertEqual(instant_ns("2026-10-23T19:45:03.123456001Z") - instant_ns(CHECKED), 1)
        with self.assertRaises(ObservationError):
            instant_ns("2026-10-23T19:45:03")

    def test_empty_checks_are_ignored_and_inner_check_time_is_used(self):
        value = summary()
        value["quote_checks"].insert(0, {"phase": "holdings", "checked_at": None, "diagnostics": {}})
        value["quote_checks"][1]["diagnostics"]["JPM"]["checked_at"] = "2026-10-23T19:45:01Z"
        samples = samples_from_summary(value)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["checked_at"], "2026-10-23T19:45:01Z")

    def test_check_cannot_be_later_than_envelope_or_on_another_session(self):
        for stamp in ("2026-10-23T19:45:04Z", "2026-10-22T19:45:03Z"):
            value = summary()
            value["quote_checks"][0]["diagnostics"]["JPM"]["checked_at"] = stamp
            with self.assertRaises(ObservationError):
                samples_from_summary(value)

    def test_sip_query_uses_exact_observation_cutoff_descending_and_one_symbol(self):
        session = Mock()
        session.get.return_value = response({"quotes": {"JPM": [{"t": "2026-10-23T19:45:03.123455999Z", "bp": 99.9, "ap": 100.1}]}})
        result = PaperData(session, "key", "secret").last_sip("JPM", CHECKED)
        params = session.get.call_args.kwargs["params"]
        self.assertEqual({key: params[key] for key in ("end", "sort", "limit", "feed", "symbols")},
                         {"end": CHECKED, "sort": "desc", "limit": 1, "feed": "sip", "symbols": "JPM"})
        self.assertAlmostEqual(result["age_seconds"], .000000001)
        self.assertTrue(result["price_valid"])
        session.post.assert_not_called()

    def test_sip_quote_one_nanosecond_after_cutoff_is_rejected(self):
        session = Mock()
        session.get.return_value = response({"quotes": {"JPM": [{"t": "2026-10-23T19:45:03.123456001Z", "bp": 100, "ap": 100.1}]}})
        with self.assertRaisesRegex(ObservationError, "exceeds"):
            PaperData(session, "key", "secret").last_sip("JPM", CHECKED)

    def test_sip_price_is_not_automatically_a_usable_execution_quote(self):
        for fields, valid in (({"bs": 2, "as": 2}, True), ({}, False),
                              ({"bs": 0, "as": 2}, False), ({"bs": 2, "as": 2, "ap": 110}, False)):
            with self.subTest(fields=fields):
                row = dict(dict(t="2026-10-23T19:45:03Z", bp=100, ap=100.1), **fields)
                session = Mock()
                session.get.return_value = response({"quotes": {"JPM": [row]}})
                result = PaperData(session, "key", "secret").last_sip("JPM", CHECKED)
                self.assertEqual(result["execution_quality_valid"], valid)

    def test_sip_age_retains_nanosecond_precision_at_freshness_boundary(self):
        session = Mock()
        session.get.return_value = response({"quotes": {"JPM": [{"t": "2026-10-23T19:43:33.123455999Z",
                                                                 "bp": 100, "ap": 100.1, "bs": 2, "as": 2}]}})
        result = PaperData(session, "key", "secret").last_sip("JPM", CHECKED)
        self.assertFalse(result["execution_quality_valid"])
        self.assertEqual(result["quality_cause"], "stale")

    def test_original_sizes_and_first_last_retry_evidence_are_preserved(self):
        value = summary()
        diagnostic = value["quote_checks"][0]["diagnostics"]["JPM"]
        diagnostic.update(bid_size=7, ask_size=8, recovered=True, attempts=2,
                          attempt_history=[dict(checked_at="2026-10-23T19:45:01Z", cause="stale", bid=100, ask=100.1),
                                           dict(checked_at=CHECKED, cause=None, bid=100, ask=100.1)])
        result = samples_from_summary(value)[0]["recorded"]
        self.assertEqual((result["bid_size"], result["ask_size"]), (7, 8))
        self.assertTrue(result["recovered"])
        self.assertEqual(result["first"]["cause"], "stale")
        self.assertIsNone(result["last"]["cause"])
        diagnostic["attempt_history"][0]["checked_at"] = "2026-10-23T19:45:04Z"
        with self.assertRaisesRegex(ObservationError, "retry time"):
            samples_from_summary(value)

    def test_missing_sip_has_no_invented_price(self):
        session = Mock()
        session.get.return_value = response({"quotes": {"JPM": []}})
        self.assertIsNone(PaperData(session, "key", "secret").last_sip("JPM", CHECKED))

    def test_wait_uses_real_recorded_check_and_sleeps_at_most_sixty_seconds(self):
        elapsed, sleeps = [0.0], []
        start = datetime.fromisoformat(CHECKED) + timedelta(minutes=13)
        def sleep(seconds):
            sleeps.append(seconds)
            elapsed[0] += seconds
        wait_for_delayed_sip(samples_from_summary(summary()),
                             lambda: (start + timedelta(seconds=elapsed[0])).isoformat(),
                             sleeper=sleep, monotonic=lambda: elapsed[0])
        self.assertEqual(sum(sleeps), 121)
        self.assertTrue(all(0 < item <= 60 for item in sleeps))

    def test_future_timestamp_beyond_wait_budget_is_rejected_without_sleep(self):
        sleeper = Mock()
        with self.assertRaisesRegex(ObservationError, "twenty-minute"):
            wait_for_delayed_sip(samples_from_summary(summary()), lambda: "2026-10-23T19:30:00Z",
                                 sleeper=sleeper, monotonic=lambda: 0)
        sleeper.assert_not_called()


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(ALPACA_PAPER="true", GITHUB_REPOSITORY=REPO, EOD_OBSERVATION_RUN_ID="1234",
                        ALPACA_API_KEY="synthetic", ALPACA_SECRET_KEY="synthetic",
                        GITHUB_EVENT_NAME="workflow_run")
        self.data = Mock()
        self.data.clock.return_value = "2026-10-23T20:01:00Z"
        self.data.last_sip.return_value = dict(timestamp="2026-10-23T19:45:03Z", bid=100, ask=100.1,
                                              spread=.001, age_seconds=.123456, price_valid=True)

    def run_observe(self, value=None):
        with patch("src.observation.fetch_summary", return_value=(summary() if value is None else value,
                   dict(run_id="1234", run_attempt=2, artifact_id=888))), \
             patch("src.observation.PaperData", return_value=self.data):
            return observe(self.env, Mock(), sleeper=Mock(), monotonic=lambda: 0)

    def test_evaluated_no_order_paper_session_is_observed_not_a_fill_validation(self):
        report = self.run_observe()
        self.assertEqual(report["status"], "observed")
        self.assertTrue(report["campaign_eligible"])
        self.assertEqual(report["source_status"], "completed")
        self.assertEqual(report["confirmed_fills"], [])
        self.assertEqual(report["comparisons"][0]["checked_at"], CHECKED)

    def test_blocked_source_remains_identified_as_blocked(self):
        value = summary()
        value["status"] = "blocked"
        report = self.run_observe(value)
        self.assertEqual(report["status"], "observed")
        self.assertEqual(report["source_status"], "blocked")

    def test_core_evaluation_requires_weekly_flag_and_computed_regime(self):
        for regime, weekly, expected in (("NOT_EVALUATED", True, False), ("RISK-ON", True, True),
                                        ("RISK_OFF", True, True), ("RISK-ON", False, False)):
            value = summary()
            value.update(regime=regime, weekly_rebalance=weekly)
            self.assertEqual(self.run_observe(value)["core_evaluated"], expected)

    def test_nonpaper_dryrun_skipped_and_missing_timestamp_evidence_do_not_query_sip(self):
        for update in ({"mode": "live"}, {"dry_run": True}, {"status": "skipped"}):
            with self.subTest(update=update):
                value = summary()
                value.update(update)
                report = self.run_observe(value)
                self.assertEqual(report["status"], "skipped")
                self.assertFalse(report["campaign_eligible"])
        self.data.last_sip.assert_not_called()

    def test_old_summary_without_quote_checks_cannot_supply_observation_evidence(self):
        value = summary()
        value.pop("quote_checks")
        report = self.run_observe(value)
        self.assertEqual(report["status"], "skipped")
        self.assertEqual(report["reason"], "insufficient_quote_evidence")
        self.assertFalse(report["campaign_eligible"])
        self.data.last_sip.assert_not_called()

    def test_new_engine_no_quotes_required_counts_operation_but_never_quotes(self):
        for status in ("completed", "degraded"):
            value = summary()
            value.update(status=status, quote_checks=[])
            report = self.run_observe(value)
            self.assertEqual(report["status"], "observed")
            self.assertTrue(report["campaign_eligible"])
            self.assertEqual(report["evidence_kind"], "operational_only_no_quotes_required")
            self.assertEqual(report["comparisons"], [])
            self.data.last_sip.assert_not_called()

    def test_blocked_empty_quote_diagnostics_are_not_counted_as_completed_operations(self):
        value = summary()
        value.update(status="blocked", quote_checks=[])
        report = self.run_observe(value)
        self.assertEqual(report["status"], "skipped")
        self.assertFalse(report["campaign_eligible"])

    def test_manual_historical_audit_is_allowed_but_never_counted_before_campaign(self):
        self.env["EOD_OBSERVATION_START_DATE"] = "2026-10-24"
        self.assertEqual(self.run_observe()["status"], "skipped")
        self.data.last_sip.assert_not_called()
        self.env["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        report = self.run_observe()
        self.assertEqual(report["status"], "observed")
        self.assertFalse(report["campaign_eligible"])

    def test_historical_failure_preserves_source_identity_without_raw_exception(self):
        self.data.last_sip.side_effect = ObservationError("private-token")
        report = self.run_observe()
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["run_id"], "1234")
        self.assertFalse(report["campaign_eligible"])
        self.assertNotIn("private-token", json.dumps(report))

    def test_disabled_paper_mode_never_downloads_artifact_or_calls_alpaca(self):
        self.env["ALPACA_PAPER"] = "false"
        session = Mock()
        self.assertEqual(observe(self.env, session)["status"], "skipped")
        session.get.assert_not_called()


class CampaignTests(unittest.TestCase):
    def report(self, day, run_id="1234", attempt=1, **updates):
        return dict(dict(schema_version=1, status="observed", mode="paper", campaign_eligible=True,
                         repository=REPO, session_date=day, run_id=run_id, run_attempt=attempt,
                         comparisons=[{"status": "compared"}]), **updates)

    def collect(self, values):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, value in enumerate(values):
                path = Path(directory) / f"report-{index}.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                paths.append(path)
            return aggregate(paths)

    def test_repeated_run_claim_and_date_only_count_one_session(self):
        first = self.report("2026-10-23")
        report = self.collect([first, first, self.report("2026-10-23", "1235"),
                               self.report("2026-10-23", "1234", 2)])
        self.assertEqual(report["distinct_sessions"], 1)
        self.assertFalse(report["complete"])

    def test_ten_distinct_dates_are_required_and_prestart_or_incomplete_reports_do_not_count(self):
        values = [self.report("2026-10-22", "99"), self.report("2026-10-23", "98", status="incomplete")]
        days = ["2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29",
                "2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04"]
        values += [self.report(day, str(1000 + i), core_evaluated=i in (0, 5)) for i, day in enumerate(days)]
        self.assertFalse(self.collect(values)["complete"])
        values.append(self.report("2026-11-05", "2000"))
        report = self.collect(values)
        self.assertEqual(report["distinct_sessions"], 10)
        self.assertTrue(report["complete"])
        self.assertEqual(report["distinct_core_evaluations"], 2)
        self.assertFalse(report["execution_evidence"]["has_buy_then_later_sell"])

    def test_ten_observed_dates_do_not_complete_without_two_distinct_core_dates(self):
        values = [self.report(f"2026-11-{i:02}", str(1000 + i), core_evaluated=i == 2) for i in range(1, 11)]
        values.append(self.report("2026-11-02", "9999", core_evaluated=True))
        report = self.collect(values)
        self.assertEqual(report["distinct_sessions"], 10)
        self.assertEqual(report["distinct_core_evaluations"], 1)
        self.assertFalse(report["complete"])

    def test_fills_require_actual_later_same_symbol_sell_and_no_duplicate_count(self):
        first = self.report("2026-10-23", confirmed_fills=[{"symbol": "META", "side": "buy"},
                                                        {"symbol": "META", "side": "sell"}])
        report = self.collect([first, self.report("2026-10-26", "1235", confirmed_fills=[{"symbol": "AAPL", "side": "sell"}])])
        self.assertFalse(report["execution_evidence"]["has_buy_then_later_sell"])
        report = self.collect([first, self.report("2026-10-26", "1235", confirmed_fills=[{"symbol": "META", "side": "sell"}])])
        self.assertEqual(report["execution_evidence"]["confirmed_buy_then_later_sell_symbols"], ["META"])

    def test_mixing_different_repository_accounts_is_rejected(self):
        with self.assertRaisesRegex(ObservationError, "mix"):
            self.collect([self.report("2026-10-23"), self.report("2026-10-26", "1235", repository="other/account")])

    def test_operational_no_quote_date_counts_separately_from_quote_evidence(self):
        report = self.collect([self.report("2026-10-23", comparisons=[], evidence_kind="operational_only_no_quotes_required"),
                               self.report("2026-10-26", "1235")])
        self.assertEqual(report["distinct_sessions"], 2)
        self.assertEqual(report["quote_evidence_sessions"], 1)


class AutomaticCampaignTests(unittest.TestCase):
    def source_report(self):
        return CampaignTests().report("2026-10-23")

    def session(self, *, source=None, metadata=None):
        raw = archive(self.source_report() if source is None else source, "paper_observation_report.json")
        artifact = dict(id=888, name="paper-observation-1234-5555-1", expired=False,
                        size_in_bytes=len(raw), digest="sha256:" + sha256(raw).hexdigest(),
                        created_at="2026-10-23T20:05:00Z", workflow_run={"id": 5555})
        run = run_metadata(id=5555, run_attempt=1, name="Observe Paper Quotes", event="workflow_run",
                           path=".github/workflows/observe_paper.yml")
        run.update(metadata or {})
        session = Mock()
        session.get.side_effect = [response({"artifacts": [artifact]}), response(run),
                                  response(status=302, headers={"Location": "https://example.blob.core.windows.net/report?signature=private"}),
                                  response(raw=raw)]
        return session

    def test_automatically_collects_only_provenance_verified_readonly_reports(self):
        session = self.session()
        reports = fetch_observations(session, REPO, "private-token")
        self.assertEqual(reports, [self.source_report()])
        calls = session.get.call_args_list
        self.assertTrue(calls[0].args[0].endswith("/actions/artifacts"))
        self.assertTrue(calls[1].args[0].endswith("/runs/5555/attempts/1"))
        self.assertEqual(calls[-1].kwargs["headers"], {})
        session.post.assert_not_called()

    def test_foreign_workflow_branch_fork_or_event_never_downloads_artifact(self):
        for change in ({"path": ".github/workflows/untrusted.yml"}, {"head_branch": "feature"},
                       {"head_repository": {"full_name": "attacker/fork"}}, {"event": "pull_request"},
                       {"run_attempt": 2}, {"status": "in_progress"}):
            with self.subTest(change=change):
                session = self.session(metadata=change)
                self.assertEqual(fetch_observations(session, REPO, "private-token"), [])
                self.assertEqual(session.get.call_count, 2)

    def test_report_identity_cannot_disagree_with_artifact_name(self):
        value = self.source_report()
        value["run_id"] = "9999"
        with self.assertRaisesRegex(ObservationError, "identity"):
            fetch_observations(self.session(source=value), REPO, "private-token")

    def test_automatic_campaign_merges_current_report_and_history_without_alpaca(self):
        current = self.source_report()
        old = dict(current, run_id="1235", session_date="2026-10-26")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper_observation_report.json"
            path.write_text(json.dumps(current), encoding="utf-8")
            with patch("src.observation.fetch_observations", return_value=[current, old]) as collect, \
                 patch("src.observation.PaperData") as data:
                report = automatic_campaign({"GITHUB_REPOSITORY": REPO, "GITHUB_TOKEN": "private"}, Mock(), path)
            self.assertEqual(report["distinct_sessions"], 2)
            self.assertTrue(report["history_complete"])
            self.assertEqual(report["status"], "aggregated")
            data.assert_not_called()
            self.assertEqual(collect.call_args.args[1:], (REPO, "private"))

    def test_history_error_produces_partial_counts_and_never_false_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper_observation_report.json"
            path.write_text(json.dumps(self.source_report()), encoding="utf-8")
            with patch("src.observation.fetch_observations", side_effect=ObservationError("private-token")):
                report = automatic_campaign({"GITHUB_REPOSITORY": REPO}, Mock(), path)
            self.assertEqual(report["distinct_sessions"], 1)
            self.assertEqual(report["status"], "incomplete")
            self.assertFalse(report["complete"])
            self.assertNotIn("private-token", json.dumps(report))

    def test_collection_time_budget_prevents_more_network_requests(self):
        session = self.session()
        with self.assertRaisesRegex(ObservationError, "time budget"):
            fetch_observations(session, REPO, "private-token", monotonic=Mock(side_effect=[0, 0, 121]))
        self.assertEqual(session.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
