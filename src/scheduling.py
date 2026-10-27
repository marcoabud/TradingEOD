"""Calendar-aware, bounded prestart waiting without account or order access.

GitHub's scheduler is best effort. Starting a runner early reduces exposure to
its delay but does not guarantee that an evaluation will happen every session.
The existing five-minute start window remains unchanged.
"""

from __future__ import annotations

from datetime import timedelta, timezone
import logging
import math
import time

from .safety import NY, SafetyError, Window, aware, session_time


MAX_WAIT_SECONDS = 330 * 60
POLL_SECONDS = 60
LOG = logging.getLogger("trading_eod.scheduling")

# Keep rows distinct so late arrivals can still be attributed to today's
# intended family. Earlier arrivals from any configured family are usable
# when the broker calendar and bounded waiting policy permit them.
WARM_HOUR_BY_TARGET_HOUR = {19: 14, 20: 15, 16: 11, 17: 12}
KNOWN_CRONS = frozenset(
    cron for target_hour, warm_hour in WARM_HOUR_BY_TARGET_HOUR.items()
    for cron in (f"45 {target_hour} * * 1-5", f"17 {warm_hour} * * 1-5")
)


def _calendar_window(broker, clock):
    now = aware(clock.timestamp).astimezone(NY)
    day = now.date()
    friday = day + timedelta(days=4 - day.weekday())
    rows = {row.date: row for row in broker.calendar(day, max(day, friday))}
    if day not in rows:
        return None
    row = rows[day]
    later_this_week = any(day < session_day <= friday for session_day in rows)
    # Unlike resolve_window, this deliberately keeps a calendar session after
    # its close; otherwise a genuinely missed start could not be diagnosed.
    return Window(day, session_time(row.open, day), session_time(row.close, day),
                  not later_this_week)


def _expected_crons(window):
    target = window.target.astimezone(timezone.utc)
    warm_hour = WARM_HOUR_BY_TARGET_HOUR.get(target.hour)
    if target.minute != 45 or target.second != 0 or warm_hour is None:
        return frozenset()
    return frozenset((f"45 {target.hour} * * 1-5", f"17 {warm_hour} * * 1-5"))


def _observe(summary, clock, window, scheduled_cron):
    now = aware(clock.timestamp).astimezone(NY)
    summary.update(
        observed_at=now.isoformat(),
        session_date=now.date().isoformat(),
        scheduled_cron=scheduled_cron,
        target_time=window.target.isoformat() if window else None,
        start_deadline=(window.target + timedelta(minutes=5)).isoformat() if window else None,
        market_close=window.closes.isoformat() if window else None,
        delay_minutes=round(max(0.0, (now - window.target).total_seconds() / 60), 3) if window else None,
    )
    return now


def _outcome(summary, status, reason):
    summary.update(status=status, reason=reason)
    return status


