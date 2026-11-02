"""Durable recovery and broker evidence tests use only temporary files/fake HTTP."""
import base64
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import requests

from src.core_recovery import (CoreRecoveryStore, MAX_RECOVERY_DAYS, allocation_reconciled,
                               build_intent, core_quantities, intent_digest,
                               reconcile_intent, select_recovery_decision)
from src.safety import NY, SafetyError
from src.state import StateError
from src.strategy import CoreDecision


ACCOUNT = "fake-account"
HASH = hashlib.sha256(ACCOUNT.encode()).hexdigest()[:16]
FRIDAY, MONDAY = date(2026, 10, 30), date(2026, 11, 2)
START = datetime(2026, 10, 30, 15, 45, tzinfo=NY)
OFF = CoreDecision("RISK-OFF", {"SGOV": .7}, {}, ())
ON = CoreDecision("RISK-ON", {"SPY": .35, "QQQ": .35}, {}, ())
POSITIONS = {"SPY": NS(symbol="SPY", qty="1.841321657"),
             "QQQ": NS(symbol="QQQ", qty="1.986672078")}
ENV = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "owner/TradingEOD",
       "GITHUB_SHA": "a" * 40, "GITHUB_TOKEN": "fake-not-a-credential"}


def intent(**changes):
    row = build_intent(HASH, "paper", FRIDAY, OFF, POSITIONS, "3956.85",
                       started_at=START, signal_as_of="2026-10-29", revision="a" * 40)
    row.update(changes)
    return row


def receipt(symbol="SPY", *, side="sell", qty=None, day=FRIDAY, **changes):
    qty = qty or (POSITIONS[symbol].qty if symbol in POSITIONS else "27.6")
    stamp = datetime.combine(day, datetime.min.time(), NY).replace(hour=15, minute=45, second=10)
    row = dict(id=f"id-{symbol}-{day}", symbol=symbol, side=side, status="filled", qty=qty,
               filled_qty=qty, filled_avg_price="100", submitted_at=stamp,
               filled_at=stamp + timedelta(seconds=2),
               client_order_id=f"eod-v1-{day:%Y%m%d}-{side[0]}-{symbol}")
    row.update(changes)
    return NS(**row)


def activity(order, **changes):
    row = dict(id=f"fill-{order.id}", order_id=order.id, symbol=order.symbol,
               side=order.side, qty=order.filled_qty, transaction_time=order.filled_at)
    row.update(changes)
    return row


