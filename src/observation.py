"""Read-only paper quote audit. Never imports or invokes the trading engine.

Only GET requests are used: GitHub run artifacts, the paper clock, and delayed
historical SIP quotes. Downloaded artifacts are parsed as bounded JSON, never
extracted or executed. This evidence does not alter execution policy.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit
import zipfile
from zoneinfo import ZoneInfo

import requests

from .quotes import inspect_quote


MAX_BYTES = 4 * 1024 * 1024
MAX_SAMPLES = 300
MAX_WAIT_SECONDS = 20 * 60
DEFAULT_START = "2026-10-23"
NY = ZoneInfo("America/New_York")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)$")


class ObservationError(ValueError):
    """Only fixed, non-provider diagnostics may be used as messages."""


def instant_ns(value):
    """Compare nanoseconds without losing precision to Python datetime rounding."""
    if not isinstance(value, str) or not (match := TIMESTAMP.fullmatch(value)):
        raise ObservationError("Invalid observation timestamp")
    try:
        whole = datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00"))
        delta = whole.astimezone(timezone.utc) - EPOCH
        return (delta.days * 86400 + delta.seconds) * 10**9 + int((match[2] or "").ljust(9, "0"))
    except (ValueError, OverflowError):
        raise ObservationError("Invalid observation timestamp") from None


def iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\d", value):
        raise ObservationError("Invalid session or campaign date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ObservationError("Invalid session or campaign date") from None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _json(raw):
    if len(raw) > MAX_BYTES:
        raise ObservationError("JSON exceeds observation size limit")
    try:
        value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ObservationError("Invalid observation JSON") from None
    if not isinstance(value, dict):
        raise ObservationError("Observation JSON must be an object")
    return value


def _body(response):
    result = bytearray()
    try:
        declared = response.headers.get("Content-Length")
        if declared is not None and (not str(declared).isdigit() or int(declared) > MAX_BYTES):
            raise ObservationError("Response exceeds observation size limit")
        for chunk in response.iter_content(chunk_size=65536):
            result.extend(chunk)
            if len(result) > MAX_BYTES:
                raise ObservationError("Response exceeds observation size limit")
        return bytes(result)
    except requests.RequestException:
        raise ObservationError("Observation download failed") from None


def _get(session, url, *, headers=None, params=None, redirect=False):
    try:
        response = session.get(url, headers=headers or {}, params=params,
                               timeout=(3.05, 8), allow_redirects=False, stream=True)
    except requests.RequestException:
        raise ObservationError("Observation GET failed") from None
    try:
        if redirect and response.status_code == 302:
            return response.headers.get("Location", "")
        if response.status_code != 200:
            raise ObservationError("Observation GET returned an unsuccessful status")
        return _body(response)
    finally:
        response.close()


def parse_summary_zip(raw, *, filename="run_summary.json"):
    if len(raw) > MAX_BYTES:
        raise ObservationError("Artifact exceeds observation size limit")
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) != 1:
                raise ObservationError("Summary artifact must contain exactly one file")
            entry = entries[0]
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (entry.filename != filename or entry.is_dir()
                    or entry.flag_bits & 1 or mode not in (0, stat.S_IFREG)
                    or entry.file_size > MAX_BYTES or entry.compress_size > MAX_BYTES):
                raise ObservationError("Unsafe or oversized summary artifact")
            with archive.open(entry) as stream:
                return _json(stream.read(MAX_BYTES + 1))
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError):
        raise ObservationError("Invalid summary artifact archive") from None


def download_artifact(session, base, artifact, headers, *, filename):
    """Read one allowlisted JSON member; credentials never reach signed hosts."""
    aid, size = artifact.get("id"), artifact.get("size_in_bytes")
    if (artifact.get("expired") is not False or not isinstance(aid, int) or isinstance(aid, bool) or aid <= 0
            or not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BYTES):
        raise ObservationError("Invalid artifact identity or size")
    location = _get(session, f"{base}/actions/artifacts/{aid}/zip", headers=headers, redirect=True)
    if not isinstance(location, str):
        raise ObservationError("Artifact download did not return a signed location")
    try:
        parts = urlsplit(location)
        hostname = (parts.hostname or "").lower()
        valid = (parts.scheme == "https" and not parts.username and not parts.password
                 and not parts.fragment and parts.port in (None, 443)
                 and hostname.endswith((".blob.core.windows.net", ".actions.githubusercontent.com", ".githubusercontent.com")))
    except ValueError:
        valid = False
    if not valid:
        raise ObservationError("Artifact download host is not allowed")
    raw = _get(session, location)  # Deliberately never forward the GitHub token.
    digest = artifact.get("digest")
    if digest is not None and digest != "sha256:" + sha256(raw).hexdigest():
        raise ObservationError("Artifact digest verification failed")
    return parse_summary_zip(raw, filename=filename)


def fetch_summary(session, repo, run_id, token, run_attempt=""):
    """Return only the exact completed main-branch Daily EOD run's artifact."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[1-9]\d{0,19}", str(run_id)):
        raise ObservationError("Invalid repository or run identity")
    base = f"https://api.github.com/repos/{repo}"
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2022-11-28"}
    run_url = f"{base}/actions/runs/{run_id}"
    if run_attempt:
        if not re.fullmatch(r"[1-9]\d{0,2}", str(run_attempt)):
            raise ObservationError("Invalid triggering run attempt")
        run_url += f"/attempts/{run_attempt}"
    run = _json(_get(session, run_url, headers=headers))
    head_repository = run.get("head_repository")
    if (run.get("id") != int(run_id) or run.get("status") != "completed"
            or run.get("event") not in ("schedule", "workflow_dispatch")
            or run.get("head_branch") != "main"
            or not isinstance(head_repository, dict)
            or str(head_repository.get("full_name", "")).lower() != repo.lower()
            or str(run.get("path", "")).split("@")[0] != ".github/workflows/daily_trading.yml"
            or run.get("name") != "Daily EOD Trading"):
        return None, {"reason": "Source run is not an eligible completed main-branch run"}
    attempt = run.get("run_attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or not 1 <= attempt <= 1000:
        raise ObservationError("Invalid source run attempt")
    if run_attempt and attempt != int(run_attempt):
        raise ObservationError("Source attempt does not match triggering attempt")
    expected = f"eod-summary-{run_id}-{attempt}"
    matches = []
    for page in range(1, 11):
        listing = _json(_get(session, f"{base}/actions/runs/{run_id}/artifacts", headers=headers,
                            params={"per_page": 100, "page": page}))
        artifacts = listing.get("artifacts")
        if not isinstance(artifacts, list):
            raise ObservationError("Invalid artifact listing")
        matches.extend(item for item in artifacts if isinstance(item, dict) and item.get("name") == expected)
        if len(artifacts) < 100:
            break
    else:
        raise ObservationError("Artifact listing exceeds observation limit")
    if not matches:
        return None, {"reason": "Exact source summary artifact is unavailable"}
    if len(matches) != 1 or matches[0].get("expired") is not False:
        raise ObservationError("Source summary artifact is ambiguous or expired")
    artifact = matches[0]
    result = download_artifact(session, base, artifact, headers, filename="run_summary.json")
    return result, {"run_id": str(run_id), "run_attempt": attempt, "artifact_id": artifact["id"]}


def recorded_quote(diagnostic):
    """Copy only bounded scalars, never arbitrary artifact text or links."""
    result = {field: number(diagnostic.get(field)) for field in
              ("bid", "ask", "bid_size", "ask_size", "spread", "attempts", "age_seconds")}
    for field in ("timestamp", "checked_at"):
        stamp = diagnostic.get(field)
        try:
            instant_ns(stamp)
        except ObservationError:
            stamp = None
        result[field] = stamp
    cause = diagnostic.get("cause")
    result["cause"] = cause if cause is None or (isinstance(cause, str) and re.fullmatch(r"[a-z_]{1,40}", cause)) else "unknown"
    result["feed"] = diagnostic.get("feed") if diagnostic.get("feed") in ("iex", "sip") else "unknown"
    result["recovered"] = diagnostic.get("recovered") is True
    return result


def samples_from_summary(summary):
    day = iso_date(summary.get("session_date"))
    checks = summary.get("quote_checks", [])
    if not isinstance(checks, list) or len(checks) > MAX_SAMPLES:
        raise ObservationError("Invalid or excessive quote checks")
    samples, seen = [], set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("diagnostics"), dict):
            raise ObservationError("Malformed quote check")
        if not check["diagnostics"]:
            continue  # Empty fetches deliberately have no observation timestamp.
        outer = check.get("checked_at")
        outer_ns = instant_ns(outer) if outer is not None else None
        phase = check.get("phase")
        phase = phase if isinstance(phase, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,64}", phase) else "unknown"
        for symbol, diagnostic in check["diagnostics"].items():
            if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9.\-]{1,16}", symbol) or not isinstance(diagnostic, dict):
                raise ObservationError("Malformed symbol diagnostic")
            checked = diagnostic.get("checked_at", outer)
            checked_ns = instant_ns(checked)
            if ((outer_ns is not None and checked_ns > outer_ns)
                    or datetime.fromisoformat(checked).astimezone(NY).date() != day):
                raise ObservationError("Diagnostic check time does not match its session or envelope")
            if (symbol, checked_ns, phase) in seen:
                continue
            seen.add((symbol, checked_ns, phase))
            original = recorded_quote(diagnostic)
            history = diagnostic.get("attempt_history", [])
            if not isinstance(history, list) or len(history) > 4 or any(not isinstance(item, dict) for item in history):
                raise ObservationError("Invalid quote retry history")
            original["attempt_history"] = [recorded_quote(item) for item in history]
            for item in original["attempt_history"]:
                if (item["checked_at"] is None or instant_ns(item["checked_at"]) > checked_ns
                        or datetime.fromisoformat(item["checked_at"]).astimezone(NY).date() != day):
                    raise ObservationError("Quote retry time exceeds its recorded observation")
            original["first"] = original["attempt_history"][0] if history else None
            original["last"] = original["attempt_history"][-1] if history else None
            samples.append({"symbol": symbol, "phase": phase, "checked_at": checked, "recorded": original})
            if len(samples) > MAX_SAMPLES:
                raise ObservationError("Too many symbol observations")
    return samples


