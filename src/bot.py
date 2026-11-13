"""Entry point: python -m src.bot. Simulation is the default, including locally."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from decimal import Decimal
import json
import logging
import os
from pathlib import Path

from .broker import Broker
from .core_recovery import (CoreRecoveryStore, MAX_RECOVERY_DAYS, reconcile_intent,
                            select_recovery_decision)
from .data import MarketData
from .notifier import notify
from .safety import (NY, SafetyError, aware, decimal, money, quantity,
                     resolve_window, usable_cash, validate_account)
from .scheduling import prepare_start
from .state import StateError, claim_missed_window, claim_session, record_evaluation_attempt
from .strategy import (DEFENSIVE_SYMBOL, OFFENSIVE_UNIVERSE, SWING_UNIVERSE,
                       StrategyDataError, core_targets, entry_candidate, require_current_price,
                       swing_entries, swing_exits)

LOG = logging.getLogger("trading_eod")
CORE = (*OFFENSIVE_UNIVERSE, DEFENSIVE_SYMBOL)
UNIVERSE = (*CORE, *SWING_UNIVERSE)
ZERO = Decimal(0)
CORE_WEIGHT, SWING_WEIGHT, SLOT_WEIGHT = Decimal(".70"), Decimal(".30"), Decimal(".15")
BAND = Decimal(".03")
CASH_BUFFER = Decimal("1.00")  # fees, rounding and small quote changes
MIN_BUY = Decimal("1.00")


def bool_env(env, key, default=True):
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    if raw not in ("true", "false"):
        raise SafetyError(f"{key} must be true or false")
    return raw == "true"


@dataclass(frozen=True)
class Config:
    key: str
    secret: str
    paper: bool = True
    dry_run: bool = True
    feed: str = "sip"
    history_feed: str = ""

    @classmethod
    def from_env(cls, env=os.environ):
        key, secret = env.get("ALPACA_API_KEY", ""), env.get("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            raise SafetyError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY")
        return cls(key, secret, bool_env(env, "ALPACA_PAPER"),
                   bool_env(env, "DRY_RUN"), env.get("ALPACA_DATA_FEED", "sip"),
                   env.get("ALPACA_HISTORY_FEED", "").strip())


def position_value(position, indicators):
    return decimal(position.qty) * decimal(require_current_price(indicators[position.symbol]))


def block_value(positions, symbols, indicators):
    return sum((position_value(p, indicators) for s, p in positions.items() if s in symbols), ZERO)


def reserved_cash_amount(positions, indicators, equity, cash, reserved_slots):
    if isinstance(reserved_slots, bool) or not isinstance(reserved_slots, int) or not 0 <= reserved_slots <= 2:
        raise SafetyError("Reserved swing slots must be between zero and two")
    room = max(ZERO, SWING_WEIGHT*equity - block_value(positions, SWING_UNIVERSE, indicators))
    return money(min(max(ZERO, money(cash-CASH_BUFFER)), room, SLOT_WEIGHT*equity*reserved_slots))


def assert_portfolio(positions):
    unknown = set(positions) - set(UNIVERSE)
    if unknown:
        raise SafetyError("Account contains positions outside dedicated strategy universe")
    if len(set(positions) & set(SWING_UNIVERSE)) > 2:
        raise SafetyError("More than two swing positions already exist")


def plan_sells(positions, indicators, equity, decision, bought_today):
    """Return quantity sales, eligible core topups and explicit protected holdings."""
    sells, core_buys, protected = {}, set(), []
    if decision is not None:
        for symbol in CORE:
            target = decimal(decision.targets.get(symbol, 0)) * equity
            p = positions.get(symbol)
            current = position_value(p, indicators) if p is not None else ZERO
            difference = target - current
            # Tolerance is three percentage POINTS of total equity. It also
            # leaves a <3% residual when a target becomes zero, per user rule.
            if p is not None and abs(difference) / equity < BAND:
                continue
            if difference > 0:
                core_buys.add(symbol)
            elif difference < 0 and p is not None:
                if symbol in bought_today:
                    protected.append({"symbol": symbol, "reason": "ANTI_PDT_CORE"})
                    continue
                qty = quantity(p.qty) if target == 0 else quantity(-difference / decimal(indicators[symbol].price))
                if qty > 0:
                    sells[symbol] = {"qty": min(qty, quantity(p.qty)), "reason": "CORE_REBALANCE"}
    exits = swing_exits(indicators, {s: float(p.avg_entry_price) for s, p in positions.items() if s in SWING_UNIVERSE})
    for symbol, reasons in exits.items():
        if symbol in bought_today:
            protected.append({"symbol": symbol, "reason": "ANTI_PDT_SWING", "exit_signals": list(reasons)})
        else:
            sells[symbol] = {"qty": quantity(positions[symbol].qty), "reason": ",".join(reasons)}
    return sells, core_buys, protected


def plan_buys(positions, indicators, equity, cash, decision, core_buys, excluded, *,
              entry_symbols=None, reserved_slots=0):
    """No leverage, no slot overbooking, no shifting unused alpha cash into core."""
    remaining = max(ZERO, money(cash - CASH_BUFFER))
    swing_room = max(ZERO, SWING_WEIGHT*equity - block_value(positions, SWING_UNIVERSE, indicators))
    reserved = reserved_cash_amount(positions, indicators, equity, cash, reserved_slots)
    remaining -= reserved
    plan = []
    core_room = max(ZERO, CORE_WEIGHT*equity - block_value(positions, CORE, indicators))
    if decision:
        for symbol, weight in decision.targets.items():
            if symbol not in core_buys or symbol in excluded:
                continue
            require_current_price(indicators[symbol])
            current = position_value(positions[symbol], indicators) if symbol in positions else ZERO
            amount = money(min(max(ZERO, decimal(weight)*equity-current), core_room, remaining))
            if amount >= MIN_BUY:
                plan.append({"symbol": symbol, "notional": amount, "block": "core"})
                remaining -= amount
                core_room -= amount
    held = set(positions) & set(SWING_UNIVERSE)
    swing_room = max(ZERO, SWING_WEIGHT*equity - block_value(positions, SWING_UNIVERSE, indicators) - reserved)
    candidates = (swing_entries(indicators, held, excluded_symbols=excluded) if entry_symbols is None else
                  [s for s in entry_symbols if s not in held and s not in excluded
                   and entry_candidate(indicators[s])]
                  [:max(0, 2-len(held)-reserved_slots)])
    for symbol in candidates:
        amount = money(min(SLOT_WEIGHT*equity, swing_room, remaining))
        if amount >= MIN_BUY:
            plan.append({"symbol": symbol, "notional": amount, "block": "swing"})
            remaining -= amount
            swing_room -= amount
    return plan


def required_quote_symbols(indicators, positions, decision):
    # Same-session RSI needs today's quote for every swing symbol. Fixtures
    # without roll state are final values: RSI >= 10 can never qualify.
    needed = {s for s in SWING_UNIVERSE
              if indicators[s].roll is not None or indicators[s].rsi2 < 10} | set(positions)
    if decision is not None:
        needed.update(decision.targets)
    if needed - set(UNIVERSE):
        raise SafetyError("Quote scope includes an asset outside the strategy universe")
    return tuple(s for s in UNIVERSE if s in needed)


def refresh_indicators(indicators, data, broker, positions, decision):
    symbols = required_quote_symbols(indicators, positions, decision)
    prices = data.prices(symbols, lambda: broker.clock().timestamp)
    if set(symbols) - prices.keys():
        raise SafetyError("Missing required quotes during refresh")
    # Unrequested prices stay explicitly absent. Never substitute yesterday's
    # close or retain an earlier quote for an asset outside the current scope.
    return {s: replace(ind, price=prices[s] if s in symbols else None) for s, ind in indicators.items()}


def snapshot_indicators(summary, indicators):
    """Keep the audit snapshot aligned with the latest successful quote refresh."""
    summary.update(quoted_symbols=[s for s, ind in indicators.items() if ind.price is not None],
                   indicators={s: asdict(ind) for s, ind in indicators.items()})


def run(config, broker, data, summary, claim=claim_session, observe=None, core_store=None):
    # Calendar and Clock are always the first account-side operations.
    clock = broker.clock()
    day = aware(clock.timestamp).astimezone(NY).date()
    friday = day + timedelta(days=4-day.weekday())
    window = resolve_window(clock, broker.calendar(day, max(day, friday)))
    summary.update(session_date=day.isoformat(), mode="paper" if config.paper else "live",
                   dry_run=config.dry_run, feed=config.feed,
                   history_feed=config.history_feed or
                   ("sip" if config.feed == "delayed_sip" else config.feed),
                   signal_price_delay_minutes=15 if config.feed == "delayed_sip" else 0,
                   orders=[], planned_orders=[],
                   protected=[], regime="NOT_EVALUATED", status="skipped")
    if window is None:
        if summary.get("prestart_approved"):
            raise SafetyError("Market session became unavailable after prestart approval; no orders submitted")
        summary["reason"] = "Market closed or no session today"
        return
    if not window.accepts_start(clock.timestamp):
        if summary.get("prestart_approved"):
            raise SafetyError("Start window expired after prestart approval; no orders submitted")
        summary["reason"] = "Outside calendar close-minus-15-minute start window (5-minute grace)"
        return
    account = broker.account()
    if not config.dry_run:
        observer = record_evaluation_attempt if observe is None else observe
        observer(account.id, day, config.paper)
        summary["evaluation_recorded"] = True
    validate_account(account)
    equity = decimal(account.equity)
    summary.update(equity=str(equity), pnl_day=str(equity-decimal(account.last_equity)),
                   pnl_day_note="Equity minus last_equity; deposits/withdrawals are not adjusted",
                   equity_snapshot="evaluation_start",
                   weekly_rebalance=window.weekly_rebalance,
                   target_time=window.target.isoformat())
    broker.bind_context(window, summary)
    broker.assert_no_open_orders()
    if any(str(o.client_order_id).startswith(f"eod-v1-{day:%Y%m%d}-") for o in broker.orders(day)):
        summary["reason"] = "Bot orders already exist today; conservative no-retry policy"
        return
    positions = broker.positions()
    assert_portfolio(positions)
    # Simulations keep their existing no-state/no-mutation behavior. Effective
    # runs require the same persistent state scope as the independent daily claim.
    store = None if config.dry_run else (core_store if core_store is not None else
                                        CoreRecoveryStore(account.id, config.paper))
    pending, core_blocked, catch_up = None, None, None
    try:
        pending = store.pending(day) if store is not None else None
        if pending is not None:
            origin = aware(pending["started_at"]).astimezone(NY).date()
            span = (day-origin).days
            if not 0 <= span <= MAX_RECOVERY_DAYS:
                raise StateError("Core recovery history outside bounded date range")
            if span == 0:
                summary.update(status="skipped", reason="Core intent already exists today; no same-day retry")
                return
            orders, fills = [], []
            # Include every calendar date: a historical order or FILL may not be
            # inferred absent merely because a date was not a normal session.
            for offset in range(span+1):
                historical_day = origin+timedelta(days=offset)
                orders.extend(broker.orders(historical_day))
                fills.extend(broker.fills_today(historical_day))
            recovery = reconcile_intent(pending, orders, fills, positions, day)
            summary["core_recovery"] = dict(recovery, status="awaiting_signal")
        elif store is not None and not window.weekly_rebalance:
            # A weekly session that never evaluated (scheduler miss) leaves no
            # intent. Review the core on the first later session instead of
            # waiting a further week. Sessions that did evaluate always record
            # an intent, so this cannot repeat a completed review. An account
            # with no mandate at all still starts on its first weekly session.
            latest = store.latest_intent_date()
            monday = day - timedelta(days=day.weekday())
            earlier = [] if latest is None else sorted(
                c.date for c in broker.calendar(day-timedelta(days=14), day-timedelta(days=1))
                if c.date < monday)
            if earlier and latest < earlier[-1]:
                catch_up = earlier[-1]
                summary["core_catch_up"] = {"missed_weekly_session": catch_up.isoformat(),
                                            "latest_intent": latest.isoformat() if latest else None}
    except (SafetyError, StateError) as exc:
        # Uncertain core state must not strand swing holdings without their
        # exits. Core orders and every purchase stay blocked for this session.
        pending, catch_up, core_blocked = None, None, str(exc)
        summary["core_recovery"] = {"status": "blocked", "reason": core_blocked, "pending": True}
    if pending is not None:
        broker.assert_no_open_orders()
        positions, account = broker.positions(), broker.account()
        assert_portfolio(positions)
        validate_account(account)
    core_due = (window.weekly_rebalance or catch_up is not None) and core_blocked is None
    summary["core_due"] = core_due
    broker.assert_assets(UNIVERSE)
    indicators = data.load(UNIVERSE, window, broker, quote_symbols=())
    fresh = core_targets(indicators) if core_due or pending is not None else None
    decision = fresh if core_due else (
        select_recovery_decision(pending, fresh, day) if pending is not None else None)
    if pending is not None:
        summary["core_recovery"].update(
            status="weekly_supersession" if window.weekly_rebalance else
                   "eligible" if decision is not None else "held_signal_changed",
            fresh_regime=fresh.regime, fresh_targets=fresh.targets)
    summary.update(regime=decision.regime if decision else
                   "CORE_BLOCKED" if core_blocked else "HOLD_UNTIL_WEEK_END",
                   core_targets=decision.targets if decision else {},
                   core_scores=decision.scores if decision else {},
                   signal_as_of=indicators["SPY"].as_of)
    intent = pending

    def before_orders():
        nonlocal intent
        if decision is not None and core_due:
            intent = store.begin(day, decision, positions, account.equity,
                                 started_at=clock.timestamp,
                                 signal_as_of=indicators["SPY"].as_of, supersedes=pending)
        if intent is not None:
            summary.setdefault("core_recovery", {}).update(
                intent_date=intent["session_date"], pending=True)

    def after_core(current, prices, final_account, active):
        if intent is None:
            return True
        if decision is None:
            summary["core_recovery"]["pending"] = True
            summary["core_status"] = "held_signal_changed"
            return False
        protected = [p["symbol"] for p in summary["protected"] if p["symbol"] in CORE]
        finished = store.finish(intent, day, current, prices, final_account.equity,
                                protected=protected, frozen=not active)
        summary.setdefault("core_recovery", {}).update(
            pending=not finished, status="completed" if finished else "pending_allocation")
        return finished

    from .execution import execute_strategy
    execute_strategy(config, broker, data, summary, window, account, positions, indicators, decision, claim,
                     before_orders=before_orders, after_core=after_core,
                     exits_only_reason=core_blocked)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary = {"status": "error", "orders": []}
    code = 0
    try:
        config = Config.from_env()
        summary.update(mode="paper" if config.paper else "live", dry_run=config.dry_run,
                       data_feed=config.feed, regime="NOT_EVALUATED")
        broker = Broker(config.key, config.secret, config.paper)
        # A separate read-only probe never contributes quotes to execution.
        def preflight(window):
            from .quotes import QuoteBudget
            probe = MarketData(config.key, config.secret, config.feed)
            batch = probe.quote_batch(UNIVERSE, lambda: broker.clock().timestamp,
                                      budget=QuoteBudget(seconds=8, max_attempts=1), deadline=window.target)
            summary["preflight_quotes"] = {"failures": batch.failures, "diagnostics": batch.diagnostics}
        outcome = prepare_start(
            broker, summary, event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
            scheduled_cron=os.environ.get("EOD_SCHEDULED_CRON", ""),
            wait_for_window=bool_env(os.environ, "EOD_WAIT_FOR_WINDOW", False), preflight=preflight,
        )
        if outcome == "ready":
            summary["prestart_approved"] = True
            run(config, broker, MarketData(config.key, config.secret, config.feed,
                                           history_feed=config.history_feed or None), summary)
        elif outcome == "missed_window" and config.dry_run:
            summary.update(status="skipped", reason="Late simulation; no trading or alert marker consumed")
        elif outcome == "missed_window":
            account = broker.account()  # Read-only identity for the separate alert marker.
            if claim_missed_window(account.id, summary["session_date"], config.paper):
                summary.update(
                    status="missed_window",
                    reason=("Se perdió la ventana EOD: inicio permitido desde "
                            f"{summary['target_time']} hasta antes de {summary['start_deadline']}; "
                            f"ejecución recibida {summary['observed_at']}. "
                            "No hay registro de evaluación EOD ni de sesión de órdenes. "
                            "Esta ejecución no envió órdenes."),
                )
                code = 1
            else:
                summary.update(status="skipped", reason="Evaluation recorded, session claimed, or missed-window alert already reserved")
        elif outcome != "skipped":
            raise SafetyError("Unexpected calendar prestart result")
    except (SafetyError, StrategyDataError, StateError) as exc:
        # These are controlled messages; upstream HTTP exception strings can contain secrets.
        summary.update(status="blocked", reason=str(exc))
        LOG.error("Execution blocked: %s", exc)
        code = 1
    except Exception as exc:
        summary.update(status="error", reason=f"External/internal failure: {type(exc).__name__}; inspect provider status")
        LOG.error("Execution aborted (%s); no automatic order resubmission", type(exc).__name__)
        code = 1
    finally:
        serializable = json.loads(json.dumps(summary, default=str, allow_nan=False))
        # Save before notifying so notification failures cannot erase execution evidence.
        Path("run_summary.json").write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")
        LOG.info("Result: %s | %s", summary["status"], summary.get("reason", ""))
        # Alternative cron families and holidays stay quiet; a missed eligible
        # session alerts once through a marker separate from the trading claim.
        if summary["status"] != "skipped":
            try:
                delivery = notify(serializable)
            except Exception:
                delivery = {"internal_error": False}
            serializable["notification_delivery"] = delivery
            if any(value is not True for value in delivery.values()):
                LOG.error("Notification delivery was not confirmed; execution will not be retried")
                code = 1
            Path("run_summary.json").write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