class Response:
    def __init__(self, status, value=None):
        self.status_code, self.value = status, value
    def json(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class CoreRecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {"EOD_STATE_DIR": self.temp.name}
        self.store = CoreRecoveryStore(ACCOUNT, True, environ=self.env)

    def begin(self, store=None, **kwargs):
        return (store or self.store).begin(FRIDAY, OFF, POSITIONS, "3956.85", "a" * 40,
                                          started_at=START, signal_as_of="2026-10-29", **kwargs)

    def write_raw(self, category, day, value):
        path = Path(self.temp.name) / self.store.prefix / category / f"{day}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_pending_intent_survives_restart_and_is_account_and_mode_scoped(self):
        original = self.begin()
        fresh = CoreRecoveryStore(ACCOUNT, True, environ=self.env)
        self.assertEqual(fresh.pending(MONDAY), original)
        self.assertEqual(fresh.pending(FRIDAY), original)
        self.assertIsNone(CoreRecoveryStore(ACCOUNT, False, environ=self.env).pending(MONDAY))
        self.assertIsNone(CoreRecoveryStore("other", True, environ=self.env).pending(MONDAY))
        document = next(Path(self.temp.name).rglob("*.json")).read_text()
        self.assertNotIn(ACCOUNT, document)
        self.assertIn(HASH, document)

    def test_begin_is_immutable_idempotent_and_conflicting_snapshot_blocks(self):
        original = self.begin()
        self.assertEqual(self.begin(), original)
        changed = {"SPY": NS(symbol="SPY", qty="1")}
        with self.assertRaisesRegex(StateError, "conflicts"):
            self.store.begin(FRIDAY, OFF, changed, "3956.85", "a" * 40,
                             started_at=START, signal_as_of="2026-10-29")
        self.assertEqual(self.store.pending(MONDAY), original)

    def test_finish_only_when_actual_allocation_reconciles(self):
        original = self.begin()
        self.assertFalse(self.store.finish(original, MONDAY, {}, {}, 4000))
        positions = {"SGOV": NS(symbol="SGOV", qty="28")}
        self.assertFalse(self.store.finish(original, MONDAY, positions, {"SGOV": 100}, 4000,
                                          frozen=True))
        self.assertFalse(self.store.finish(original, MONDAY, positions, {"SGOV": 100}, 4000,
                                          protected={"SGOV"}))
        self.assertTrue(self.store.finish(original, MONDAY, positions, {"SGOV": 100}, 4000))
        self.assertIsNone(self.store.pending(MONDAY))
        self.assertTrue(self.store.finish(original, MONDAY, positions, {"SGOV": 100}, 4000))

    def test_finish_does_not_trust_caller_mutated_intent(self):
        original = self.begin()
        original["equity"] = "4001"
        with self.assertRaisesRegex(StateError, "durable intent"):
            self.store.finish(original, MONDAY, {"SGOV": NS(qty=28)}, {"SGOV": 100}, 4000)

    def test_future_and_expired_intents_fail_closed(self):
        self.begin()
        for day in (FRIDAY - timedelta(days=1), FRIDAY + timedelta(days=MAX_RECOVERY_DAYS+1)):
            with self.subTest(day=day), self.assertRaises(StateError):
                self.store.pending(day)

    def test_dry_run_cannot_create_or_complete_but_can_read(self):
        original = self.begin()
        dry = CoreRecoveryStore(ACCOUNT, True, environ=self.env, dry_run=True)
        self.assertEqual(dry.pending(MONDAY), original)
        before = sorted(p.read_bytes() for p in Path(self.temp.name).rglob("*.json"))
        with self.assertRaisesRegex(StateError, "Dry run"):
            self.begin(dry)
        with self.assertRaisesRegex(StateError, "Dry run"):
            dry.finish(original, MONDAY, {"SGOV": NS(qty=28)}, {"SGOV": 100}, 4000)
        self.assertEqual(before, sorted(p.read_bytes() for p in Path(self.temp.name).rglob("*.json")))

    def test_new_weekly_mandate_atomically_supersedes_reconciled_old_one(self):
        original = self.begin()
        next_week = date(2026, 11, 6)
        kwargs = dict(started_at=datetime(2026, 11, 6, 15, 45, tzinfo=NY), signal_as_of="2026-11-05")
        with self.assertRaisesRegex(StateError, "Earlier core intent"):
            self.store.begin(next_week, ON, {}, 4000, **kwargs)
        replacement = self.store.begin(next_week, ON, {}, 4000, supersedes=original, **kwargs)
        self.assertEqual(replacement["supersedes"]["digest"], intent_digest(original))
        self.assertEqual(self.store.pending(next_week), replacement)
        self.assertFalse((Path(self.temp.name) / self.store.prefix / "completed").exists())

    def test_multiple_unrelated_intents_cannot_be_silently_ignored(self):
        self.begin()
        another = intent(session_date="2026-11-02", started_at="2026-11-02T15:45:00-05:00",
                         signal_as_of="2026-10-30")
        self.write_raw("intents", MONDAY, another)
        with self.assertRaisesRegex(StateError, "Multiple unrelated"):
            self.store.pending(MONDAY)

    def test_scope_schema_and_predecessor_corruption_rejected(self):
        for field, value in (("account_hash", "b"*16), ("mode", "live"), ("schema", 2), ("schema", True),
                             ("targets", {"SGOV": ".80"}), ("initial_core_qty", {}),
                             ("signal_as_of", "2026-10-30"), ("source_revision", "bad")):
            with self.subTest(field=field):
                self.write_raw("intents", FRIDAY, intent(**{field:value}))
                with self.assertRaises(StateError):
                    self.store.pending(MONDAY)

    def test_completion_missing_intent_or_false_allocations_are_rejected(self):
        self.write_raw("completed", FRIDAY, {})
        with self.assertRaisesRegex(StateError, "no corresponding intent"):
            self.store.pending(MONDAY)
        original = self.begin_after_removing_invalid_completion()
        self.store.finish(original, MONDAY, {"SGOV": NS(qty=28)}, {"SGOV": 100}, 4000)
        path = Path(self.temp.name) / self.store.prefix / "completed" / f"{FRIDAY}.json"
        completed = json.loads(path.read_text())
        completed["core_qty"]["SGOV"] = "0"
        path.write_text(json.dumps(completed))
        with self.assertRaisesRegex(StateError, "allocation"):
            self.store.pending(MONDAY)

    def begin_after_removing_invalid_completion(self):
        # Only this test's known temporary artifact, never a deployed state file.
        (Path(self.temp.name) / self.store.prefix / "completed" / f"{FRIDAY}.json").unlink()
        return self.begin()

    def test_import_requires_verified_source_evidence(self):
        with self.assertRaisesRegex(StateError, "provenance"):
            self.begin(origin="verified_import")
        evidence = {"artifact_id": 13006682237, "digest": "b" * 64}
        imported = self.begin(origin="verified_import", source_evidence=evidence)
        self.assertEqual(imported["source_evidence"], evidence)

    def test_corrupt_partial_json_never_looks_like_no_pending_intent(self):
        self.begin()
        path = next(Path(self.temp.name).rglob("*.json"))
        for payload in (b"{", b'{"schema":1,"schema":1}', b'{"bad":NaN}'):
            with self.subTest(payload=payload):
                path.write_bytes(payload)
                with self.assertRaises(StateError):
                    self.store.pending(MONDAY)

    def test_read_errors_and_relative_local_directory_rejected(self):
        with self.assertRaises(StateError):
            CoreRecoveryStore(ACCOUNT, True, environ={"EOD_STATE_DIR":"relative"})
        with patch.object(Path, "iterdir", side_effect=PermissionError("private path")):
            with self.assertRaisesRegex(StateError, "lookup failed"):
                self.store.pending(MONDAY)


class RecoveryDecisionTests(unittest.TestCase):
    def test_fresh_matching_signal_can_resume_only_later_day(self):
        self.assertIsNone(select_recovery_decision(intent(), OFF, FRIDAY))
        self.assertIs(select_recovery_decision(intent(), OFF, MONDAY), OFF)
        self.assertIsNone(select_recovery_decision(intent(), ON, MONDAY))
        on = intent(regime="RISK-ON", targets={"SPY":".35", "QQQ":".35"})
        self.assertIs(select_recovery_decision(on, ON, MONDAY), ON)
        alternate = CoreDecision("RISK-ON", {"SPY":.35, "GLD":.35}, {}, ())
        self.assertIsNone(select_recovery_decision(on, alternate, MONDAY))
        self.assertIsNone(select_recovery_decision(on, OFF, MONDAY))

    def test_allocation_band_and_protected_positions(self):
        self.assertFalse(allocation_reconciled(intent(), {"SGOV":NS(qty="26.8")}, {"SGOV":100}, 4000))
        self.assertTrue(allocation_reconciled(intent(), {"SGOV":NS(qty="26.800000001")}, {"SGOV":100}, 4000))
        with self.assertRaises(StateError):
            allocation_reconciled(intent(), {"SGOV":NS(qty=28)}, {}, 4000)
        with self.assertRaises(StateError):
            core_quantities({"SPY":NS(symbol="QQQ",qty=1)})


class RecoveryReconciliationTests(unittest.TestCase):
    def test_friday_fills_and_current_positions_reconcile_before_monday_resume(self):
        orders = [receipt("SPY"),receipt("QQQ")]
        result = reconcile_intent(intent(), orders, [activity(o) for o in orders],
                                  {"COST": NS(symbol="COST",qty=".6")}, MONDAY)
        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["fill_count"], 2)
        self.assertTrue(all(Decimal(q) == 0 for q in result["core_qty"].values()))

    def test_multiple_recovery_days_accumulate_exact_quantity_receipts(self):
        orders = [receipt("SPY"), receipt("QQQ"), receipt("SGOV", side="buy",qty="27.7",day=MONDAY)]
        result = reconcile_intent(intent(), orders,[activity(o) for o in orders],
                                  {"SGOV":NS(qty="27.7")}, date(2026,11,3))
        self.assertEqual(result["core_qty"]["SGOV"], "27.7")

    def test_partial_open_external_or_ambiguous_orders_block(self):
        for changes in ({"status":"new","filled_qty":"0"},
                        {"status":"partially_filled","filled_qty":".5"},
                        {"status":"canceled","filled_qty":".5"},
                        {"client_order_id":"manual-order"},
                        {"submitted_at":"2026-10-29T19:45:00Z"},
                        {"filled_qty":None}, {"filled_avg_price":"0"},
                        {"qty":"99"}, {"filled_at":"2026-10-30T14:00:00Z"}):
            with self.subTest(changes=changes), self.assertRaises(SafetyError):
                o = receipt(**changes)
                reconcile_intent(intent(), [o], [activity(o)], {}, MONDAY)

    def test_zero_fill_terminal_order_can_reconcile_without_replay(self):
        o = receipt(status="rejected", filled_qty="0", filled_at=None)
        result = reconcile_intent(intent(), [o], [], POSITIONS, MONDAY)
        self.assertEqual(result["fill_count"], 0)

    def test_missing_duplicate_mismatched_or_external_fills_block(self):
        orders = [receipt("SPY"), receipt("QQQ")]
        good = [activity(o) for o in orders]
        cases = [good[:1], good + [good[0]], [dict(good[0], order_id="unknown"), good[1]],
                 [dict(good[0], qty=".1"),good[1]], [dict(good[0],side="buy"),good[1]],
                 [dict(good[0],transaction_time="2026-11-03T15:45:00-05:00"),good[1]],
                 [dict(good[0],transaction_time=START+timedelta(seconds=5)),good[1]],
                 [dict(good[0],transaction_time=START+timedelta(seconds=15)),good[1]]]
        for fills in cases:
            with self.subTest(fills=fills), self.assertRaises(SafetyError):
                reconcile_intent(intent(), orders, fills, {}, MONDAY)
        with self.assertRaises(SafetyError):
            reconcile_intent(intent(), orders+[orders[0]], good, {}, MONDAY)

    def test_position_discrepancy_and_out_of_range_history_block(self):
        orders=[receipt("SPY"),receipt("QQQ")]
        for positions in ({"SPY":NS(qty=".1")},{"SGOV":NS(qty=1)}):
            with self.subTest(positions=positions), self.assertRaises(SafetyError):
                reconcile_intent(intent(),orders,[activity(o) for o in orders],positions,MONDAY)
        for day in (FRIDAY-timedelta(days=1), FRIDAY+timedelta(days=MAX_RECOVERY_DAYS+1)):
            with self.subTest(day=day),self.assertRaises(SafetyError):
                reconcile_intent(intent(), [], [], POSITIONS, day)

    def test_non_core_orders_do_not_pollute_core_reconciliation(self):
        swing=receipt("COST",qty=".6")
        self.assertEqual(reconcile_intent(intent(),[swing],[activity(swing)],POSITIONS,MONDAY)["fill_count"],0)