class PaperData:
    """No account, position, order, or notification endpoints exist here."""
    def __init__(self, session, key, secret):
        if not key or not secret:
            raise ObservationError("Missing paper market-data credentials")
        self.session = session
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def clock(self):
        value = _json(_get(self.session, "https://paper-api.alpaca.markets/v2/clock", headers=self.headers))
        instant_ns(value.get("timestamp"))
        return value["timestamp"]

    def last_sip(self, symbol, checked):
        start = (datetime.fromisoformat(checked) - timedelta(days=1)).isoformat()
        value = _json(_get(self.session, "https://data.alpaca.markets/v2/stocks/quotes", headers=self.headers,
                          params={"symbols": symbol, "feed": "sip", "start": start, "end": checked,
                                  "sort": "desc", "limit": 1, "asof": datetime.fromisoformat(checked).date().isoformat()}))
        rows = value.get("quotes", {})
        if not isinstance(rows, dict) or set(rows) - {symbol}:
            raise ObservationError("Historical SIP response has unexpected symbols")
        rows = rows.get(symbol, [])
        if rows is None:
            rows = []
        if not isinstance(rows, list) or len(rows) > 1:
            raise ObservationError("Historical SIP response has unexpected quote count")
        if not rows:
            return None
        quote = rows[0]
        if not isinstance(quote, dict) or instant_ns(quote.get("t")) > instant_ns(checked):
            raise ObservationError("Historical SIP quote exceeds its recorded check time")
        bid, ask = number(quote.get("bp")), number(quote.get("ap"))
        valid = bid is not None and ask is not None and 0 < bid <= ask
        bid_size, ask_size = number(quote.get("bs")), number(quote.get("as"))
        spread = (ask - bid) / (bid / 2 + ask / 2) if valid else None
        age = (instant_ns(checked) - instant_ns(quote["t"])) / 10**9
        _, cause, _ = inspect_quote(quote, datetime.fromisoformat(checked), feed="sip")
        # The shared validator has microsecond datetime precision; retain the
        # stricter nanosecond historical cutoff and 90-second age boundary.
        if age > 90:
            cause = "stale"
        return {"timestamp": quote["t"], "bid": bid, "ask": ask,
                "bid_size": bid_size, "ask_size": ask_size, "spread": spread,
                "age_seconds": age, "price_valid": valid,
                "execution_quality_valid": cause is None, "quality_cause": cause}


