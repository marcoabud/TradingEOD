"""Persistence tests use fake HTTP only and never contact an execution API."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from src.state import claim_missed_window, claim_session, record_evaluation_attempt, StateError


ACCOUNT = "test-account-not-a-credential"
DAY = date(2026, 10, 16)
ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY": "owner/TradingEOD",
    "GITHUB_SHA": "a" * 40,
    "GITHUB_TOKEN": "test-token-not-a-credential",
}


class Response:
    def __init__(self, status):
        self.status_code = status


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected additional HTTP request")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return Response(result)


class StateTests(unittest.TestCase):
    def test_local_claim_is_persistent_and_partitioned(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            self.assertTrue(claim_session(ACCOUNT, DAY, True, environ=env))
            self.assertFalse(claim_session(ACCOUNT, DAY, True, environ=env))
            self.assertTrue(claim_session(ACCOUNT, DAY, False, environ=env))
            self.assertTrue(claim_session("another-account", DAY, True, environ=env))
            self.assertTrue(claim_session(ACCOUNT, date(2026, 10, 19), True, environ=env))
            files = list(Path(directory).rglob("*.json"))
            self.assertEqual(len(files), 4)
            for file in files:
                payload = json.loads(file.read_text())
                self.assertEqual(set(payload), {"mode", "session_date"})
                self.assertNotIn(ACCOUNT, str(file))
                self.assertNotIn(ACCOUNT, file.read_text())

    def test_concurrent_local_claim_allows_exactly_one(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: claim_session(ACCOUNT, DAY, True, environ=env), range(20)))
            self.assertEqual(sum(results), 1)

    def test_local_requires_explicit_absolute_directory(self):
        for env in ({}, {"EOD_STATE_DIR": "relative/path"}):
            with self.subTest(env=env), self.assertRaises(StateError):
                claim_session(ACCOUNT, DAY, True, environ=env)

    def test_github_claim_creates_branch_and_immutable_marker(self):
        session = FakeSession([404, 201, 404, 201])
        self.assertTrue(claim_session(ACCOUNT, DAY, True, environ=ENV, session=session))
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST", "GET", "PUT"])
        branch = session.calls[1][2]["json"]
        self.assertEqual(branch, {"ref": "refs/heads/eod-state", "sha": ENV["GITHUB_SHA"]})
        claim = session.calls[-1]
        account_hash = hashlib.sha256(ACCOUNT.encode()).hexdigest()[:16]
        self.assertTrue(claim[1].endswith(f"contents/sessions/paper/{account_hash}/2026-10-16.json"))
        self.assertNotIn("sha", claim[2]["json"])
        self.assertEqual(claim[2]["json"]["branch"], "eod-state")
        payload = json.loads(base64.b64decode(claim[2]["json"]["content"]))
        self.assertEqual(payload, {"mode": "paper", "session_date": "2026-10-16"})
        for _, url, kwargs in session.calls:
            self.assertTrue(url.startswith("https://api.github.com/repos/owner/TradingEOD/"))
            self.assertEqual(kwargs["timeout"], (5, 20))
            self.assertFalse(kwargs["allow_redirects"])

    def test_existing_marker_never_mutates(self):
        session = FakeSession([200, 200])
        self.assertFalse(claim_session(ACCOUNT, DAY, True, environ=ENV, session=session))
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])

    def test_branch_creation_race_reconciles(self):
        session = FakeSession([404, 422, 200, 404, 201])
        self.assertTrue(claim_session(ACCOUNT, DAY, True, environ=ENV, session=session))
        self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)

    def test_branch_creation_timeout_reconciles_without_replay(self):
        session = FakeSession([404, requests.Timeout("secret URL"), 200, 404, 201])
        self.assertTrue(claim_session(ACCOUNT, DAY, True, environ=ENV, session=session))
        self.assertEqual(sum(call[0] == "POST" for call in session.calls), 1)

    def test_branch_creation_failure_does_not_claim(self):
        session = FakeSession([404, 403, 404])
        with self.assertRaisesRegex(StateError, "HTTP 403"):
            claim_session(ACCOUNT, DAY, True, environ=ENV, session=session)
        self.assertNotIn("PUT", [call[0] for call in session.calls])

    def test_marker_creation_race_requires_verified_marker(self):
        for status in (409, 422):
            with self.subTest(status=status):
                session = FakeSession([200, 404, status, 200])
                self.assertFalse(claim_session(ACCOUNT, DAY, True, environ=ENV, session=session))
                self.assertEqual(sum(call[0] == "PUT" for call in session.calls), 1)
                session = FakeSession([200, 404, status, 404])
                with self.assertRaises(StateError):
                    claim_session(ACCOUNT, DAY, True, environ=ENV, session=session)

    def test_ambiguous_marker_write_skips_even_if_we_may_own_marker(self):
        for failure in (requests.Timeout("token-secret"), requests.ConnectionError("token-secret"), 500):
            with self.subTest(failure=type(failure).__name__):
                session = FakeSession([200, 404, failure, 200])
                self.assertFalse(claim_session(ACCOUNT, DAY, True, environ=ENV, session=session))
                self.assertEqual(sum(call[0] == "PUT" for call in session.calls), 1)

    def test_ambiguous_write_without_marker_fails_closed_and_hides_details(self):
        session = FakeSession([200, 404, requests.Timeout("token-secret https://sensitive.example"), 404])
        with self.assertRaises(StateError) as raised:
            claim_session(ACCOUNT, DAY, True, environ=ENV, session=session)
        self.assertIn("Timeout", str(raised.exception))
        self.assertNotIn("token-secret", str(raised.exception))
        self.assertNotIn("https", str(raised.exception))

    def test_lookup_authentication_error_never_means_absence(self):
        for responses in ([401], [200, 403], [200, 404, 409, 503]):
            with self.subTest(responses=responses), self.assertRaises(StateError):
                claim_session(ACCOUNT, DAY, True, environ=ENV, session=FakeSession(responses))

    def test_invalid_environment_is_rejected_before_http(self):
        for key, value in (
            ("GITHUB_TOKEN", ""), ("GITHUB_TOKEN", "abc\nsecret"),
            ("GITHUB_SHA", "bad"), ("GITHUB_REPOSITORY", "../owner/repo"),
            ("GITHUB_REPOSITORY", "owner/repo?token=secret"), ("GITHUB_ACTIONS", "maybe"),
        ):
            with self.subTest(key=key, value=value):
                env = {**ENV, key: value}
                session = FakeSession([])
                with self.assertRaises(StateError):
                    claim_session(ACCOUNT, DAY, True, environ=env, session=session)
                self.assertEqual(session.calls, [])

    def test_invalid_input_never_requests_http(self):
        for account, day, paper in (("", DAY, True), (None, DAY, True), (ACCOUNT, "2026-02-30", True),
                                    (ACCOUNT, "2026-1-1", True), (ACCOUNT, datetime(2026, 10, 16), True),
                                    (ACCOUNT, DAY, "false")):
            with self.subTest(account=account, day=day, paper=paper), self.assertRaises(StateError):
                claim_session(account, day, paper, environ=ENV, session=FakeSession([]))


class MissedWindowStateTests(unittest.TestCase):
    def test_local_notice_deduplicates_without_consuming_trading_session(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            self.assertTrue(claim_missed_window(ACCOUNT, DAY, True, environ=env))
            self.assertFalse(claim_missed_window(ACCOUNT, DAY, True, environ=env))
            self.assertFalse((Path(directory) / "sessions").exists())
            notices = list((Path(directory) / "notifications").rglob("*.json"))
            self.assertEqual(len(notices), 1)
            self.assertIn("missed-window", notices[0].parts)
            self.assertEqual(json.loads(notices[0].read_text()), {
                "event": "missed_window", "mode": "paper", "session_date": "2026-10-16",
            })
            self.assertNotIn(ACCOUNT, str(notices[0]))
            self.assertNotIn(ACCOUNT, notices[0].read_text())
            self.assertTrue(claim_session(ACCOUNT, DAY, True, environ=env))
            self.assertFalse(claim_missed_window(ACCOUNT, DAY, True, environ=env))

    def test_local_existing_trading_claim_suppresses_without_creating_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            self.assertTrue(claim_session(ACCOUNT, DAY, True, environ=env))
            self.assertFalse(claim_missed_window(ACCOUNT, DAY, True, environ=env))
            self.assertFalse((Path(directory) / "notifications").exists())

    def test_local_notices_are_partitioned_by_account_date_and_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            for account, day, paper in (
                (ACCOUNT, DAY, True), (ACCOUNT, DAY, False),
                ("another-account", DAY, True), (ACCOUNT, date(2026, 10, 19), True),
            ):
                self.assertTrue(claim_missed_window(account, day, paper, environ=env))
            self.assertEqual(len(list(Path(directory).rglob("*.json"))), 4)

    def test_concurrent_local_notices_allow_only_one(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(
                    lambda _: claim_missed_window(ACCOUNT, DAY, True, environ=env), range(20),
                ))
            self.assertEqual(sum(results), 1)
            self.assertFalse((Path(directory) / "sessions").exists())

    def test_local_notice_requires_absolute_state_directory(self):
        for env in ({}, {"EOD_STATE_DIR": "relative/path"}):
            with self.subTest(env=env), self.assertRaises(StateError):
                claim_missed_window(ACCOUNT, DAY, True, environ=env)

    def test_local_suppression_read_failure_cannot_reserve_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("src.state.Path.stat", side_effect=PermissionError("private detail")):
                with self.assertRaisesRegex(StateError, "local suppression lookup") as raised:
                    claim_missed_window(ACCOUNT, DAY, True, environ={"EOD_STATE_DIR": directory})
            self.assertNotIn("private detail", str(raised.exception))
            self.assertEqual(list(Path(directory).rglob("*.json")), [])

    def test_local_notice_creation_failure_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("src.state.os.open", side_effect=PermissionError("private detail")):
                with self.assertRaisesRegex(StateError, "local claim") as raised:
                    claim_missed_window(ACCOUNT, DAY, True, environ={"EOD_STATE_DIR": directory})
            self.assertNotIn("private detail", str(raised.exception))
            self.assertEqual(list(Path(directory).rglob("*.json")), [])

    def test_local_failed_write_retains_notice_but_still_allows_trading_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            with patch("src.state.os.fsync", side_effect=OSError("private detail")):
                with self.assertRaisesRegex(StateError, "local write"):
                    claim_missed_window(ACCOUNT, DAY, True, environ=env)
            self.assertFalse(claim_missed_window(ACCOUNT, DAY, True, environ=env))
            self.assertTrue(claim_session(ACCOUNT, DAY, True, environ=env))

    def test_github_notice_creates_only_separate_atomic_marker_and_deduplicates(self):
        session = FakeSession([200, 404, 404, 404, 201, 200, 404, 404, 200])
        self.assertTrue(claim_missed_window(ACCOUNT, DAY, True, environ=ENV, session=session))
        self.assertFalse(claim_missed_window(ACCOUNT, DAY, True, environ=ENV, session=session))
        puts = [call for call in session.calls if call[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        account_hash = hashlib.sha256(ACCOUNT.encode()).hexdigest()[:16]
        self.assertTrue(puts[0][1].endswith(
            f"contents/notifications/missed-window/paper/{account_hash}/2026-10-16.json"
        ))
        body = puts[0][2]["json"]
        self.assertNotIn("sha", body)
        self.assertEqual(body["branch"], "eod-state")
        self.assertEqual(body["message"], "Claim EOD missed-window notification 2026-10-16 (paper)")
        self.assertEqual(json.loads(base64.b64decode(body["content"])), {
            "event": "missed_window", "mode": "paper", "session_date": "2026-10-16",
        })
        for method, url, kwargs in session.calls:
            if "/contents/sessions/" in url:
                self.assertEqual(method, "GET")
            self.assertEqual(kwargs["timeout"], (5, 20))
            self.assertFalse(kwargs["allow_redirects"])

    def test_github_trading_claim_suppresses_notice_without_mutation(self):
        session = FakeSession([200, 200])
        self.assertFalse(claim_missed_window(ACCOUNT, DAY, True, environ=ENV, session=session))
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])
        self.assertIn("/contents/sessions/", session.calls[-1][1])
        self.assertFalse(any("/contents/notifications/" in call[1] for call in session.calls))

    def test_github_notice_does_not_impede_later_trading_claim(self):
        session = FakeSession([200, 404, 404, 404, 201, 200, 404, 201])
        self.assertTrue(claim_missed_window(ACCOUNT, DAY, True, environ=ENV, session=session))
        self.assertTrue(claim_session(ACCOUNT, DAY, True, environ=ENV, session=session))
        paths = [call[1] for call in session.calls if call[0] == "PUT"]
        self.assertEqual(len(paths), 2)
        self.assertIn("/contents/notifications/missed-window/", paths[0])
        self.assertIn("/contents/sessions/", paths[1])

    def test_github_notice_can_initialize_branch_without_claiming_trading(self):
        session = FakeSession([404, 201, 404, 404, 404, 201])
        self.assertTrue(claim_missed_window(ACCOUNT, DAY, True, environ=ENV, session=session))
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST", "GET", "GET", "GET", "PUT"])
        self.assertTrue(session.calls[-1][1].find("/contents/notifications/missed-window/") > 0)

    def test_notice_suppression_lookup_failure_never_means_no_trade(self):
        for failure in (401, 403, 503, requests.Timeout("private-token https://private.example")):
            with self.subTest(failure=type(failure).__name__):
                session = FakeSession([200, failure])
                with self.assertRaises(StateError) as raised:
                    claim_missed_window(ACCOUNT, DAY, True, environ=ENV, session=session)
                self.assertNotIn("private-token", str(raised.exception))
                self.assertNotIn("https", str(raised.exception))
                self.assertEqual([call[0] for call in session.calls], ["GET", "GET"])

    def test_ambiguous_github_notice_creation_does_not_retry_or_authorize_alert(self):
        for failure in (409, 422, 500, requests.Timeout("private-token")):
            with self.subTest(failure=type(failure).__name__):
                session = FakeSession([200, 404, 404, 404, failure, 200])
                self.assertFalse(claim_missed_window(ACCOUNT, DAY, True, environ=ENV, session=session))
                self.assertEqual(sum(call[0] == "PUT" for call in session.calls), 1)
                self.assertIn("/contents/notifications/missed-window/", session.calls[-1][1])

    def test_ambiguous_github_notice_without_verified_marker_fails_closed(self):
        session = FakeSession([200, 404, 404, 404, requests.Timeout("private-token"), 404])
        with self.assertRaises(StateError) as raised:
            claim_missed_window(ACCOUNT, DAY, True, environ=ENV, session=session)
        self.assertNotIn("private-token", str(raised.exception))
        self.assertEqual(sum(call[0] == "PUT" for call in session.calls), 1)

    def test_invalid_notice_input_or_environment_never_requests_http(self):
        for account, day, paper, environ in (
            (None, DAY, True, ENV), (ACCOUNT, "2026-02-30", True, ENV),
            (ACCOUNT, DAY, "true", ENV), (ACCOUNT, DAY, True, {**ENV, "GITHUB_ACTIONS": "maybe"}),
            (ACCOUNT, DAY, True, {**ENV, "GITHUB_TOKEN": ""}),
        ):
            with self.subTest(account=account, day=day, paper=paper):
                session = FakeSession([])
                with self.assertRaises(StateError):
                    claim_missed_window(account, day, paper, environ=environ, session=session)
                self.assertEqual(session.calls, [])


class EvaluationAttemptStateTests(unittest.TestCase):
    def test_local_evaluation_record_never_consumes_trading_or_notification_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            self.assertTrue(record_evaluation_attempt(ACCOUNT, DAY, True, environ=env))
            self.assertFalse(record_evaluation_attempt(ACCOUNT, DAY, True, environ=env))
            self.assertFalse((Path(directory) / "sessions").exists())
            self.assertFalse((Path(directory) / "notifications").exists())
            markers = list((Path(directory) / "evaluations").rglob("*.json"))
            self.assertEqual(len(markers), 1)
            self.assertEqual(json.loads(markers[0].read_text()), {
                "event": "evaluation_started", "mode": "paper", "session_date": "2026-10-16",
            })
            self.assertNotIn(ACCOUNT, str(markers[0]))
            self.assertNotIn(ACCOUNT, markers[0].read_text())
            self.assertTrue(claim_session(ACCOUNT, DAY, True, environ=env))
            self.assertFalse(claim_session(ACCOUNT, DAY, True, environ=env))

    def test_local_attempt_suppresses_late_notice_even_without_trading_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            self.assertTrue(record_evaluation_attempt(ACCOUNT, DAY, True, environ=env))
            self.assertFalse(claim_missed_window(ACCOUNT, DAY, True, environ=env))
            self.assertFalse((Path(directory) / "sessions").exists())
            self.assertFalse((Path(directory) / "notifications").exists())

    def test_local_evaluations_and_notice_suppression_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            self.assertTrue(record_evaluation_attempt(ACCOUNT, DAY, True, environ=env))
            for account, day, paper in (
                ("another-account", DAY, True), (ACCOUNT, date(2026, 10, 19), True),
                (ACCOUNT, DAY, False),
            ):
                with self.subTest(account=account, day=day, paper=paper):
                    self.assertTrue(claim_missed_window(account, day, paper, environ=env))
                    self.assertTrue(record_evaluation_attempt(account, day, paper, environ=env))
                    self.assertTrue(claim_session(account, day, paper, environ=env))
            self.assertEqual(len(list((Path(directory) / "evaluations").rglob("*.json"))), 4)

    def test_concurrent_local_evaluations_record_only_one_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"EOD_STATE_DIR": directory}
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(
                    lambda _: record_evaluation_attempt(ACCOUNT, DAY, True, environ=env), range(20),
                ))
            self.assertEqual(sum(results), 1)
            self.assertTrue(claim_session(ACCOUNT, DAY, True, environ=env))

    def test_github_attempt_is_atomic_deduplicated_and_does_not_read_or_claim_trading(self):
        session = FakeSession([200, 404, 201, 200, 200])
        self.assertTrue(record_evaluation_attempt(ACCOUNT, DAY, True, environ=ENV, session=session))
        self.assertFalse(record_evaluation_attempt(ACCOUNT, DAY, True, environ=ENV, session=session))
        puts = [call for call in session.calls if call[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        account_hash = hashlib.sha256(ACCOUNT.encode()).hexdigest()[:16]
        self.assertTrue(puts[0][1].endswith(f"contents/evaluations/paper/{account_hash}/2026-10-16.json"))
        body = puts[0][2]["json"]
        self.assertNotIn("sha", body)
        self.assertEqual(body["message"], "Claim EOD evaluation attempt 2026-10-16 (paper)")
        self.assertEqual(json.loads(base64.b64decode(body["content"])), {
            "event": "evaluation_started", "mode": "paper", "session_date": "2026-10-16",
        })
        self.assertFalse(any("/contents/sessions/" in call[1] for call in session.calls))

    def test_github_evaluation_suppresses_notice_without_any_mutation(self):
        session = FakeSession([200, 404, 200])
        self.assertFalse(claim_missed_window(ACCOUNT, DAY, True, environ=ENV, session=session))
        self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "GET"])
        self.assertIn("/contents/sessions/", session.calls[1][1])
        self.assertIn("/contents/evaluations/", session.calls[2][1])
        self.assertFalse(any("/contents/notifications/" in call[1] for call in session.calls))

    def test_github_existing_evaluation_still_allows_new_trading_claim(self):
        session = FakeSession([200, 200, 200, 404, 201])
        self.assertFalse(record_evaluation_attempt(ACCOUNT, DAY, True, environ=ENV, session=session))
        self.assertTrue(claim_session(ACCOUNT, DAY, True, environ=ENV, session=session))
        puts = [call for call in session.calls if call[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertIn("/contents/sessions/", puts[0][1])

    def test_evaluation_lookup_failure_never_means_no_attempt(self):
        for failure in (403, 503, requests.Timeout("private-token https://private.example")):
            with self.subTest(failure=type(failure).__name__):
                session = FakeSession([200, 404, failure])
                with self.assertRaises(StateError) as raised:
                    claim_missed_window(ACCOUNT, DAY, True, environ=ENV, session=session)
                self.assertNotIn("private-token", str(raised.exception))
                self.assertEqual([call[0] for call in session.calls], ["GET", "GET", "GET"])

    def test_local_evaluation_lookup_failure_does_not_create_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("src.state.Path.stat", side_effect=[FileNotFoundError(), PermissionError("private-token")]):
                with self.assertRaisesRegex(StateError, "suppression lookup"):
                    claim_missed_window(ACCOUNT, DAY, True, environ={"EOD_STATE_DIR": directory})
            self.assertEqual(list(Path(directory).rglob("*.json")), [])

    def test_recording_failures_are_sanitized_and_grant_no_trading_claim(self):
        session = FakeSession([200, 404, requests.Timeout("private-token"), 404])
        with self.assertRaises(StateError) as raised:
            record_evaluation_attempt(ACCOUNT, DAY, True, environ=ENV, session=session)
        self.assertNotIn("private-token", str(raised.exception))
        self.assertFalse(any("/contents/sessions/" in call[1] for call in session.calls))
        with tempfile.TemporaryDirectory() as directory:
            with patch("src.state.os.open", side_effect=PermissionError("private-token")):
                with self.assertRaises(StateError):
                    record_evaluation_attempt(ACCOUNT, DAY, True, environ={"EOD_STATE_DIR": directory})
            self.assertFalse((Path(directory) / "sessions").exists())

    def test_string_suppression_remains_compatible(self):
        from src.state import _claim_github, _claim_local

        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "suppress.json").write_text("{}")
            self.assertFalse(_claim_local(directory, "notice.json", b"{}", suppress_if_exists="suppress.json"))
            self.assertFalse((Path(directory) / "notice.json").exists())
        session = FakeSession([200, 200])
        self.assertFalse(_claim_github(
            ENV, "notice.json", b"{}", "2026-10-16", "paper", session,
            suppress_if_exists="suppress.json",
        ))
        self.assertTrue(session.calls[-1][1].endswith("contents/suppress.json"))


if __name__ == "__main__":
    unittest.main()