class RecoveryGithubTests(unittest.TestCase):
    def test_missing_state_read_is_readonly(self):
        session=Session([Response(404),Response(404)])
        store=CoreRecoveryStore(ACCOUNT,True,environ=ENV,session=session)
        self.assertIsNone(store.pending(MONDAY))
        self.assertEqual([c[0] for c in session.calls],["GET","GET"])
        for method,url,kwargs in session.calls:
            self.assertTrue(url.startswith("https://api.github.com/repos/owner/TradingEOD/contents/"))
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"],(5,20))
            self.assertEqual(kwargs["params"],{"ref":"eod-state"})

    def test_github_payload_scope_checked_and_read_errors_sanitized(self):
        prefix=f"core-recovery/paper/{HASH}/intents"
        listing=[{"type":"file","name":f"{FRIDAY}.json","path":f"{prefix}/{FRIDAY}.json"}]
        encoded={"type":"file","encoding":"base64","content":base64.b64encode(json.dumps(intent()).encode()).decode()}
        session=Session([Response(200,listing),Response(404),Response(200,encoded)])
        store=CoreRecoveryStore(ACCOUNT,True,environ=ENV,session=session)
        self.assertEqual(store.pending(MONDAY),intent())
        for response in (Response(403),requests.Timeout("secret token URL"),Response(200,ValueError())):
            with self.subTest(response=response):
                store=CoreRecoveryStore(ACCOUNT,True,environ=ENV,session=Session([response]))
                with self.assertRaises(StateError) as caught:
                    store.pending(MONDAY)
                self.assertNotIn("secret",str(caught.exception))

    def test_github_begin_atomic_create_and_no_raw_account_in_payload(self):
        session=Session([Response(404),Response(404),Response(200),Response(404),Response(201)])
        store=CoreRecoveryStore(ACCOUNT,True,environ=ENV,session=session)
        row=store.begin(FRIDAY,OFF,POSITIONS,"3956.85",started_at=START,signal_as_of="2026-10-29")
        method,url,kwargs=session.calls[-1]
        self.assertEqual(method,"PUT")
        self.assertNotIn("sha",kwargs["json"])
        decoded=base64.b64decode(kwargs["json"]["content"]).decode()
        self.assertNotIn(ACCOUNT,decoded)
        self.assertEqual(json.loads(decoded),row)

    def test_invalid_environment_and_saturated_directory_fail_closed(self):
        for key,value in (("GITHUB_TOKEN",""),("GITHUB_ACTIONS","maybe"),("GITHUB_SHA","bad"),
                          ("GITHUB_REPOSITORY","owner/repo?token=x")):
            with self.subTest(key=key),self.assertRaises(StateError):
                CoreRecoveryStore(ACCOUNT,True,environ={**ENV,key:value},session=Session([]))
        with self.assertRaisesRegex(StateError,"saturated"):
            CoreRecoveryStore(ACCOUNT,True,environ=ENV,session=Session([Response(200,[{}]*1000)])).pending(MONDAY)


if __name__ == "__main__":
    unittest.main()