def wait_for_delayed_sip(samples, clock, *, sleeper=time.sleep, monotonic=time.monotonic):
    # A one-second margin avoids requesting the live edge of a delayed feed.
    eligible_ns = max(instant_ns(sample["checked_at"]) for sample in samples) + 901 * 10**9
    started = monotonic()
    while True:
        now = instant_ns(clock())
        if now >= eligible_ns:
            return
        elapsed = monotonic() - started
        remaining = (eligible_ns - now) / 10**9
        if not math.isfinite(elapsed) or elapsed < 0 or elapsed >= MAX_WAIT_SECONDS or remaining > MAX_WAIT_SECONDS - elapsed:
            raise ObservationError("Delayed SIP observation exceeds twenty-minute wait budget")
        sleeper(min(60, remaining, MAX_WAIT_SECONDS - elapsed))


def observe(environ, session, *, sleeper=time.sleep, monotonic=time.monotonic):
    start = iso_date(environ.get("EOD_OBSERVATION_START_DATE", DEFAULT_START))
    repo, run_id = environ.get("GITHUB_REPOSITORY", ""), environ.get("EOD_OBSERVATION_RUN_ID", "")
    report = {"schema_version": 1, "status": "skipped", "repository": repo,
              "campaign_start": start.isoformat(), "campaign_eligible": False, "comparisons": []}
    if environ.get("ALPACA_PAPER", "").strip().lower() != "true":
        return {**report, "reason": "Paper observation requires ALPACA_PAPER=true"}
    summary, identity = fetch_summary(session, repo, run_id, environ.get("GITHUB_TOKEN", ""),
                                     environ.get("EOD_OBSERVATION_RUN_ATTEMPT", ""))
    report.update(identity)
    if summary is None:
        return report
    if summary.get("mode") != "paper" or summary.get("dry_run") is not False or summary.get("status") == "skipped":
        return {**report, "reason": "Source summary is not an evaluated paper execution"}
    day = iso_date(summary.get("session_date"))
    report.update(mode="paper", session_date=day.isoformat(), session_key=f"{repo.lower()}/paper/{day}",
                  session_claimed=summary.get("session_claimed") is True,
                  source_status=summary.get("status") if summary.get("status") in ("completed", "degraded", "blocked", "error") else "unknown",
                  weekly_rebalance=summary.get("weekly_rebalance") is True,
                  core_evaluated=(summary.get("weekly_rebalance") is True
                                  and summary.get("regime") in ("RISK_ON", "RISK_OFF", "RISK-ON", "RISK-OFF")),
                  source_feed=summary.get("feed") if summary.get("feed") in ("iex", "sip") else "unknown")
    if day < start and environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        return {**report, "reason": "Session precedes the configured observation campaign"}
    samples = samples_from_summary(summary)
    orders = summary.get("orders", [])
    report["confirmed_fills"] = [
        {"symbol": order["symbol"], "side": order["side"]}
        for order in (orders[:50] if isinstance(orders, list) else [])
        if isinstance(order, dict) and order.get("status") == "filled"
        and order.get("side") in ("buy", "sell") and isinstance(order.get("symbol"), str)
        and re.fullmatch(r"[A-Z0-9.\-]{1,16}", order["symbol"])
    ]
    if not samples:
        if "quote_checks" in summary and summary.get("status") in ("completed", "degraded"):
            return {**report, "status": "observed", "campaign_eligible": day >= start,
                    "evidence_kind": "operational_only_no_quotes_required",
                    "reason": "Operational session recorded; no requested quotes to compare; no quote-quality evidence"}
        return {**report, "reason": "insufficient_quote_evidence"}
    report["evidence_kind"] = "quote_comparison"
    data = PaperData(session, environ.get("ALPACA_API_KEY", ""), environ.get("ALPACA_SECRET_KEY", ""))
    started = monotonic()
    try:
        wait_for_delayed_sip(samples, data.clock, sleeper=sleeper, monotonic=monotonic)
    except ObservationError:
        return {**report, "status": "incomplete", "reason": "Delayed SIP wait or paper clock unavailable"}
    cache = {}
    for sample in samples:
        if monotonic() - started > 25 * 60:
            report["comparisons"].append({**sample, "status": "unavailable", "reason": "Collection time budget exhausted"})
            break
        key = (sample["symbol"], sample["checked_at"])
        try:
            if key not in cache:
                cache[key] = data.last_sip(*key)
            sip = cache[key]
            compared = {**sample, "sip": sip, "status": "compared" if sip else "missing_sip"}
            old_bid, old_ask = sample["recorded"]["bid"], sample["recorded"]["ask"]
            if sip and sip["price_valid"] and old_bid is not None and old_ask is not None and 0 < old_bid <= old_ask:
                compared["midpoint_difference_bps"] = ((old_bid / 2 + old_ask / 2) / (sip["bid"] / 2 + sip["ask"] / 2) - 1) * 10000
            report["comparisons"].append(compared)
        except ObservationError:
            report["comparisons"].append({**sample, "status": "unavailable", "reason": "Historical SIP comparison unavailable"})
    complete = len(report["comparisons"]) == len(samples) and all(item["status"] != "unavailable" for item in report["comparisons"])
    report.update(status="observed" if complete else "incomplete", campaign_eligible=complete and day >= start)
    return report


