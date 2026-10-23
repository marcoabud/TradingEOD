"""Read-only quote recovery, strict validation and safe replay snapshots.

Only active quote work consumes QuoteBudget; time between phases does not.
The HTTP timeout bounds socket operations, not a hard process interrupt (DNS
and a streaming peer may overrun it). Results are rechecked against both clocks
after every request, so an overrun never licenses a late order.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Timeout

from .safety import SafetyError, aware


_SYMBOL = re.compile(r"[A-Z0-9.\-]{1,16}")
_FEEDS = {"iex", "sip"}
_RETRY_AT = (0.0, 3.0, 8.0, 18.0)
_MAX_REQUEST_SECONDS = 8.0
_MIN_REQUEST_SECONDS = 0.05


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _utc(value):
    result = aware(value)
    if result.utcoffset() is None:
        raise SafetyError("Ambiguous quote clock")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class QuoteSnapshot:
    """Allowlisted fields only; compatible with data.quote_price and replay."""
    timestamp: datetime | None
    bid_price: float | None
    ask_price: float | None
    bid_size: float | None
    ask_size: float | None


def quote_snapshot(quote) -> QuoteSnapshot:
    def get(long_name, short_name):
        if isinstance(quote, Mapping):
            return quote.get(long_name, quote.get(short_name))
        return getattr(quote, long_name, None)
    try:
        timestamp = _utc(get("timestamp", "t"))
    except (AttributeError, TypeError, ValueError, OverflowError, SafetyError):
        timestamp = None
    return QuoteSnapshot(timestamp, _number(get("bid_price", "bp")),
                         _number(get("ask_price", "ap")),
                         _number(get("bid_size", "bs")), _number(get("ask_size", "as")))


def inspect_quote(quote, now, *, feed):
    """Return (price | None, safe cause | None, diagnostics), without network.

    Accepts an SDK quote, a REST quote dict, or QuoteSnapshot. Historical replay
    must supply its original observation time; no timestamp is inferred.
    """
    if feed not in _FEEDS:
        raise SafetyError("Quote feed must be explicitly iex or sip")
    try:
        observed = _utc(now)
    except (TypeError, ValueError, OverflowError, SafetyError):
        raise SafetyError("Invalid quote observation clock") from None
    snap = quote_snapshot(quote)
    bid, ask = snap.bid_price, snap.ask_price
    age = (observed - snap.timestamp).total_seconds() if snap.timestamp else None
    midpoint = bid / 2 + ask / 2 if bid is not None and ask is not None else None
    spread = ((ask - bid) / midpoint
              if midpoint is not None and midpoint > 0 else None)
    if spread is not None and not math.isfinite(spread):
        spread = None
    diagnostic = {"feed": feed, "checked_at": observed.isoformat(),
                  "timestamp": snap.timestamp.isoformat() if snap.timestamp else None,
                  "age_seconds": age, "bid": bid, "ask": ask,
                  "bid_size": snap.bid_size, "ask_size": snap.ask_size, "spread": spread}
    if quote is None:
        cause = "missing_quote"
    elif age is None:
        cause = "invalid_timestamp"
    elif age < -5:
        cause = "future_timestamp"
    elif age > 90:
        cause = "stale"
    elif bid is None or ask is None:
        cause = "invalid_price"
    elif bid <= 0 or ask <= 0:
        cause = "nonpositive_price"
    elif bid > ask:
        cause = "crossed_market"
    elif snap.bid_size is None or snap.ask_size is None:
        cause = "invalid_size"
    elif min(snap.bid_size, snap.ask_size) <= 0:
        cause = "empty_size"
    elif spread is None or spread > 0.005:
        cause = "excessive_spread"
    else:
        cause = None
    diagnostic["cause"] = cause
    return (midpoint if cause is None else None), cause, diagnostic


@dataclass
class QuoteBatch:
    prices: dict[str, float] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    diagnostics: dict[str, dict] = field(default_factory=dict)
    quotes: dict[str, QuoteSnapshot] = field(default_factory=dict)


class QuoteFatalError(SafetyError):
    """Global API failure. Message and attached diagnostics are safe to log."""
    def __init__(self, cause, *, diagnostics=None):
        allowed = {"authentication", "permission", "malformed_response", "invalid_request",
                   "redirect_refused", "transport_exhausted", "rate_limit_exhausted",
                   "server_exhausted", "invalid_clock", "clock_regressed", "deadline_reached"}
        self.cause = cause if cause in allowed else "malformed_response"
        self.diagnostics = diagnostics or {}
        super().__init__(f"Quote service unavailable: {self.cause}")


class _TransientQuoteError(Exception):
    def __init__(self, cause, retry_after=0.0):
        self.cause = cause
        self.retry_after = retry_after


def _retry_after(response):
    """Do not echo provider headers; honor valid delays without capping early."""
    value = response.headers.get("Retry-After")
    if value is None:
        return 0.0
    seconds = _number(value)
    if seconds is not None:
        return max(0.0, seconds)
    try:
        retry_at = _utc(parsedate_to_datetime(value))
        server_date = response.headers.get("Date")
        reference = _utc(parsedate_to_datetime(server_date)) if server_date else datetime.now(timezone.utc)
        return max(0.0, (retry_at - reference).total_seconds())
    except (TypeError, ValueError, OverflowError, SafetyError):
        return 0.0


@dataclass
class QuoteBudget:
    seconds: float = 30.0
    max_attempts: int = 4
    monotonic: object = time.monotonic
    sleeper: object = time.sleep
    used_seconds: float = field(default=0.0, init=False)

    def __post_init__(self):
        seconds = _number(self.seconds)
        if (seconds is None or not 0 <= seconds <= 30
                or isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int)
                or not 1 <= self.max_attempts <= 4 or not callable(self.monotonic) or not callable(self.sleeper)):
            raise SafetyError("Quote budget requires 0..30 seconds and 1..4 attempts")
        self.seconds = seconds

    @property
    def remaining_seconds(self):
        return max(0.0, self.seconds - self.used_seconds)


class LatestQuoteTransport:
    """One fixed HTTPS GET, explicit feed, no redirects or hidden retries."""
    URL = "https://data.alpaca.markets/v2/stocks/quotes/latest"

    def __init__(self, key, secret, *, session=None):
        self._session = session if session is not None else requests.Session()
        self._session.mount("https://", HTTPAdapter(max_retries=0))
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def __call__(self, symbols, *, feed, timeout_seconds):
        if feed not in _FEEDS or not 0 < timeout_seconds <= _MAX_REQUEST_SECONDS:
            raise QuoteFatalError("invalid_request")
        timeout = Timeout(total=timeout_seconds, connect=min(3.0, timeout_seconds / 2),
                          read=min(5.0, timeout_seconds / 2))
        response = None
        try:
            response = self._session.get(self.URL, params={"symbols": ",".join(symbols), "feed": feed},
                                         headers=self._headers, timeout=timeout, allow_redirects=False)
            status = response.status_code
            if status == 401:
                raise QuoteFatalError("authentication")
            if status == 403:
                raise QuoteFatalError("permission")
            if status == 429:
                raise _TransientQuoteError("rate_limit", _retry_after(response))
            if 500 <= status <= 599:
                raise _TransientQuoteError("server")
            if 300 <= status <= 399:
                raise QuoteFatalError("redirect_refused")
            if status != 200:
                raise QuoteFatalError("invalid_request")
            try:
                payload = response.json()
            except (ValueError, TypeError):
                raise QuoteFatalError("malformed_response") from None
            if not isinstance(payload, dict) or not isinstance(payload.get("quotes"), dict):
                raise QuoteFatalError("malformed_response")
            return payload["quotes"]
        except requests.RequestException:
            raise _TransientQuoteError("transport") from None
        except (QuoteFatalError, _TransientQuoteError):
            raise
        except Exception:
            raise QuoteFatalError("malformed_response") from None
        finally:
            if response is not None:
                response.close()


def collect_quotes(symbols, now, *, feed, transport, budget, deadline) -> QuoteBatch:
    """Recover individual quotes without extending age/spread/time limits.

    Four attempts is a per-symbol, per-batch ceiling; active time is shared by
    every batch using the same budget. Exhausted budgets perform no Clock or
    market-data requests. All accepted quotes are revalidated after waiting.
    """
    symbols = tuple(symbols)
    if any(not isinstance(s, str) or not _SYMBOL.fullmatch(s) for s in symbols) or len(set(symbols)) != len(symbols):
        raise SafetyError("Quote symbols must be unique recognized tickers")
    if feed not in _FEEDS or not isinstance(budget, QuoteBudget) or not callable(now):
        raise SafetyError("Invalid quote batch configuration")
    try:
        deadline = _utc(deadline)
    except (TypeError, ValueError, OverflowError, SafetyError):
        raise SafetyError("Quote deadline requires an explicit timezone") from None
    result = QuoteBatch()
    if not symbols:
        return result
    attempts = dict.fromkeys(symbols, 0)
    history = {s: [] for s in symbols}
    ever_failed = set()
    started = budget.monotonic()
    charged = started
    last_time = None
    monotonic_deadline = None
    transient = None
    retry_ready = started

    def charge():
        nonlocal charged
        tick = budget.monotonic()
        if tick < charged:
            raise QuoteFatalError("clock_regressed")
        budget.used_seconds += tick - charged
        charged = tick
        return tick

    def observe():
        nonlocal last_time, monotonic_deadline
        try:
            observed = _utc(now())
        except Exception:
            raise QuoteFatalError("invalid_clock") from None
        tick = charge()
        if last_time is not None and observed < last_time:
            raise QuoteFatalError("clock_regressed")
        last_time = observed
        if monotonic_deadline is None:
            monotonic_deadline = tick + max(0.0, (deadline - observed).total_seconds())
        if observed >= deadline or tick >= monotonic_deadline:
            raise QuoteFatalError("deadline_reached")
        return observed

    def revalidate(observed):
        for symbol in symbols:
            snap = result.quotes.get(symbol)
            price, cause, diagnostic = inspect_quote(snap, observed, feed=feed)
            if cause is not None and attempts[symbol]:
                ever_failed.add(symbol)
            diagnostic.update(attempts=attempts[symbol], recovered=cause is None and symbol in ever_failed)
            diagnostic.update(attempt_history=list(history[symbol]),
                              first=history[symbol][0] if history[symbol] else None,
                              last=history[symbol][-1] if history[symbol] else None)
            result.diagnostics[symbol] = diagnostic
            if cause is None:
                result.prices[symbol] = price
                result.failures.pop(symbol, None)
            else:
                result.prices.pop(symbol, None)
                result.failures[symbol] = cause

    def record_attempts(pending):
        for symbol in pending:
            record = {k: v for k, v in result.diagnostics[symbol].items()
                      if k not in {"attempt_history", "first", "last"}}
            history[symbol].append(record)
            result.diagnostics[symbol].update(attempt_history=list(history[symbol]),
                                              first=history[symbol][0], last=history[symbol][-1])

    def exhausted():
        for symbol in symbols:
            if symbol not in result.prices:
                previous = result.diagnostics.get(symbol, {})
                diagnostic = {"feed": feed, "checked_at": last_time.isoformat() if last_time else None,
                              "timestamp": None, "age_seconds": None, "bid": None,
                              "ask": None, "bid_size": None, "ask_size": None, "spread": None,
                              **previous, "attempts": attempts[symbol], "recovered": False,
                              "cause": "budget_exhausted", "attempt_history": list(history[symbol]),
                              "first": history[symbol][0] if history[symbol] else None,
                              "last": history[symbol][-1] if history[symbol] else None}
                diagnostic["last_cause"] = previous.get("cause")
                result.failures[symbol] = "budget_exhausted"
                result.diagnostics[symbol] = diagnostic
        return result

    try:
        if budget.remaining_seconds < _MIN_REQUEST_SECONDS:
            return exhausted()
        observed = observe()
        if budget.remaining_seconds < _MIN_REQUEST_SECONDS:
            return exhausted()
        for round_index in range(budget.max_attempts):
            revalidate(observed)
            pending = [s for s in symbols if s not in result.prices and attempts[s] < budget.max_attempts]
            if not pending:
                return result
            charge()
            delay = max(0.0, max(started + _RETRY_AT[round_index], retry_ready) - budget.monotonic())
            time_left = min(budget.remaining_seconds, (deadline - observed).total_seconds(),
                            monotonic_deadline - budget.monotonic())
            if time_left <= delay + _MIN_REQUEST_SECONDS:
                if transient:
                    raise QuoteFatalError(f"{transient}_exhausted", diagnostics=result.diagnostics)
                return exhausted()
            if delay:
                budget.sleeper(delay)
                charge()
                if budget.remaining_seconds < _MIN_REQUEST_SECONDS:
                    return exhausted()
                observed = observe()
                if budget.remaining_seconds < _MIN_REQUEST_SECONDS:
                    result.prices.clear()
                    return exhausted()
                revalidate(observed)
                pending = [s for s in symbols if s not in result.prices and attempts[s] < budget.max_attempts]
            request_limit = min(_MAX_REQUEST_SECONDS, budget.remaining_seconds,
                                (deadline - observed).total_seconds(), monotonic_deadline - budget.monotonic())
            if request_limit < _MIN_REQUEST_SECONDS:
                if transient:
                    raise QuoteFatalError(f"{transient}_exhausted", diagnostics=result.diagnostics)
                return exhausted()
            for symbol in pending:
                attempts[symbol] += 1
            try:
                fetched = transport(pending, feed=feed, timeout_seconds=request_limit)
                if not isinstance(fetched, Mapping):
                    raise QuoteFatalError("malformed_response")
                for symbol in pending:
                    if symbol in fetched and fetched[symbol] is not None:
                        result.quotes[symbol] = quote_snapshot(fetched[symbol])
                    else:
                        result.quotes.pop(symbol, None)
                transient = None
            except _TransientQuoteError as error:
                transient = error.cause
                retry_ready = budget.monotonic() + error.retry_after
            except requests.RequestException:
                # Injectable test/replay transports obey the same safe handling.
                transient = "transport"
            except QuoteFatalError:
                raise
            except Exception:
                raise QuoteFatalError("malformed_response") from None
            finally:
                charge()
            # This Clock read also consumes the global budget. If I/O exceeded
            # its allowance, use the monotonic extrapolation only to reject all
            # results, never to publish a price obtained outside the budget.
            if budget.remaining_seconds <= 0:
                result.prices.clear()
                if transient:
                    raise QuoteFatalError(f"{transient}_exhausted", diagnostics=result.diagnostics)
                return exhausted()
            observed = observe()
            if budget.remaining_seconds <= 0:
                result.prices.clear()
                return exhausted()
            revalidate(observed)
            ever_failed.update(result.failures)
            if transient:
                for symbol in pending:
                    result.prices.pop(symbol, None)
                    result.failures[symbol] = transient
                    result.diagnostics[symbol].update(cause=transient, attempts=attempts[symbol], recovered=False)
                ever_failed.update(pending)
            record_attempts(pending)
            if not result.failures:
                return result
        if transient:
            raise QuoteFatalError(f"{transient}_exhausted", diagnostics=result.diagnostics)
        return result
    finally:
        charge()
