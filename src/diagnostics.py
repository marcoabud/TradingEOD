"""Paper-only connectivity check, independent of the trading window.

Run with ``python -m src.diagnostics``. The Alpaca operations are strictly read
only: Clock, Calendar, Account, Positions, and Open Orders. No signal evaluation,
order submission/cancellation, or trading-session claim is performed. Telegram
receives an explicitly identified connectivity report; delivery must be confirmed
for this diagnostic to succeed. Output contains counts and balances, never account
identifiers, credentials, order objects, request URLs, or provider error bodies.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
import os
from pathlib import Path
from typing import Callable, Mapping

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest

from .broker import bound_client
from .notifier import notify
from .safety import NY, aware, decimal


LOG = logging.getLogger("trading_eod.diagnostics")


class DiagnosticConfigError(ValueError):
    """Missing configuration or an attempt to change the diagnostic to live."""


def _paper_client(key: str, secret: str):
    # Intentionally not derived from the environment: no live endpoint is possible.
    return bound_client(TradingClient(key, secret, paper=True))


def connection_check(
    environ: Mapping[str, str],
    *,
    client_factory: Callable = _paper_client,
    notifier: Callable = notify,
) -> tuple[dict, int]:
    """Return a safe report and exit code; all brokerage operations are GETs.

    Missing/empty ALPACA_PAPER selects paper. Any explicit value other than true
    is rejected before constructing a client. Notification failure fails this
    diagnostic even when every read-only Alpaca check succeeded.
    """
    summary = {
        "status": "connection_check",
        "session_date": datetime.now(NY).date().isoformat(),
        "mode": "paper",
        "read_only": True,
        "regime": "NOT_EVALUATED",
        "orders": [],
        "alpaca_connected": False,
        "telegram_confirmed": False,
        "checks": {},
        "reason": "PAPER: comprobación de conexión de solo lectura; no se enviaron órdenes.",
    }
    failures = []
    try:
        paper = environ.get("ALPACA_PAPER", "").strip().lower()
        if paper not in ("", "true"):
            raise DiagnosticConfigError("This diagnostic supports paper=true only")
        key = environ.get("ALPACA_API_KEY", "").strip()
        secret = environ.get("ALPACA_SECRET_KEY", "").strip()
        if not key or not secret:
            raise DiagnosticConfigError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY")
        client = client_factory(key, secret)

        # Clock and calendar are the first broker calls, even outside market hours.
        clock = client.get_clock()
        day = aware(clock.timestamp).astimezone(NY).date()
        summary.update(session_date=day.isoformat(), market_open=bool(clock.is_open))
        summary["checks"]["clock"] = True
        calendar = client.get_calendar(GetCalendarRequest(start=day, end=day))
        summary["checks"]["calendar"] = True
        summary["calendar_sessions"] = len(calendar)

        account = client.get_account()
        equity, last_equity = decimal(account.equity), decimal(account.last_equity)
        summary.update(equity=str(equity), pnl_day=str(equity-last_equity),
                       cash=str(decimal(account.cash)),
                       pnl_day_note="Equity minus last_equity; deposits/withdrawals are not adjusted")
        summary["checks"]["account"] = True
        positions = client.get_all_positions()
        summary["positions_count"] = len(positions)
        summary["checks"]["positions"] = True
        open_orders = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, limit=500, nested=True,
        ))
        summary["open_orders_count"] = len(open_orders)
        summary["open_orders_count_capped"] = len(open_orders) >= 500
        summary["checks"]["open_orders"] = True
        summary["alpaca_connected"] = True
        summary["reason"] = (
            "PAPER: Clock, Calendar, cuenta, posiciones y órdenes abiertas consultados. "
            f"Posiciones: {len(positions)}; órdenes abiertas: {len(open_orders)}. "
            "Comprobación de solo lectura; no se enviaron ni cancelaron órdenes."
        )
    except DiagnosticConfigError as exc:
        # Only these locally constructed messages may be shown verbatim.
        failures.append("configuration")
        summary.update(status="connection_check_failed", reason=str(exc))
    except Exception as exc:
        failures.append("alpaca_read")
        summary.update(
            status="connection_check_failed",
            reason=f"Paper read-only connection check failed ({type(exc).__name__}); no orders submitted.",
        )

    # This diagnostic checks Telegram specifically; do not forward to a webhook
    # merely because WEBHOOK_URL happens to exist in the caller's environment.
    telegram_environment = {
        name: environ.get(name, "").strip()
        for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    }
    try:
        delivery = notifier(summary, environ=telegram_environment)
        summary["telegram_confirmed"] = isinstance(delivery, dict) and delivery.get("telegram") is True
    except Exception:
        # Unexpected notifier errors must not expose URLs, tokens, or API bodies.
        summary["telegram_confirmed"] = False
    if not summary["telegram_confirmed"]:
        failures.append("telegram_delivery")
        summary["status"] = "connection_check_failed"
        summary["reason"] += " Telegram delivery was not confirmed; check the Telegram secrets and bot/chat access."
    summary["failures"] = failures
    return summary, 1 if failures else 0


def main(
    environ: Mapping[str, str] | None = None,
    *,
    output_path: str | Path = "connectivity_summary.json",
    client_factory: Callable = _paper_client,
    notifier: Callable = notify,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary, code = connection_check(
        os.environ if environ is None else environ,
        client_factory=client_factory, notifier=notifier,
    )
    try:
        Path(output_path).write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        LOG.error("Connectivity report could not be saved")
        return 1
    LOG.info("Connectivity result: %s | %s", summary["status"], summary["reason"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
