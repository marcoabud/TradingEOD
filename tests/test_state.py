"""Persistence tests use fake HTTP only and never contact an execution API."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import requests

from src.state import claim_session, StateError


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


if __name__ == "__main__":
    unittest.main()
