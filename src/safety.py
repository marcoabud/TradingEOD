"""Calendar, monetary precision and fail-closed execution rules."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
CENT = Decimal("0.01")
SHARE = Decimal("0.000000001")


class SafetyError(RuntimeError):
    """A safety condition failed; stop the entire order pipeline."""


def decimal(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise SafetyError("Non-finite monetary value")
    return result


def money(value) -> Decimal:
    return decimal(value).quantize(CENT, rounding=ROUND_DOWN)


def quantity(value) -> Decimal:
    return decimal(value).quantize(SHARE, rounding=ROUND_DOWN)


def aware(value) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise SafetyError("Ambiguous timestamp without timezone")
    return result


def session_time(value, day: date) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=NY) if value.tzinfo is None else value.astimezone(NY)
    return datetime.combine(day, time.fromisoformat(str(value)), NY)


def midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, NY).astimezone(timezone.utc)


@dataclass(frozen=True)
class Window:
    day: date
    opens: datetime
    closes: datetime
    weekly_rebalance: bool

    @property
    def target(self):
        return self.closes - timedelta(minutes=15)

    def accepts_start(self, now: datetime) -> bool:
        # GitHub cron delivery is not punctual. A bounded five-minute grace is explicit.
        return self.target <= aware(now) < self.target + timedelta(minutes=5)

    def accepts_order(self, now: datetime) -> bool:
        # Leave five minutes for cancellation / reconciliation before market close.
        return self.target <= aware(now) < self.closes - timedelta(minutes=5)


def resolve_window(clock, calendar) -> Window | None:
    now = aware(clock.timestamp).astimezone(NY)
    day = now.date()
    rows = {c.date: c for c in calendar}
    if day not in rows or not clock.is_open:
        return None
    row = rows[day]
    friday = day + timedelta(days=4-day.weekday())
    remaining = [d for d in rows if day < d <= friday]
    return Window(day, session_time(row.open, day), session_time(row.close, day), not remaining)


def usable_cash(account) -> Decimal:
    # Buying power may be leveraged: cash always caps expenditure.
    values = [decimal(account.cash), decimal(account.buying_power),
              decimal(account.non_marginable_buying_power)]
    return money(max(Decimal(0), min(values)))


def validate_account(account):
    status = str(getattr(account.status, "value", account.status)).upper()
    if status != "ACTIVE" or account.trading_blocked or account.account_blocked:
        raise SafetyError("Account inactive or blocked")
    if getattr(account, "trade_suspended_by_user", False):
        raise SafetyError("Trading suspended by account owner")
    if decimal(account.equity) <= 0 or decimal(account.cash) < 0:
        raise SafetyError("Nonpositive equity or existing cash debit")
    # A 1x account may be cash-only/restricted. Its settled funds cannot be
    # certified from cash/buying_power; never silently assume sale proceeds settle.
    if decimal(account.multiplier) < 2:
        raise SafetyError("Requires unrestricted margin account; cash settlement not certified")
