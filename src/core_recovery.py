"""Durable, immutable core intent journal and read-only recovery reconciliation.

This module never submits or cancels orders. An intent is not permission to
trade: the caller must enforce the current Calendar/Clock window, account and
quote checks, anti-PDT rules and the independent atomic daily execution claim.
Record intent before submissions. After interruption, only a later valid session
may reconcile broker orders/FILL activities and use a freshly revalidated signal.
"""
from __future__ import annotations

import base64
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re

import requests

from .safety import NY, SafetyError, aware
from .state import REQUEST_TIMEOUT, STATE_BRANCH, StateError, _claim_marker, _validated_inputs
from .strategy import DEFENSIVE_SYMBOL, OFFENSIVE_UNIVERSE


CORE_SYMBOLS = (*OFFENSIVE_UNIVERSE, DEFENSIVE_SYMBOL)
MAX_RECOVERY_DAYS = 35
MAX_DIRECTORY_ENTRIES = 1000
QTY_TOLERANCE = Decimal("0.000000001")
ZERO = Decimal(0)


def _day(value):
    if isinstance(value, datetime):
        raise StateError("Core recovery requires a calendar date")
    text = value.isoformat() if isinstance(value, date) else str(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise StateError("Invalid core recovery date")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise StateError("Invalid core recovery date") from None


def _number(value, *, positive=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or (result <= 0 if positive else result < 0):
            raise ValueError
        return result
    except (ArithmeticError, TypeError, ValueError):
        raise StateError("Invalid core recovery numeric value") from None


def _get(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _value(value):
    return str(getattr(value, "value", value)).lower()


def _canonical(value):
    try:
        return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError):
        raise StateError("Invalid core recovery document") from None


def intent_digest(intent):
    return hashlib.sha256(_canonical(intent)).hexdigest()


def core_quantities(positions):
    result = {}
    for symbol in CORE_SYMBOLS:
        position = positions.get(symbol)
        if position is None:
            result[symbol] = "0"
        else:
            if _get(position, "symbol", symbol) != symbol:
                raise StateError("Core position identity mismatch")
            result[symbol] = str(_number(_get(position, "qty")))
    return result


def _targets(regime, targets):
    if not isinstance(targets, dict):
        raise StateError("Malformed core targets")
    result = {symbol: str(_number(weight, positive=True)) for symbol, weight in targets.items()}
    if regime == "RISK-OFF":
        valid = set(result) == {DEFENSIVE_SYMBOL} and _number(result[DEFENSIVE_SYMBOL]) == Decimal(".70")
    elif regime == "RISK-ON":
        valid = (len(result) == 2 and set(result) <= set(OFFENSIVE_UNIVERSE)
                 and all(_number(weight) == Decimal(".35") for weight in result.values()))
    else:
        valid = False
    if not valid:
        raise StateError("Core recovery target differs from strategy allocation")
    return result


def _validate_intent(intent, account_hash=None, mode=None):
    required = {"schema", "account_hash", "mode", "session_date", "started_at", "signal_as_of",
                "regime", "targets", "initial_core_qty", "equity", "source_revision", "origin",
                "source_evidence", "supersedes"}
    if (not isinstance(intent, dict) or set(intent) != required
            or type(intent["schema"]) is not int or intent["schema"] != 1):
        raise StateError("Malformed core intent schema")
    if not re.fullmatch(r"[0-9a-f]{16}", str(intent["account_hash"])):
        raise StateError("Invalid core intent account scope")
    if intent["mode"] not in {"paper", "live"}:
        raise StateError("Invalid core intent mode")
    if account_hash is not None and intent["account_hash"] != account_hash:
        raise StateError("Core intent account mismatch")
    if mode is not None and intent["mode"] != mode:
        raise StateError("Core intent mode mismatch")
    day = _day(intent["session_date"])
    try:
        started = aware(intent["started_at"]).astimezone(NY)
    except (TypeError, ValueError, SafetyError):
        raise StateError("Invalid core intent start timestamp") from None
    if started.date() != day or _day(intent["signal_as_of"]) >= day:
        raise StateError("Core intent signal or start date is not causal")
    _targets(intent["regime"], intent["targets"])
    quantities = intent["initial_core_qty"]
    if not isinstance(quantities, dict) or set(quantities) != set(CORE_SYMBOLS):
        raise StateError("Incomplete initial core quantities")
    for value in quantities.values():
        _number(value)
    _number(intent["equity"], positive=True)
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}|local", str(intent["source_revision"])):
        raise StateError("Invalid core intent source revision")
    if intent["origin"] not in {"weekly", "verified_import"}:
        raise StateError("Invalid core intent origin")
    evidence = intent["source_evidence"]
    if evidence is not None:
        if (not isinstance(evidence, dict) or set(evidence) != {"artifact_id", "digest"}
                or isinstance(evidence["artifact_id"], bool) or not isinstance(evidence["artifact_id"], int)
                or evidence["artifact_id"] <= 0
                or not re.fullmatch(r"[0-9a-f]{64}", str(evidence["digest"]))):
            raise StateError("Malformed core import provenance")
    if intent["origin"] == "verified_import" and evidence is None:
        raise StateError("Core import requires verified provenance")
    predecessor = intent["supersedes"]
    if predecessor is not None:
        if (not isinstance(predecessor, dict) or set(predecessor) != {"session_date", "digest"}
                or _day(predecessor["session_date"]) >= day
                or not re.fullmatch(r"[0-9a-f]{64}", str(predecessor["digest"]))):
            raise StateError("Malformed core predecessor")
    return intent


def build_intent(account_hash, mode, day, decision, positions, equity, *, started_at,
                 signal_as_of, revision="local", origin="weekly", source_evidence=None,
                 supersedes=None):
    """Build an account-hashed document; caller owns verification of imported evidence."""
    result = {
        "schema": 1, "account_hash": account_hash, "mode": mode,
        "session_date": _day(day).isoformat(), "started_at": aware(started_at).isoformat(),
        "signal_as_of": _day(signal_as_of).isoformat(), "regime": decision.regime,
        "targets": _targets(decision.regime, decision.targets),
        "initial_core_qty": core_quantities(positions), "equity": str(_number(equity, positive=True)),
        "source_revision": revision, "origin": origin, "source_evidence": source_evidence,
        "supersedes": None if supersedes is None else {
            "session_date": supersedes["session_date"], "digest": intent_digest(supersedes),
        },
    }
    return _validate_intent(result, account_hash, mode)


def select_recovery_decision(intent, fresh_decision, day):
    """Return the fresh decision only when it still matches the pending mandate.

    Changed signals hold the core for review/new weekly supersession. This does
    not silently activate daily defensive rules, nor unrelated risk-on entries.
    """
    _validate_intent(intent)
    today, origin = _day(day), _day(intent["session_date"])
    if today < origin or (today - origin).days > MAX_RECOVERY_DAYS:
        raise StateError("Core recovery intent is future-dated or expired")
    if today == origin:
        return None
    targets = _targets(fresh_decision.regime, fresh_decision.targets)
    if (fresh_decision.regime != intent["regime"] or set(targets) != set(intent["targets"])
            or any(_number(targets[s]) != _number(intent["targets"][s]) for s in targets)):
        return None
    return fresh_decision


def reconcile_intent(intent, orders, fills, positions, through_day):
    """Certify prior core activity and holdings from COMPLETE broker query results.

    Caller must query all orders and FILL activities from intent session through
    through_day, including orders submitted earlier that filled in that range.
    Any non-bot core activity, unresolved/partial order, missing receipt, repeated
    identity or position discrepancy is a hard stop. Non-core activity is ignored.
    This proves quantities only; the caller must refresh account funds/quotes.
    """
    _validate_intent(intent)
    today, first_day = _day(through_day), _day(intent["session_date"])
    if today < first_day or (today - first_day).days > MAX_RECOVERY_DAYS:
        raise SafetyError("Core recovery history outside bounded date range")
    started = aware(intent["started_at"])
    relevant, clients, activities, sums = {}, set(), set(), {}
    expected = {s: _number(q) for s, q in intent["initial_core_qty"].items()}
    try:
        for order in orders:
            symbol = _get(order, "symbol")
            if symbol not in CORE_SYMBOLS:
                continue
            order_id, client_id = str(_get(order, "id", "")), _get(order, "client_order_id")
            side, status = _value(_get(order, "side")), _value(_get(order, "status"))
            submitted = aware(_get(order, "submitted_at")).astimezone(NY)
            if (not order_id or order_id == "None" or order_id in relevant or client_id in clients
                    or side not in {"buy", "sell"} or not first_day <= submitted.date() <= today
                    or submitted < started
                    or client_id != f"eod-v1-{submitted:%Y%m%d}-{side[0]}-{symbol.replace('.', '')}"):
                raise SafetyError("Core recovery found unknown or duplicate order identity")
            filled = _number(_get(order, "filled_qty"))
            if status == "filled":
                filled_at = aware(_get(order, "filled_at")).astimezone(NY)
                if filled <= 0 or filled_at < submitted or filled_at.date() > today:
                    raise SafetyError("Core recovery found inconsistent fill timestamp or quantity")
                # Alpaca can record a fractional leg after filled_at but before
                # updated_at on an order already marked fully filled. Bound the
                # activity by the verified order snapshot, not its first fill.
                updated_raw = _get(order, "updated_at")
                evidence_until = aware(updated_raw).astimezone(NY) if updated_raw is not None else filled_at
                if evidence_until < filled_at or evidence_until.date() > today:
                    raise SafetyError("Core recovery found inconsistent order update timestamp")
                _number(_get(order, "filled_avg_price"), positive=True)
                if side == "sell" and abs(_number(_get(order, "qty")) - filled) > QTY_TOLERANCE:
                    raise SafetyError("Core recovery sell was not fully filled")
            elif status not in {"canceled", "expired", "rejected"} or filled != 0:
                raise SafetyError("Core recovery found unresolved or partially filled order")
            relevant[order_id] = (symbol, side, filled, submitted,
                                  evidence_until if status == "filled" else None)
            clients.add(client_id)
            sums[order_id] = ZERO
        for fill in fills:
            symbol = _get(fill, "symbol")
            if symbol not in CORE_SYMBOLS:
                continue
            activity_id, order_id = _get(fill, "id"), str(_get(fill, "order_id", ""))
            side = _value(_get(fill, "side"))
            stamp = aware(_get(fill, "transaction_time")).astimezone(NY)
            if (not isinstance(activity_id, str) or not activity_id or activity_id in activities
                    or order_id not in relevant or relevant[order_id][:2] != (symbol, side)
                    or not first_day <= stamp.date() <= today or stamp < started):
                raise SafetyError("Core recovery FILL identity does not match verified orders")
            if (stamp < relevant[order_id][3] or relevant[order_id][4] is None
                    or stamp > relevant[order_id][4]):
                raise SafetyError(f"Core recovery FILL timestamp outside verified order snapshot: {symbol}")
            qty = _number(_get(fill, "qty"), positive=True)
            sums[order_id] += qty
            expected[symbol] += qty if side == "buy" else -qty
            activities.add(activity_id)
        for order_id, (_, _, filled, _, _) in relevant.items():
            if abs(sums[order_id] - filled) > QTY_TOLERANCE:
                raise SafetyError("Core recovery orders and FILL quantities disagree")
        actual = core_quantities(positions)
        for symbol in CORE_SYMBOLS:
            if expected[symbol] < -QTY_TOLERANCE or abs(_number(actual[symbol]) - expected[symbol]) > QTY_TOLERANCE:
                raise SafetyError("Core recovery positions do not reconcile with prior fills")
    except (TypeError, ValueError, AttributeError, StateError):
        raise SafetyError("Core recovery broker evidence is incomplete or malformed") from None
    return {"status": "reconciled", "through_day": today.isoformat(),
            "intent_date": first_day.isoformat(), "order_ids": sorted(relevant),
            "fill_count": len(activities), "core_qty": actual}


def allocation_reconciled(intent, positions, prices, equity, *, protected=(), frozen=False,
                          band=Decimal(".03")):
    _validate_intent(intent)
    if frozen or set(protected) & set(CORE_SYMBOLS):
        return False
    total, tolerance = _number(equity, positive=True), _number(band, positive=True)
    if tolerance != Decimal(".03"):
        raise StateError("Core completion must retain the configured tolerance band")
    quantities = core_quantities(positions)
    for symbol in CORE_SYMBOLS:
        qty, target = _number(quantities[symbol]), _number(intent["targets"].get(symbol, 0))
        if qty:
            price = prices.get(symbol)
            if hasattr(price, "price"):
                price = price.price
            current = qty * _number(price, positive=True) / total
        else:
            current = ZERO
        if abs(current - target) >= tolerance:
            return False
    return True


class CoreRecoveryStore:
    """Immutable local/GitHub state; no raw account ID or credentials in payloads.

    begin()/finish() are forbidden for dry-run instances. Reads remain available
    to simulations. Existing documents are never overwritten or deleted.
    """
    def __init__(self, account_id, paper, *, environ=None, session=None, dry_run=False):
        self.environ = os.environ if environ is None else environ
        self.account_hash, _, self.mode = _validated_inputs(account_id, date(2000, 1, 1), paper)
        self.session, self.dry_run = session, dry_run
        if not isinstance(dry_run, bool):
            raise StateError("Core recovery dry-run flag must be boolean")
        flag = self.environ.get("GITHUB_ACTIONS", "").lower()
        if flag not in {"", "false", "true"}:
            raise StateError("Invalid GITHUB_ACTIONS environment flag")
        self.github = flag == "true"
        self.prefix = f"core-recovery/{self.mode}/{self.account_hash}"
        if self.github:
            repo, revision, token = (self.environ.get(k, "") for k in ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_TOKEN"))
            if (not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*", repo)
                    or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision)
                    or not token.strip() or any(c.isspace() for c in token)):
                raise StateError("Invalid GitHub environment for core recovery")
            self.base = f"https://api.github.com/repos/{repo}"
            self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28"}
        else:
            raw = self.environ.get("EOD_STATE_DIR", "")
            if not raw or not Path(raw).is_absolute():
                raise StateError("Local core recovery requires an absolute EOD_STATE_DIR")
            self.root = Path(raw)

    def _request(self, path):
        try:
            kwargs = dict(headers=self.headers, timeout=REQUEST_TIMEOUT, allow_redirects=False,
                          params={"ref": STATE_BRANCH})
            if self.session is not None:
                response = self.session.request("GET", f"{self.base}/contents/{path}", **kwargs)
            else:
                with requests.Session() as session:
                    response = session.request("GET", f"{self.base}/contents/{path}", **kwargs)
        except requests.RequestException as exc:
            raise StateError(f"Core recovery lookup failed ({type(exc).__name__})") from None
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise StateError(f"Core recovery lookup failed (HTTP {response.status_code})")
        try:
            return response.json()
        except (TypeError, ValueError):
            raise StateError("Malformed GitHub core recovery response") from None

    def _list(self, category):
        path = f"{self.prefix}/{category}"
        if self.github:
            listing = self._request(path)
            if listing is None:
                return []
            if not isinstance(listing, list) or len(listing) >= MAX_DIRECTORY_ENTRIES:
                raise StateError("Core recovery directory is malformed or saturated")
            names = []
            for row in listing:
                if (not isinstance(row, dict) or row.get("type") != "file"
                        or row.get("path") != f"{path}/{row.get('name')}"):
                    raise StateError("Unexpected core recovery directory entry")
                names.append(row.get("name"))
        else:
            directory = self.root / path
            try:
                if directory.is_symlink():
                    raise StateError("Core recovery directory may not be a symlink")
                names = []
                for entry in directory.iterdir():
                    if not entry.is_file() or entry.is_symlink():
                        raise StateError("Unexpected local core recovery entry")
                    names.append(entry.name)
                if len(names) >= MAX_DIRECTORY_ENTRIES:
                    raise StateError("Core recovery directory is saturated")
            except FileNotFoundError:
                return []
            except OSError:
                raise StateError("Core recovery local directory lookup failed") from None
        if len(set(names)) != len(names):
            raise StateError("Duplicate core recovery directory entry")
        for name in names:
            if not isinstance(name, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", name):
                raise StateError("Unexpected core recovery filename")
            _day(name[:-5])
        return sorted(names)

    def _read(self, category, day):
        path = f"{self.prefix}/{category}/{_day(day).isoformat()}.json"
        if self.github:
            row = self._request(path)
            if row is None:
                return None
            if not isinstance(row, dict) or row.get("type") != "file" or row.get("encoding") != "base64":
                raise StateError("Malformed core recovery content response")
            try:
                payload = base64.b64decode("".join(row["content"].split()), validate=True)
            except (KeyError, TypeError, ValueError):
                raise StateError("Invalid core recovery content encoding") from None
        else:
            file = self.root / path
            try:
                if file.is_symlink():
                    raise StateError("Core recovery file may not be a symlink")
                payload = file.read_bytes()
            except FileNotFoundError:
                return None
            except OSError:
                raise StateError("Core recovery local read failed") from None
        if len(payload) > 65536:
            raise StateError("Core recovery document exceeds size limit")
        try:
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            return json.loads(payload.decode("utf-8"), object_pairs_hook=pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise StateError("Invalid core recovery JSON document") from None

    def _put(self, category, day, document):
        if self.dry_run:
            raise StateError("Dry run cannot mutate core recovery state")
        day = _day(day).isoformat()
        path = f"{self.prefix}/{category}/{day}.json"
        created = _claim_marker(self.environ, self.session, path, _canonical(document), day,
                                self.mode, claim_label=f"core {category}")
        if not created:
            existing = self._read(category, day)
            if existing != document:
                raise StateError("Existing core recovery record conflicts with proposed record")

    def pending(self, today):
        """Read latest unresolved mandate; never infer success from a missing record."""
        today = _day(today)
        names, finished_names = self._list("intents"), self._list("completed")
        if set(finished_names) - set(names):
            raise StateError("Core completion has no corresponding intent")
        intents, closed = {}, set()
        for name in names:
            day = _day(name[:-5])
            intent = self._read("intents", day)
            _validate_intent(intent, self.account_hash, self.mode)
            if intent["session_date"] != day.isoformat() or day > today:
                raise StateError("Core intent filename/date mismatch or future date")
            intents[day.isoformat()] = intent
        predecessors = set()
        for intent in intents.values():
            prior = intent["supersedes"]
            if prior:
                previous = intents.get(prior["session_date"])
                if (previous is None or intent_digest(previous) != prior["digest"]
                        or prior["session_date"] in predecessors):
                    raise StateError("Core supersession predecessor is missing or changed")
                predecessors.add(prior["session_date"])
                closed.add(prior["session_date"])
        for name in finished_names:
            key = name[:-5]
            completed = self._read("completed", key)
            required = {"schema", "account_hash", "mode", "intent_date", "intent_digest", "completed_on",
                        "core_qty", "equity", "prices"}
            if (not isinstance(completed, dict) or set(completed) != required
                    or type(completed["schema"]) is not int or completed["schema"] != 1
                    or completed["account_hash"] != self.account_hash or completed["mode"] != self.mode
                    or completed["intent_date"] != key or completed["intent_digest"] != intent_digest(intents[key])
                    or not _day(key) <= _day(completed["completed_on"]) <= today):
                raise StateError("Malformed or inconsistent core completion")
            qty = completed["core_qty"]
            if not isinstance(qty, dict) or set(qty) != set(CORE_SYMBOLS):
                raise StateError("Incomplete core completion quantities")
            positions = {s: {"symbol": s, "qty": q} for s, q in qty.items()}
            if not isinstance(completed["prices"], dict) or not allocation_reconciled(
                    intents[key], positions, completed["prices"], completed["equity"]):
                raise StateError("Core completion allocation is not reconciled")
            closed.add(key)
        active = [item for key, item in intents.items() if key not in closed]
        if len(active) > 1:
            raise StateError("Multiple unrelated pending core intents require reconciliation")
        if active and (today - _day(active[0]["session_date"])).days > MAX_RECOVERY_DAYS:
            raise StateError("Pending core intent expired; manual reconciliation required")
        return active[0] if active else None

    def begin(self, day, decision, positions, equity, revision=None, *, started_at, signal_as_of,
              supersedes=None, origin="weekly", source_evidence=None):
        if self.dry_run:
            raise StateError("Dry run cannot begin a core intent")
        day = _day(day)
        existing = self.pending(day)
        if supersedes is not None:
            _validate_intent(supersedes, self.account_hash, self.mode)
            if existing != supersedes or _day(supersedes["session_date"]) >= day:
                raise StateError("Core supersession requires the current earlier pending intent")
        elif existing is not None and existing["session_date"] != day.isoformat():
            raise StateError("Earlier core intent must be reconciled before a new mandate")
        intent = build_intent(self.account_hash, self.mode, day, decision, positions, equity,
                              started_at=started_at, signal_as_of=signal_as_of,
                              revision=revision or self.environ.get("GITHUB_SHA", "local"),
                              supersedes=supersedes, origin=origin, source_evidence=source_evidence)
        self._put("intents", day, intent)
        return intent

    def finish(self, intent, day, positions, prices, equity, *, protected=(), frozen=False):
        if self.dry_run:
            raise StateError("Dry run cannot complete a core intent")
        _validate_intent(intent, self.account_hash, self.mode)
        day = _day(day)
        if day < _day(intent["session_date"]) or (day - _day(intent["session_date"])).days > MAX_RECOVERY_DAYS:
            raise StateError("Core completion outside permitted recovery dates")
        if self._read("intents", intent["session_date"]) != intent:
            raise StateError("Core completion does not match durable intent")
        if not allocation_reconciled(intent, positions, prices, equity, protected=protected, frozen=frozen):
            return False
        qty = core_quantities(positions)
        used_prices = {}
        for symbol, quantity in qty.items():
            if _number(quantity):
                price = prices[symbol]
                if hasattr(price, "price"):
                    price = price.price
                used_prices[symbol] = str(_number(price, positive=True))
        document = {"schema": 1, "account_hash": self.account_hash, "mode": self.mode,
                    "intent_date": intent["session_date"], "intent_digest": intent_digest(intent),
                    "completed_on": day.isoformat(), "core_qty": qty,
                    "equity": str(_number(equity, positive=True)), "prices": used_prices}
        self._put("completed", intent["session_date"], document)
        return True
