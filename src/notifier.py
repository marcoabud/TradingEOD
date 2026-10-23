"""Best-effort notifications; trading must never depend on a webhook succeeding."""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

LOGGER = logging.getLogger(__name__)
RETRY_STATUSES = frozenset({429, 502, 503, 504})
SUMMARY_FIELDS = (
    "status", "session_date", "mode", "regime", "equity", "pnl_day", "reason",
    "reserved_cash", "reserved_slots", "core_status", "buy_blocked", "evaluation_outcome",
)
ORDER_FIELDS = (
    "symbol", "side", "status", "notional", "qty", "filled_qty",
    "filled_avg_price", "client_order_id", "reason",
)
SECRET_FIELDS = (
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "WEBHOOK_URL", "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_TRADINGEOD", "TELEGRAM_CHAT_ID", "GITHUB_TOKEN", "GH_TOKEN",
)
ISSUE_FIELDS = ("symbol", "phase", "reason", "attempts")
MAX_ITEMS = 50


def _scalar(value: Any, secrets: tuple[str, ...]) -> str | int | float | bool | None:
    """Produce small JSON-safe values without arbitrary objects or nested data."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, (Decimal, date, datetime)):
        value = str(value)
    if isinstance(value, int):
        return value if abs(value) <= 10**18 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    return value[:500]


def _payload(summary: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    secrets = tuple(sorted({value for key in SECRET_FIELDS if environ.get(key)
                            for value in (environ[key], environ[key].strip()) if value},
                           key=len, reverse=True))
    payload = {
        key: _scalar(summary.get(key), secrets)
        for key in SUMMARY_FIELDS if key in summary
    }
    orders = summary.get("orders", [])
    if not isinstance(orders, (list, tuple)):
        orders = []
    payload["orders"] = [
        {key: _scalar(order[key], secrets) for key in ORDER_FIELDS if key in order}
        for order in orders[:MAX_ITEMS] if isinstance(order, Mapping)
    ]
    issues = summary.get("data_issues", [])
    if not isinstance(issues, (list, tuple)):
        issues = []
    payload["data_issues"] = [
        {key: _scalar(issue[key], secrets) for key in ISSUE_FIELDS if key in issue}
        for issue in issues[:MAX_ITEMS] if isinstance(issue, Mapping)
    ]
    payload["source"] = "TradingEOD"
    return payload


def _telegram_text(payload: Mapping[str, Any]) -> str:
    status = payload.get("status", "unknown")
    status_label = {
        "completed": "Completado", "degraded": "Ejecución limitada por datos",
        "blocked": "Bloqueado", "error": "Error", "dry_run": "Simulación",
        "missed_window": "Ventana EOD perdida", "skipped": "Omitido",
    }.get(status, str(status))
    lines = [
        "TradingEOD | " + status_label,
        "Fecha: " + str(payload.get("session_date", "n/a")),
        "Modo: " + str(payload.get("mode", "n/a")),
        "Régimen: " + str(payload.get("regime", "n/a")),
        "Equity USD: " + str(payload.get("equity", "n/a")),
        "P&L del día USD: " + str(payload.get("pnl_day", "n/a")),
    ]
    if payload.get("reason"):
        lines.append("Detalle: " + str(payload["reason"]))
    if payload.get("evaluation_outcome"):
        outcome = payload["evaluation_outcome"]
        outcome_label = {
            "no_signals": "Sin señales operables", "completed": "Evaluación completada",
            "no_orders_required": "Sin operaciones requeridas por señales y cartera",
            "orders_confirmed": "Órdenes confirmadas", "data_omissions": "Operaciones omitidas por datos",
            "planned": "Plan simulado", "degraded": "Plan simulado con datos insuficientes",
            "evaluating": "Evaluación interrumpida",
        }.get(outcome, str(outcome))
        lines.append("Evaluación: " + outcome_label)
    if payload.get("core_status"):
        core = payload["core_status"]
        core_label = {
            "not_due": "Sin rebalanceo semanal hoy", "ready": "Pendiente",
            "frozen": "Suspendido por datos", "incomplete": "Rebalanceo incompleto",
            "completed": "Evaluación semanal completada",
        }.get(core, str(core))
        lines.append("Estado Core: " + core_label)
    if "reserved_cash" in payload:
        lines.append("Efectivo reservado USD: " + str(payload["reserved_cash"]))
    if "reserved_slots" in payload:
        lines.append("Slots reservados: " + str(payload["reserved_slots"]))
    if isinstance(payload.get("buy_blocked"), bool):
        lines.append("Compras bloqueadas por datos: " + ("sí" if payload["buy_blocked"] else "no"))
    for order in payload.get("orders", []):
        lines.append(" | ".join(
            str(order.get(key, ""))
            for key in ("side", "symbol", "status", "filled_qty", "filled_avg_price")
        ))
    if not payload.get("orders"):
        lines.append("Sin órdenes ejecutadas.")
    issues = payload.get("data_issues", [])
    if issues:
        lines.append("Incidencias de datos:")
        for issue in issues[:8]:
            lines.append(
                f"{issue.get('symbol', 'n/a')} [{issue.get('phase', 'n/a')}]: "
                f"{str(issue.get('reason', 'n/a'))[:160]} (intentos: {issue.get('attempts', 'n/a')})"
            )
        if len(issues) > 8:
            lines.append(f"Otras {len(issues) - 8} incidencias; consultar el resumen de ejecución.")
    # Telegram's text limit is 4096 characters; leave room for future metadata.
    return "\n".join(lines)[:3900]


def _https_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return bool(
            parts.scheme == "https" and parts.hostname
            and parts.username is None and parts.password is None and not parts.fragment
        )
    except ValueError:
        return False


def _post(
    session: requests.Session,
    channel: str,
    url: str,
    payload: Mapping[str, Any],
    sleeper: Callable[[float], None],
) -> bool:
    for attempt in range(3):
        retry = False
        try:
            response = session.post(
                url, json=payload, timeout=(3.05, 8.0), allow_redirects=False
            )
            try:
                if 200 <= response.status_code < 300:
                    if channel == "telegram":
                        try:
                            result = response.json()
                            if not isinstance(result, dict) or result.get("ok") is not True:
                                LOGGER.warning("Notification telegram rejected")
                                return False
                        except (ValueError, requests.RequestException):
                            LOGGER.warning("Notification telegram invalid response")
                            return False
                    return True
                LOGGER.warning(
                    "Notification %s HTTP status=%s", channel, response.status_code
                )
                retry = response.status_code in RETRY_STATUSES
            finally:
                response.close()
        except (requests.Timeout, requests.ConnectionError) as exc:
            # Exception messages may contain webhook URLs or Telegram tokens.
            LOGGER.warning("Notification %s failed (%s)", channel, type(exc).__name__)
            retry = True
        except requests.RequestException as exc:
            LOGGER.warning("Notification %s failed (%s)", channel, type(exc).__name__)
            return False
        if not retry or attempt == 2:
            return False
        # Notifications are at-least-once; a timeout can cause a duplicate message.
        sleeper(float(2 ** attempt))
    return False


def notify(
    summary: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    session: requests.Session | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, bool]:
    """Send an allowlisted summary to configured channels and return delivery flags.

    Missing configuration is a no-op. HTTP failures never raise into the trading
    path. Neither request URLs nor response/error bodies are logged.
    """
    environ = os.environ if environ is None else environ
    payload = _payload(summary, environ)
    results: dict[str, bool] = {}
    owns_session = session is None
    session = requests.Session() if session is None else session
    try:
        webhook = environ.get("WEBHOOK_URL", "").strip()
        if webhook:
            if _https_url(webhook):
                results["webhook"] = _post(session, "webhook", webhook, payload, sleeper)
            else:
                LOGGER.warning("Notification webhook requires a valid HTTPS URL")
                results["webhook"] = False
        token = environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = environ.get("TELEGRAM_CHAT_ID", "").strip()
        if token and chat_id:
            # Reject path/query manipulation while preserving Telegram token syntax.
            if not all(char.isalnum() or char in ":_-" for char in token):
                LOGGER.warning("Notification telegram token format invalid")
                results["telegram"] = False
            else:
                results["telegram"] = _post(
                    session, "telegram", f"https://api.telegram.org/bot{token}/sendMessage",
                    {"chat_id": chat_id, "text": _telegram_text(payload)}, sleeper,
                )
        elif token or chat_id:
            LOGGER.warning("Notification telegram configuration incomplete")
            results["telegram"] = False
    finally:
        if owns_session:
            session.close()
    return results