def prepare_start(
    broker,
    summary,
    *,
    event_name="",
    scheduled_cron="",
    wait_for_window=False,
    sleeper=time.sleep,
    monotonic=time.monotonic,
    preflight=None,
) -> str:
    """Return ready/skipped/missed_window using only broker Clock and Calendar.

    Any explicitly configured cron can wait for today's supported calendar
    target or enter its valid start window. A late arrival from today's primary
    family, or an admitted wait that overruns, is a *candidate* missed window:
    the caller must reconcile persistent execution state before sending an
    alert. Manual late runs and late alternative-family arrivals stay quiet.

    Waiting is optional, uses sleeps of at most 60 seconds and is bounded by
    330 monotonic minutes. The calendar, session date, target and broker clock
    are checked again after waiting. An optional read-only preflight(window)
    callback runs once in the five minutes BEFORE the target, only while
    waiting. Its failure is recorded separately and never authorizes or blocks
    the subsequent evaluation. The caller must not submit orders or consume
    trading claims in this callback. This function itself only reads Clock and
    Calendar and never obtains account data or submits orders.
    """
    # Clock and Calendar are always the first broker operations.
    clock = broker.clock()
    window = _calendar_window(broker, clock)
    scheduled_cron = " ".join(str(scheduled_cron).split())
    now = _observe(summary, clock, window, scheduled_cron)
    if window is None:
        return _outcome(summary, "skipped", "No market session on the observed New York date")

    scheduled = event_name == "schedule"
    expected_crons = _expected_crons(window)
    if scheduled:
        if not expected_crons:
            return _outcome(summary, "skipped", "Unsupported calendar target for configured schedule")
        if scheduled_cron not in KNOWN_CRONS:
            return _outcome(summary, "skipped", "Unrecognized scheduled UTC family")
        summary["schedule_role"] = "primary" if scheduled_cron in expected_crons else "fallback"

    original_day, original_target = window.day, window.target
    wait_admitted = False
    if now < window.target:
        if not wait_for_window:
            return _outcome(summary, "skipped", "Before calendar start window; waiting disabled")
        if (window.target - now).total_seconds() > MAX_WAIT_SECONDS:
            if scheduled:
                return _outcome(summary, "skipped", "Calendar target beyond maximum prestart wait of 330 minutes")
            raise SafetyError("Calendar target exceeds maximum prestart wait of 330 minutes")
        wait_admitted = True
        wait_started = monotonic()
        if not math.isfinite(wait_started):
            raise SafetyError("Invalid monotonic clock during prestart wait")
        LOG.info(
            "Prestart wait begins: target_et=%s expected_wait_minutes=%.1f max_wait_minutes=%d",
            original_target.isoformat(), (original_target - now).total_seconds() / 60,
            MAX_WAIT_SECONDS // 60,
        )
        preflight_at = original_target - timedelta(minutes=5)
        preflight_attempted = False
        while now < original_target:
            elapsed = monotonic() - wait_started
            if not math.isfinite(elapsed) or elapsed < 0:
                raise SafetyError("Invalid monotonic clock during prestart wait")
            if elapsed >= MAX_WAIT_SECONDS:
                raise SafetyError("Maximum prestart wait of 330 minutes exhausted")
            if preflight is not None and not preflight_attempted and now >= preflight_at and clock.is_open:
                preflight_attempted = True
                summary["preflight"] = {"status": "completed", "observed_at": now.isoformat()}
                try:
                    preflight(window)
                except Exception:
                    # Provider exceptions may contain credentials or URLs. The
                    # regular evaluation will fetch and validate its own data.
                    summary["preflight"].update(
                        status="failed", reason="Preflight failed; regular evaluation will recheck data",
                    )
                    LOG.warning("Read-only preflight failed; regular evaluation will recheck data")
                # A callback can take time. Never sleep or grant ready using the
                # timestamp observed before it, even when the callback failed.
                clock = broker.clock()
                now = _observe(summary, clock, window, scheduled_cron)
                if now.date() != original_day:
                    raise SafetyError("New York session date changed during prestart wait")
                continue
            next_wake = preflight_at if preflight is not None and now < preflight_at else original_target
            sleeper(min(POLL_SECONDS, (next_wake - now).total_seconds(),
                        MAX_WAIT_SECONDS - elapsed))
            clock = broker.clock()
            now = _observe(summary, clock, window, scheduled_cron)
            if now.date() != original_day:
                raise SafetyError("New York session date changed during prestart wait")
        elapsed = monotonic() - wait_started
        if not math.isfinite(elapsed) or not 0 <= elapsed <= MAX_WAIT_SECONDS:
            raise SafetyError("Maximum prestart wait of 330 minutes exhausted")
        LOG.info(
            "Prestart wait finished: observed_et=%s elapsed_minutes=%.1f; revalidating calendar",
            now.isoformat(), elapsed / 60,
        )

        # Re-read both immediately before granting ready. A stale initial clock
        # or a changed calendar can never authorize a start after this wait.
        clock = broker.clock()
        refreshed = _calendar_window(broker, clock)
        now = _observe(summary, clock, refreshed, scheduled_cron)
        if (refreshed is None or now.date() != original_day
                or refreshed.day != original_day or refreshed.target != original_target):
            raise SafetyError("Market session or target changed during prestart wait")
        window = refreshed
        if now < window.target:
            raise SafetyError("Broker clock moved before the target after prestart wait")

    if window.accepts_start(now):
        if not clock.is_open:
            raise SafetyError("Market is closed inside the calendar start window")
        return _outcome(summary, "ready", "Inside calendar start window")
    if scheduled and (scheduled_cron in expected_crons or wait_admitted):
        return _outcome(summary, "missed_window", "Scheduled run arrived after calendar start deadline")
    if scheduled:
        return _outcome(summary, "skipped", "Alternative scheduled family arrived after calendar start deadline")
    return _outcome(summary, "skipped", "Manual run outside calendar start window")
