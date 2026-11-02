"""Bounded Alpaca I/O and conservative reconciliation; no blind POST retries."""
from __future__ import annotations

import time
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest, MarketOrderRequest

from .safety import NY, SafetyError, aware, decimal, midnight, quantity, validate_account


OPEN_ORDER_RECHECK_SECONDS = 6.0
OPEN_ORDER_RECHECK_POLLS = 3
RECENT_FILL_SECONDS = 30.0


class TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", (5, 15))
        # Never send credentials to redirects, even if a proxy misbehaves.
        kwargs.setdefault("allow_redirects", False)
        return super().request(method, url, **kwargs)


def bound_client(client):
    client._session.close()
    client._session = TimeoutSession()
    client._retry = 0
    return client


def value(item):
    return str(getattr(item, "value", item)).lower()


class Broker:
    def __init__(self, key, secret, paper=True):
        self.client = bound_client(TradingClient(key, secret, paper=paper))

    def bind_context(self, window, summary):
        """Attach diagnostics to this run, without granting order authorization.

        Receipts come only from this instance's successful execute() calls.
        A summary or previous-session record can never seed them.
        """
        previous = getattr(self, "_execution_context", None)
        if previous is None or previous[0].day != window.day or previous[1] is not summary:
            self._recent_fills = {}
        self._execution_context = (window, summary)

    def clock(self):
        return self.client.get_clock()

    def calendar(self, start, end):
        return self.client.get_calendar(GetCalendarRequest(start=start, end=end))

    def account(self):
        return self.client.get_account()

    def positions(self):
        result = {}
        for p in self.client.get_all_positions():
            if value(p.side) != "long" or decimal(p.qty) <= 0:
                raise SafetyError("Short/invalid position is outside strategy scope")
            result[p.symbol] = p
        return result

    def assert_assets(self, symbols):
        for symbol in symbols:
            asset = self.client.get_asset(symbol)
            if not asset.tradable or not asset.fractionable or value(asset.status) != "active":
                raise SafetyError(f"Asset unavailable for fractional trading: {symbol}")

    def orders(self, day=None, open_only=False):
        kwargs = {"status": QueryOrderStatus.OPEN if open_only else QueryOrderStatus.ALL,
                  "limit": 500, "nested": True}
        if day is not None:
            kwargs.update(after=midnight(day), until=midnight(day+timedelta(days=1)))
        result = self.client.get_orders(GetOrdersRequest(**kwargs))
        # No timestamp cursor can safely disambiguate 500 simultaneous submissions.
        if len(result) >= 500:
            raise SafetyError("Order query saturated; cannot certify complete history")
        return result

    @staticmethod
    def _order_diagnostic(order):
        """Allowlisted metadata only; never stringify an API exception or body."""
        def token(name):
            raw = getattr(order, name, None)
            if raw is None:
                return None
            raw = str(getattr(raw, "value", raw))
            return raw if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", raw) else "invalid"

        row = {name: token(name) for name in ("id", "symbol", "side", "status", "client_order_id")}
        for name in ("created_at", "submitted_at", "updated_at", "filled_at"):
            raw = getattr(order, name, None)
            try:
                row[name] = aware(raw).isoformat() if raw is not None else None
            except (TypeError, ValueError, OverflowError, SafetyError):
                row[name] = "invalid"
        return row

    @staticmethod
    def _same_filled_order(order, receipt):
        """A re-read must identify exactly the verified full fill, not just a symbol."""
        try:
            return (
                str(getattr(order, "id", "")) == receipt["id"]
                and getattr(order, "symbol", None) == receipt["symbol"]
                and getattr(order, "client_order_id", None) == receipt["client_order_id"]
                and value(getattr(order, "side", None)) == receipt["side"]
                and value(getattr(order, "status", None)) == "filled"
                and decimal(getattr(order, "filled_qty", None)) == receipt["filled_qty"]
                and decimal(getattr(order, "filled_avg_price", None)) == receipt["filled_avg_price"]
                and not getattr(order, "legs", None)
            )
        except (ArithmeticError, ValueError, TypeError, SafetyError):
            return False

    def assert_no_open_orders(self, *, sleeper=time.sleep, monotonic=time.monotonic):
        """Require an empty open-order query; only recent own fills may be rechecked.

        This is a bounded read-only reconciliation, never an exception to the
        empty-list requirement. Unknown orders, partials, conflicting state,
        expired receipts and any uncertainty keep the account blocked.
        """
        rows = self.orders(open_only=True)
        if not rows:
            return
        context = getattr(self, "_execution_context", None)
        window, summary = context if context is not None else (None, None)
        check = {"status": "blocked", "snapshots": [], "polls": 0}
        if summary is not None:
            summary.setdefault("open_order_checks", []).append(check)

        def snapshot(items, observed_at=None):
            check["snapshots"].append({
                "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
                "orders": [self._order_diagnostic(item) for item in items],
            })

        snapshot(rows)
        blocked = "Open orders exist; dedicated account must be quiescent"
        receipts = getattr(self, "_recent_fills", {})
        if window is None or not receipts:
            check["reason"] = "NO_RECENT_OWN_FILL"
            raise SafetyError(blocked)
        started = monotonic()
        if not math.isfinite(started):
            check["reason"] = "INVALID_RECONCILIATION_CLOCK"
            raise SafetyError(blocked)

        def remaining():
            elapsed = monotonic() - started
            if not math.isfinite(elapsed) or elapsed < 0 or elapsed >= OPEN_ORDER_RECHECK_SECONDS:
                raise SafetyError("Open-order reconciliation deadline reached")
            return OPEN_ORDER_RECHECK_SECONDS - elapsed

        involved = {}
        try:
            for attempt in range(OPEN_ORDER_RECHECK_POLLS):
                remaining()
                checked_at = aware(self.assert_time(window)).isoformat()
                check["snapshots"][-1]["observed_at"] = checked_at
                ids = set()
                for item in rows:
                    order_id = str(getattr(item, "id", ""))
                    receipt = receipts.get(order_id)
                    age = monotonic() - receipt["confirmed_monotonic"] if receipt else -1
                    if (receipt is None or order_id in ids or not math.isfinite(age)
                            or not 0 <= age <= RECENT_FILL_SECONDS
                            or getattr(item, "symbol", None) != receipt["symbol"]
                            or getattr(item, "client_order_id", None) != receipt["client_order_id"]
                            or value(getattr(item, "side", None)) != receipt["side"]
                            or value(getattr(item, "status", None)) not in {
                                "new", "accepted", "pending_new", "accepted_for_bidding", "filled",
                            } or getattr(item, "legs", None)):
                        check["reason"] = "UNKNOWN_OR_UNSAFE_OPEN_ORDER"
                        raise SafetyError(blocked)
                    # A pending row may be an old zero-fill snapshot, but a
                    # partial/contradictory fill never earns a waiting exception.
                    try:
                        listed_qty = decimal(getattr(item, "filled_qty", None))
                    except (ArithmeticError, ValueError, TypeError, SafetyError):
                        listed_qty = Decimal(-1)
                    if (value(item.status) == "filled" and not self._same_filled_order(item, receipt)
                            or value(item.status) != "filled" and listed_qty != 0):
                        check["reason"] = "CONTRADICTORY_OPEN_LIST_FILL"
                        raise SafetyError(blocked)
                    ids.add(order_id)
                    remaining()
                    verified = self.client.get_order_by_id(order_id)
                    remaining()
                    if not self._same_filled_order(verified, receipt):
                        check["reason"] = "OWN_FILL_RECHECK_MISMATCH"
                        raise SafetyError(blocked)
                    involved[order_id] = receipt
                # Polling may only confirm absence, never filter away an order.
                sleeper(min(1.0, remaining()))
                remaining()
                self.assert_time(window)
                rows = self.orders(open_only=True)
                check["polls"] = attempt + 1
                remaining()
                snapshot(rows, aware(self.assert_time(window)).isoformat())
                if rows:
                    continue
                current = self.positions()
                remaining()
                for receipt in involved.values():
                    actual = decimal(current[receipt["symbol"]].qty) if receipt["symbol"] in current else Decimal(0)
                    if actual != receipt["expected_position_qty"]:
                        check["reason"] = "POSITION_NOT_RECONCILED"
                        raise SafetyError("Position has not reconciled after confirmed fill")
                account = self.account()
                remaining()
                validate_account(account)
                # Persist numeric account evidence, but never an account ID/key.
                cash = decimal(account.cash)
                if cash < 0:
                    raise SafetyError("Cash has not reconciled after confirmed fill")
                self.assert_time(window)
                remaining()
                check.update(status="resolved", reason="OWN_FILLS_RECONCILED_OPEN_LIST_EMPTY",
                             cash=str(cash), equity=str(decimal(account.equity)))
                return
            check["reason"] = "OPEN_LIST_STILL_NONEMPTY"
            raise SafetyError(blocked)
        except Exception:
            check.setdefault("reason", "RECONCILIATION_FAILED_CLOSED")
            raise

    def fills_today(self, day):
        """FILL covers executions of orders submitted before today, including partials."""
        rows, seen, token = [], set(), None
        for _ in range(100):
            params = {"after": (midnight(day)-timedelta(seconds=1)).isoformat(),
                      "until": midnight(day+timedelta(days=1)).isoformat(),
                      "direction": "asc", "page_size": 100}
            if token:
                params["page_token"] = token
            page = self.client.get("/account/activities/FILL", params)
            if not isinstance(page, list):
                raise SafetyError("Invalid FILL response")
            for row in page:
                if not isinstance(row, dict) or not all(k in row for k in ("id", "symbol", "side", "qty", "transaction_time")):
                    raise SafetyError("Incomplete FILL record")
                if any(not isinstance(row[k], str) or not row[k].strip() for k in ("id", "symbol")):
                    raise SafetyError("Invalid FILL identity")
                if row["id"] in seen or row["side"] not in ("buy", "sell") or decimal(row["qty"]) <= 0:
                    raise SafetyError("Ambiguous FILL record or repeated pagination")
                seen.add(row["id"])
                if aware(row["transaction_time"]).astimezone(NY).date() == day:
                    rows.append(row)
            if len(page) < 100:
                return rows
            token = page[-1]["id"]
        raise SafetyError("FILL pagination exhausted safety limit")

    def traded_symbols(self, day, side):
        symbols = {f["symbol"] for f in self.fills_today(day) if f["side"] == side}
        # Cross-check fully/partially filled orders created today as well. The FILL
        # ledger above remains authoritative for orders submitted on earlier dates.
        for order in self.orders(day):
            if order.filled_qty is None or order.filled_qty == "":
                raise SafetyError("Missing order filled quantity")
            filled = decimal(order.filled_qty)
            if filled < 0 or (value(order.status) in ("filled", "partially_filled") and filled <= 0):
                raise SafetyError("Contradictory order filled quantity")
            if value(order.side) == side and filled > 0:
                symbols.add(order.symbol)
        return symbols

    def assert_can_sell(self, symbol, day):
        self.assert_no_open_orders()
        if symbol in self.traded_symbols(day, "buy"):
            raise SafetyError(f"ANTI_PDT: bought today: {symbol}")

    def assert_time(self, window):
        clock = self.clock()
        if not clock.is_open or not window.accepts_order(clock.timestamp):
            raise SafetyError("Order deadline reached or market closed")
        return clock.timestamp

    def execute(self, *, symbol, side, amount, window, summary, sleeper=time.sleep, pre_submit=None):
        """Return only on full fill. A partial/rejected/uncertain order aborts run."""
        from uuid import UUID
        from alpaca.trading.enums import OrderStatus

        self.bind_context(window, summary)
        self.assert_time(window)
        self.assert_no_open_orders()
        if side == "sell":
            self.assert_can_sell(symbol, window.day)
            position = self.positions().get(symbol)
            if position is None or quantity(amount) > quantity(position.qty):
                raise SafetyError("Sell exceeds verified long position")
        elif symbol in self.traded_symbols(window.day, "sell"):
            raise SafetyError(f"Same-session reentry blocked: {symbol}")
        else:
            position = self.positions().get(symbol)
        before_qty = decimal(position.qty) if position is not None else Decimal(0)
        submission_time = self.assert_time(window)
        if pre_submit is not None:
            # The callback must be local validation only. Recheck quotes using
            # the final Alpaca clock after FILL/position reads, without adding
            # another network request between validation and submission.
            try:
                pre_submit(submission_time)
            except SafetyError:
                raise
            except Exception:
                raise SafetyError("Pre-submission validation failed") from None
        client_id = f"eod-v1-{window.day:%Y%m%d}-{side[0]}-{symbol.replace('.', '')}"
        fields = {"qty": float(quantity(amount))} if side == "sell" else {"notional": float(amount)}
        req = MarketOrderRequest(symbol=symbol, side=OrderSide(side),
                                 time_in_force=TimeInForce.DAY, extended_hours=False,
                                 client_order_id=client_id, **fields)
        record = {"symbol": symbol, "side": side, "client_order_id": client_id,
                  "requested": str(amount), "status": "submitting"}
        summary["orders"].append(record)
        try:
            order = self.client.submit_order(order_data=req)
        except Exception:
            # Submission may have succeeded despite a network timeout. Never POST
            # again: query using the same deterministic key; uncertainty stops run.
            try:
                order = self.client.get_order_by_client_id(client_id)
            except Exception:
                record["status"] = "unknown_submission"
                raise SafetyError("Submission ambiguous; inspect broker before next run") from None
        terminal = {"canceled", "expired", "rejected", "replaced", "done_for_day", "suspended", "stopped"}
        valid_statuses = {item.value for item in OrderStatus}
        verified_id, verified_status = None, None

        def verify_response(candidate):
            nonlocal verified_id, verified_status
            candidate_id = getattr(candidate, "id", None)
            matches_request = (
                getattr(candidate, "symbol", None) == symbol
                and value(getattr(candidate, "side", None)) == side
                and getattr(candidate, "client_order_id", None) == client_id
                and isinstance(candidate_id, (str, UUID))
                and bool(str(candidate_id).strip())
            )
            if not matches_request or (verified_id is not None and str(candidate_id) != verified_id):
                record.update(status="invalid_response", reason="ORDER_IDENTITY_MISMATCH")
                raise SafetyError("Order response identity mismatch")

            # Retain the verified original ID separately. An unrelated response
            # must never supply the ID used by the cancellation recovery path.
            verified_id = str(candidate_id)
            verified_status = value(getattr(candidate, "status", None))
            record["id"] = verified_id
            try:
                if verified_status not in valid_statuses:
                    raise SafetyError("Unknown order response status")
                filled = decimal(getattr(candidate, "filled_qty", None))
                if filled < 0 or (verified_status in {"filled", "partially_filled"} and filled <= 0):
                    raise SafetyError("Order response has invalid filled quantity")
                if filled > 0 and decimal(getattr(candidate, "filled_avg_price", None)) <= 0:
                    raise SafetyError("Order response has invalid filled average price")
                if side == "sell":
                    requested = quantity(amount)
                    if filled > requested or (verified_status == "filled" and filled != requested):
                        raise SafetyError("Filled sell quantity differs from requested quantity")
            except (ArithmeticError, ValueError, TypeError):
                record.update(status="invalid_response", reason="INVALID_ORDER_FILL")
                raise SafetyError("Order response has malformed fill values") from None
            except SafetyError:
                record.update(status="invalid_response", reason="INVALID_ORDER_FILL")
                raise
            record.update(status=verified_status, filled_qty=str(candidate.filled_qty),
                          filled_avg_price=str(candidate.filled_avg_price or ""))
            return candidate

        try:
            order = verify_response(order)
            deadline = time.monotonic() + 90
            while True:
                if verified_status == "filled":
                    confirmed_at = time.monotonic()
                    if not math.isfinite(confirmed_at):
                        raise SafetyError("Invalid monotonic clock after fill confirmation")
                    filled_qty = decimal(order.filled_qty)
                    self._recent_fills[verified_id] = {
                        "id": verified_id, "symbol": symbol, "side": side,
                        "client_order_id": client_id, "filled_qty": filled_qty,
                        "filled_avg_price": decimal(order.filled_avg_price),
                        "expected_position_qty": before_qty + (filled_qty if side == "buy" else -filled_qty),
                        "confirmed_monotonic": confirmed_at,
                    }
                    return order
                if verified_status in terminal:
                    raise SafetyError(f"Order did not fully fill: {symbol} ({verified_status})")
                if time.monotonic() >= deadline:
                    raise SafetyError(f"Fill confirmation timed out: {symbol}")
                self.assert_time(window)
                sleeper(2)
                order = verify_response(self.client.get_order_by_id(verified_id))
        except Exception:
            # Cancel only this bot's unresolved order, never blanket-cancel others.
            # Failure to verify cancellation remains a hard stop, never a BUY.
            if verified_id is not None and verified_status not in terminal | {"filled"}:
                try:
                    self.client.cancel_order_by_id(verified_id)
                    for _ in range(5):
                        sleeper(1)
                        verify_response(self.client.get_order_by_id(verified_id))
                        if verified_status in terminal | {"filled"}:
                            break
                    else:
                        record["status"] = "cancel_unconfirmed"
                except Exception:
                    record["status"] = "cancel_unconfirmed"
            raise