def aggregate_reports(reports, *, start=DEFAULT_START):
    """Count observations, core decisions and fills as distinct evidence classes."""
    start_day = iso_date(start)
    sessions, runs, repositories = {}, set(), set()
    for report in reports:
        if (report.get("schema_version") != 1 or report.get("status") != "observed"
                or report.get("mode") != "paper" or report.get("campaign_eligible") is not True
                or not (report.get("comparisons") or report.get("evidence_kind") == "operational_only_no_quotes_required")):
            continue
        day = iso_date(report.get("session_date"))
        if day < start_day:
            continue
        repo = report.get("repository")
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ObservationError("Invalid campaign repository")
        repo = repo.lower()
        run = (repo, str(report.get("run_id")), report.get("run_attempt"))
        if run in runs:
            continue
        runs.add(run)
        repositories.add(repo)
        if len(repositories) > 1:
            raise ObservationError("Campaign cannot mix repositories or accounts")
        key = (repo, day.isoformat())
        sessions.setdefault(key, {"session_date": day.isoformat(), "run_ids": [], "comparisons": 0,
                                  "core_evaluated": False, "confirmed_fills": [], "source_statuses": [],
                                  "missing_sip": 0, "sip_quality_valid": 0, "recovered_quotes": 0})
        sessions[key]["run_ids"].append(str(report["run_id"]))
        sessions[key]["comparisons"] += len(report["comparisons"])
        sessions[key]["core_evaluated"] |= report.get("core_evaluated") is True
        sessions[key]["source_statuses"] = sorted(set(sessions[key]["source_statuses"]) | {str(report.get("source_status", "unknown"))})
        for item in report["comparisons"]:
            sessions[key]["missing_sip"] += item.get("status") == "missing_sip"
            sessions[key]["sip_quality_valid"] += isinstance(item.get("sip"), dict) and item["sip"].get("execution_quality_valid") is True
            sessions[key]["recovered_quotes"] += isinstance(item.get("recorded"), dict) and item["recorded"].get("recovered") is True
        for fill in report.get("confirmed_fills", []):
            if (isinstance(fill, dict) and fill.get("side") in ("buy", "sell")
                    and isinstance(fill.get("symbol"), str) and re.fullmatch(r"[A-Z0-9.\-]{1,16}", fill["symbol"])):
                safe_fill = {"symbol": fill["symbol"], "side": fill["side"]}
                if safe_fill not in sessions[key]["confirmed_fills"]:
                    sessions[key]["confirmed_fills"].append(safe_fill)
    rows = sorted(sessions.values(), key=lambda row: row["session_date"])
    core_dates = [row["session_date"] for row in rows if row["core_evaluated"]]
    bought, round_trips = {}, set()
    for row in rows:
        for fill in row["confirmed_fills"]:
            if fill["side"] == "buy":
                bought.setdefault(fill["symbol"], row["session_date"])
            elif fill["symbol"] in bought and bought[fill["symbol"]] < row["session_date"]:
                round_trips.add(fill["symbol"])
    return {"schema_version": 1, "campaign_start": start_day.isoformat(), "required_sessions": 10,
            "required_core_evaluations": 2, "distinct_sessions": len(rows),
            "core_evaluation_dates": core_dates, "distinct_core_evaluations": len(core_dates),
            "complete": len(rows) >= 10 and len(core_dates) >= 2,
            "quote_evidence_sessions": sum(row["comparisons"] > 0 for row in rows),
            "quote_readiness": "Separate assessment required; operation-only dates provide no quote-quality evidence",
            "execution_evidence": {"confirmed_buy_then_later_sell_symbols": sorted(round_trips),
                                   "has_buy_then_later_sell": bool(round_trips),
                                   "note": "Observed sessions and core decisions do not by themselves validate fills or profitability"},
            "sessions": rows}


