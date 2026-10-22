"""Read-only reconstruction of EOD quotes; never invokes the trading engine.

Only GET requests to fixed Alpaca paper clock/calendar and market-data endpoints
are permitted. Historical IEX quotes are requested separately for each symbol,
descending with limit=1: intentionally no pagination, because only the last quote
at the calendar target is relevant. A SIP denial is diagnostic evidence, not an
instruction to change feeds or loosen price validation. No notifications are sent.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path

import requests

from .safety import NY, aware, session_time
from .strategy import DEFENSIVE_SYMBOL, OFFENSIVE_UNIVERSE, SWING_UNIVERSE

LOG = logging.getLogger("market_diagnostics")
SYMBOLS = (*OFFENSIVE_UNIVERSE, DEFENSIVE_SYMBOL, *SWING_UNIVERSE)
PAPER = "https://paper-api.alpaca.markets/v2"
DATA = "https://data.alpaca.markets/v2/stocks"
TIMEOUT = (5, 15)


def _get(session, url, headers, params=None):
    """Return a controlled result and private payload; never log HTTP bodies."""
    try:
        response = session.get(url, headers=headers, params=params,
                               timeout=TIMEOUT, allow_redirects=False)
        result = {"http_status": int(response.status_code)}
        if response.status_code != 200:
            return result, None
        try:
            payload = response.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            return {**result, "error": "invalid_json"}, None
        if not isinstance(payload, (dict, list)):
            return {**result, "error": "invalid_response_shape"}, None
        return result, payload
    except requests.Timeout:
        return {"error": "request_timeout"}, None
    except requests.RequestException:
        return {"error": "network_error"}, None
    except Exception:
        return {"error": "unexpected_request_error"}, None


def quote_summary(quote, reference):
    """Explain the existing 90s/positive/two-sided/0.5% guard using safe fields."""
    if not isinstance(quote, dict):
        return {"valid": False, "validation_reasons": ["missing_quote"]}
    result, reasons = {}, []
    try:
        stamp = aware(quote["t"])
        age = (reference - stamp).total_seconds()
        result.update(timestamp=stamp.astimezone(timezone.utc).isoformat(),
                      age_seconds=round(age, 6))
        if age < -5:
            reasons.append("future_timestamp")
        elif age > 90:
            reasons.append("stale_over_90_seconds")
    except Exception:
        reasons.append("invalid_timestamp")
    for source, label in (("bp", "bid_price"), ("ap", "ask_price"),
                          ("bs", "bid_size"), ("as", "ask_size")):
        try:
            value = float(quote[source])
            if isinstance(quote[source], bool) or not math.isfinite(value):
                raise ValueError
            result[label] = value
        except (KeyError, TypeError, ValueError, OverflowError):
            result[label] = None
            reasons.append(f"invalid_{label}")
    bid, ask = result["bid_price"], result["ask_price"]
    if bid is not None and ask is not None:
        if bid <= 0 or ask <= 0:
            reasons.append("nonpositive_price")
        elif bid > ask:
            reasons.append("crossed_quote")
        else:
            spread = (ask-bid)/((ask+bid)/2)
            result["spread_fraction"] = round(spread, 9)
            if spread > 0.005:
                reasons.append("spread_over_0_5_percent")
    sizes = (result["bid_size"], result["ask_size"])
    if any(value is not None and value <= 0 for value in sizes):
        reasons.append("empty_quote_size")
    result.update(valid=not reasons, validation_reasons=reasons)
    return result


def _historical(session, headers, symbol, feed, opens, target):
    result, payload = _get(session, f"{DATA}/{symbol}/quotes", headers, {
        "start": opens.astimezone(timezone.utc).isoformat(),
        "end": target.astimezone(timezone.utc).isoformat(),
        "sort": "desc", "limit": 1, "feed": feed,
    })
    if payload is None:
        return result
    if isinstance(payload, dict) and "quotes" in payload and payload["quotes"] is None:
        return {**result, **quote_summary(None, target)}
    rows = payload.get("quotes") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 1:
        return {**result, "error": "invalid_quotes_shape"}
    return {**result, **quote_summary(rows[0] if rows else None, target)}


def market_check(environ=os.environ, *, session=None):
    report = {"status": "error", "mode": "paper", "read_only": True,
              "historical_iex": {}, "sip_historical": {}, "sip_latest": {}}
    key = str(environ.get("ALPACA_API_KEY", "")).strip()
    secret = str(environ.get("ALPACA_SECRET_KEY", "")).strip()
    paper = str(environ.get("ALPACA_PAPER", "")).strip().lower()
    if paper not in ("", "true"):
        report["reason"] = "paper_only_configuration_required"
        return report, 1
    if not key or not secret:
        report["reason"] = "missing_credentials"
        return report, 1
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    own_session = session is None
    session = requests.Session() if own_session else session
    try:
        clock_status, clock = _get(session, f"{PAPER}/clock", headers)
        report["clock"] = clock_status
        if not isinstance(clock, dict):
            report["reason"] = "clock_unavailable"
            return report, 1
        try:
            observed = aware(clock["timestamp"]).astimezone(NY)
            if not isinstance(clock["is_open"], bool):
                raise ValueError
            report.update(observed_at=observed.isoformat(), market_open=clock["is_open"],
                          session_date=observed.date().isoformat())
        except Exception:
            report["reason"] = "invalid_clock"
            return report, 1
        day = observed.date()
        requested_day = environ.get("EOD_DIAGNOSTIC_DATE", "").strip()
        if requested_day:
            try:
                day = date.fromisoformat(requested_day)
                if day > observed.date() or day.isoformat() != requested_day:
                    raise ValueError
            except (TypeError, ValueError):
                report["reason"] = "invalid_or_future_diagnostic_date"
                return report, 1
            report["session_date"] = day.isoformat()
        calendar_status, calendar = _get(session, f"{PAPER}/calendar", headers,
                                        {"start": day.isoformat(), "end": day.isoformat()})
        report["calendar"] = calendar_status
        if not isinstance(calendar, list):
            report["reason"] = "calendar_unavailable"
            return report, 1
        if not calendar:
            report.update(status="skipped", reason="no_market_session_today")
            return report, 0
        try:
            if len(calendar) != 1 or calendar[0]["date"] != day.isoformat():
                raise ValueError
            opens = session_time(calendar[0]["open"], day)
            closes = session_time(calendar[0]["close"], day)
            target = closes-timedelta(minutes=15)
            if not opens < target < closes:
                raise ValueError
            report.update(target_time=target.isoformat(), market_close=closes.isoformat())
        except Exception:
            report["reason"] = "invalid_calendar"
            return report, 1
        if observed < target:
            report.update(status="skipped", reason="target_not_reached_yet")
            return report, 0
        # Incident 2026-10-21 failed around target+3.494s. This fixed +3s
        # reconstruction is approximate; it is not the original HTTP response.
        reference = target+timedelta(seconds=3)
        if observed < reference:
            report.update(status="skipped", reason="reconstruction_time_not_reached_yet")
            return report, 0
        report.update(reconstruction_time=reference.isoformat(),
                      reconstruction_note="Approximate target+3s; original live response was not recorded")
        for symbol in SYMBOLS:
            report["historical_iex"][symbol] = _historical(
                session, headers, symbol, "iex", opens, reference)
        # A completed target at least 15 minutes old is eligible for delayed SIP
        # historical access. Always set end explicitly; do not query recent SIP.
        if observed-reference >= timedelta(minutes=15):
            report["sip_historical"] = _historical(
                session, headers, DEFENSIVE_SYMBOL, "sip", opens, reference)
        else:
            report["sip_historical"] = {"skipped": "target_not_15_minutes_old"}
        latest_status, latest = _get(
            session, f"{DATA}/{DEFENSIVE_SYMBOL}/quotes/latest", headers, {"feed": "sip"})
        report["sip_latest"] = latest_status
        if isinstance(latest, dict):
            received = datetime.now(timezone.utc)
            report["sip_latest"].update(
                reference_time=received.isoformat(), reference_kind="client_received_utc",
                **quote_summary(latest.get("quote"), received))
        rows = report["historical_iex"].values()
        report.update(status="completed", quote_checks_passed=all(r.get("valid") is True for r in rows),
                      reason="historical_reconstruction_only_no_orders_or_notifications")
        # Bad quotes are the investigation's result. Unexpected IEX HTTP failures
        # make collection incomplete; SIP entitlement denials remain evidence.
        incomplete = any(r.get("http_status") != 200 or "error" in r for r in rows)
        return report, int(incomplete)
    finally:
        if own_session:
            session.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report, code = market_check()
    if report["status"] == "completed":
        from .signal_diagnostics import reconstruct_signals
        report["signal_replay"] = reconstruct_signals(report, os.environ)
    try:
        Path("market_data_summary.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        LOG.error("Market diagnostic report could not be saved")
        return 1
    LOG.info("Market data check: %s | %s", report["status"], report.get("reason"))
    for symbol, result in report["historical_iex"].items():
        LOG.info("IEX %s %s", symbol, json.dumps(result, sort_keys=True, allow_nan=False))
    LOG.info("SIP historical %s", json.dumps(report["sip_historical"], sort_keys=True))
    LOG.info("SIP latest %s", json.dumps(report["sip_latest"], sort_keys=True))
    if "signal_replay" in report:
        LOG.info("Historical signal evaluation %s", json.dumps(report["signal_replay"], sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
