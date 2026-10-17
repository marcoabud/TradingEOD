"""Bounded Alpaca I/O and conservative reconciliation; no blind POST retries."""
from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal

import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest, MarketOrderRequest

from .safety import NY, SafetyError, aware, decimal, midnight, quantity


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

    def assert_no_open_orders(self):
        if self.orders(open_only=True):
            raise SafetyError("Open orders exist; dedicated account must be quiescent")

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

    def execute(self, *, symbol, side, amount, window, summary, sleeper=time.sleep):
        """Return only on full fill. A partial/rejected/uncertain order aborts run."""
        from uuid import UUID
        from alpaca.trading.enums import OrderStatus

        self.assert_time(window)
        self.assert_no_open_orders()
        if side == "sell":
            self.assert_can_sell(symbol, window.day)
            position = self.positions().get(symbol)
            if position is None or quantity(amount) > quantity(position.qty):
                raise SafetyError("Sell exceeds verified long position")
        elif symbol in self.traded_symbols(window.day, "sell"):
            raise SafetyError(f"Same-session reentry blocked: {symbol}")
        self.assert_time(window)
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