def aggregate(paths, *, start=DEFAULT_START):
    """Combine explicitly supplied bounded JSON reports; never execute them."""
    reports = []
    for path in sorted(map(Path, paths)):
        if path.is_symlink() or path.stat().st_size > MAX_BYTES:
            raise ObservationError("Unsafe or oversized campaign report")
        reports.append(_json(path.read_bytes()))
    return aggregate_reports(reports, start=start)


def fetch_observations(session, repo, token, *, start=DEFAULT_START, monotonic=time.monotonic):
    """Retrieve bounded historical reports only from this repository's trusted observer.

    All artifact names, workflow identity, branch, repository and event are
    checked before reading a single JSON member. A forged artifact in another
    workflow, a fork or a PR cannot contribute evidence to the campaign.
    """
    start_day = iso_date(start)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ObservationError("Invalid observation repository")
    base = f"https://api.github.com/repos/{repo}"
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2022-11-28"}
    reports, verified_runs, artifacts = [], {}, []
    began = monotonic()
    def check_budget():
        elapsed = monotonic() - began
        if not math.isfinite(elapsed) or elapsed < 0 or elapsed >= 120:
            raise ObservationError("Campaign collection time budget exhausted")
    # Pagination is bounded; exhaustion is an incomplete audit, never success.
    for page in range(1, 11):
        check_budget()
        listing = _json(_get(session, f"{base}/actions/artifacts", headers=headers,
                            params={"per_page": 100, "page": page}))
        items = listing.get("artifacts")
        if not isinstance(items, list):
            raise ObservationError("Invalid campaign artifact listing")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            match = re.fullmatch(r"paper-observation-([1-9]\d{0,19})-([1-9]\d{0,19})-([1-9]\d{0,2})", item["name"])
            if not match:
                continue
            created = item.get("created_at")
            instant_ns(created)
            if datetime.fromisoformat(created).astimezone(NY).date() < start_day:
                continue
            if item.get("expired") is not False:
                raise ObservationError("Campaign observation artifact has expired")
            artifacts.append((item, match.groups()))
            if len(artifacts) > 100:
                raise ObservationError("Campaign observation count exceeds collection limit")
        if len(items) < 100:
            break
    else:
        raise ObservationError("Campaign artifact listing exceeds collection limit")
    for artifact, (source_run, observer_run, observer_attempt) in artifacts:
        check_budget()
        origin = artifact.get("workflow_run")
        if not isinstance(origin, dict) or origin.get("id") != int(observer_run):
            raise ObservationError("Observation artifact provenance mismatch")
        key = (observer_run, observer_attempt)
        if key not in verified_runs:
            run = _json(_get(session, f"{base}/actions/runs/{observer_run}/attempts/{observer_attempt}", headers=headers))
            head_repo = run.get("head_repository")
            verified_runs[key] = (run.get("id") == int(observer_run)
                    and run.get("run_attempt") == int(observer_attempt)
                    and run.get("status") == "completed"
                    and run.get("event") in ("workflow_run", "workflow_dispatch")
                    and run.get("head_branch") == "main"
                    and isinstance(head_repo, dict) and str(head_repo.get("full_name", "")).lower() == repo.lower()
                    and str(run.get("path", "")).split("@")[0] == ".github/workflows/observe_paper.yml"
                    and run.get("name") == "Observe Paper Quotes")
        if not verified_runs[key]:
            continue
        check_budget()
        report = download_artifact(session, base, artifact, headers, filename="paper_observation_report.json")
        if str(report.get("run_id")) != source_run or str(report.get("repository", "")).lower() != repo.lower():
            raise ObservationError("Campaign report identity mismatch")
        reports.append(report)
    return reports


def automatic_campaign(environ, session, current_path="paper_observation_report.json"):
    """Current report plus previous verified artifacts; no Alpaca credentials used."""
    path = Path(current_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ObservationError("Current observation report is unavailable")
    current = _json(path.read_bytes())
    start = environ.get("EOD_OBSERVATION_START_DATE", DEFAULT_START)
    reports = [current]
    history_complete = True
    try:
        reports += fetch_observations(session, environ.get("GITHUB_REPOSITORY", ""),
                                      environ.get("GITHUB_TOKEN", ""), start=start)
    except ObservationError:
        history_complete = False
    report = aggregate_reports(reports, start=start)
    report.update(status="aggregated" if history_complete else "incomplete", history_complete=history_complete)
    if not history_complete:
        report.update(complete=False, reason="Historical observation collection unavailable; counts are partial")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only paper quote observation")
    parser.add_argument("--aggregate", nargs="+", metavar="REPORT_JSON")
    parser.add_argument("--campaign", action="store_true", help="Automatically combine this report with prior trusted GitHub observations")
    args = parser.parse_args(argv)
    if args.aggregate and args.campaign:
        parser.error("--aggregate and --campaign cannot be combined")
    output = "paper_observation_campaign.json" if args.aggregate or args.campaign else "paper_observation_report.json"
    try:
        if args.aggregate:
            report = aggregate(args.aggregate, start=os.environ.get("EOD_OBSERVATION_START_DATE", DEFAULT_START))
        elif args.campaign:
            with requests.Session() as session:
                report = automatic_campaign(os.environ, session)
        else:
            with requests.Session() as session:
                report = observe(os.environ, session)
        code = int(report.get("status") == "incomplete")
    except Exception:
        report, code = {"schema_version": 1, "status": "error", "reason": "Paper observation failed; no trading or notifications were invoked"}, 1
    Path(output).write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print("Paper observation: " + str(report.get("status", "campaign aggregated")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
