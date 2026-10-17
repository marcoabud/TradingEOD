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
from .data import MarketData
from .notifier import notify
from .safety import (NY, SafetyError, aware, decimal, money, quantity,
                     resolve_window, usable_cash, validate_account)
from .state import StateError, claim_session
from .strategy import (DEFENSIVE_SYMBOL, OFFENSIVE_UNIVERSE, SWING_UNIVERSE,
                       StrategyDataError, core_targets, swing_entries, swing_exits)

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

    @classmethod
    def from_env(cls, env=os.environ):
        key, secret = env.get("ALPACA_API_KEY", ""), env.get("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            raise SafetyError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY")
        return cls(key, secret, bool_env(env, "ALPACA_PAPER"),
                   bool_env(env, "DRY_RUN"), env.get("ALPACA_DATA_FEED", "sip"))


def position_value(position, indicators):
    return decimal(position.qty) * decimal(indicators[position.symbol].price)


def block_value(positions, symbols, indicators):
    return sum((position_value(p, indicators) for s, p in positions.items() if s in symbols), ZERO)


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


def plan_buys(positions, indicators, equity, cash, decision, core_buys, excluded):
    """No leverage, no slot overbooking, no shifting unused alpha cash into core."""
    remaining = max(ZERO, money(cash - CASH_BUFFER))
    plan = []
    core_room = max(ZERO, CORE_WEIGHT*equity - block_value(positions, CORE, indicators))
    if decision:
        for symbol, weight in decision.targets.items():
            if symbol not in core_buys or symbol in excluded:
                continue
            current = position_value(positions[symbol], indicators) if symbol in positions else ZERO
            amount = money(min(max(ZERO, decimal(weight)*equity-current), core_room, remaining))
            if amount >= MIN_BUY:
                plan.append({"symbol": symbol, "notional": amount, "block": "core"})
                remaining -= amount
                core_room -= amount
    held = set(positions) & set(SWING_UNIVERSE)
    swing_room = max(ZERO, SWING_WEIGHT*equity - block_value(positions, SWING_UNIVERSE, indicators))
    candidates = swing_entries(indicators, held, excluded_symbols=excluded)
    for symbol in candidates:
        amount = money(min(SLOT_WEIGHT*equity, swing_room, remaining))
        if amount >= MIN_BUY:
            plan.append({"symbol": symbol, "notional": amount, "block": "swing"})
            remaining -= amount
            swing_room -= amount
    return plan


def refresh_indicators(indicators, data, broker):
    prices = data.prices(indicators.keys(), lambda: broker.clock().timestamp)
    return {s: replace(ind, price=prices[s]) for s, ind in indicators.items()}


def run(config, broker, data, summary, claim=claim_session):
    # Calendar and Clock are always the first account-side operations.
    clock = broker.clock()
    day = aware(clock.timestamp).astimezone(NY).date()
    friday = day + timedelta(days=4-day.weekday())
    window = resolve_window(clock, broker.calendar(day, max(day, friday)))
    summary.update(session_date=day.isoformat(), mode="paper" if config.paper else "live",
                   dry_run=config.dry_run, feed=config.feed, orders=[], planned_orders=[],
                   protected=[], regime="NOT_EVALUATED", status="skipped")
    if window is None:
        summary["reason"] = "Market closed or no session today"
        return
    if not window.accepts_start(clock.timestamp):
        summary["reason"] = "Outside calendar close-minus-15-minute start window (5-minute grace)"
        return
    account = broker.account()
    validate_account(account)
    equity = decimal(account.equity)
    summary.update(equity=str(equity), pnl_day=str(equity-decimal(account.last_equity)),
                   pnl_day_note="Equity minus last_equity; deposits/withdrawals are not adjusted",
                   weekly_rebalance=window.weekly_rebalance,
                   target_time=window.target.isoformat())
    broker.assert_no_open_orders()
    if any(str(o.client_order_id).startswith(f"eod-v1-{day:%Y%m%d}-") for o in broker.orders(day)):
        summary["reason"] = "Bot orders already exist today; conservative no-retry policy"
        return
    positions = broker.positions()
    assert_portfolio(positions)
    broker.assert_assets(UNIVERSE)
    indicators = data.load(UNIVERSE, window, broker)
    decision = core_targets(indicators) if window.weekly_rebalance else None
    summary.update(regime=decision.regime if decision else "HOLD_UNTIL_WEEK_END",
                   core_targets=decision.targets if decision else {},
                   core_scores=decision.scores if decision else {},
                   signal_as_of=indicators["SPY"].as_of,
                   indicators={s: asdict(i) for s, i in indicators.items()})
    bought_today = broker.traded_symbols(day, "buy")
    sold_today = broker.traded_symbols(day, "sell")
    sells, core_buys, protected = plan_sells(positions, indicators, equity, decision, bought_today)
    summary["protected"] = protected
    summary["planned_orders"] = [{"symbol": s, "side": "sell", **v} for s, v in sells.items()]
    broker.assert_time(window)
    if config.dry_run:
        # Hypothetical post-sale cash is labelled and never treated as an actual fill.
        simulated_positions = dict(positions)
        cash = usable_cash(account)
        for symbol, sale in sells.items():
            p = simulated_positions[symbol]
            cash += sale["qty"] * decimal(indicators[symbol].price)
            left = decimal(p.qty) - sale["qty"]
            if left <= 0:
                del simulated_positions[symbol]
            else:
                from types import SimpleNamespace
                simulated_positions[symbol] = SimpleNamespace(symbol=symbol, qty=left, avg_entry_price=p.avg_entry_price)
        buys = plan_buys(simulated_positions, indicators, equity, cash, decision, core_buys,
                         bought_today | sold_today | set(sells))
        summary["planned_orders"] += [{"side": "buy", **b} for b in buys]
        summary.update(status="dry_run", reason="Hypothetical plan only; fills and proceeds are estimates")
        return
    if not claim(account.id, day, config.paper):
        summary["reason"] = "Persistent daily claim already exists; no same-day retry"
        return
    summary["session_claimed"] = True
    original_quantities = {s: decimal(p.qty) for s, p in positions.items()}
    # Strict phase barrier: any exception/partial/unknown sell aborts before buys.
    for symbol, sale in sells.items():
        broker.execute(symbol=symbol, side="sell", amount=sale["qty"], window=window, summary=summary)
        LOG.info("SELL confirmed: %s", symbol)
    broker.assert_no_open_orders()
    positions = broker.positions()
    for symbol, sale in sells.items():
        actual = decimal(positions[symbol].qty) if symbol in positions else ZERO
        if actual > original_quantities[symbol] - sale["qty"]:
            raise SafetyError(f"Position has not reconciled after sale: {symbol}")
        # A full exit must actually release its position before a slot can be reused.
        if symbol in SWING_UNIVERSE and symbol in positions:
            raise SafetyError(f"Swing position remains after confirmed exit: {symbol}")
    assert_portfolio(positions)
    indicators = refresh_indicators(indicators, data, broker)
    account = broker.account()
    validate_account(account)
    excluded = broker.traded_symbols(day, "buy") | broker.traded_symbols(day, "sell") | set(sells)
    buys = plan_buys(positions, indicators, decimal(account.equity), usable_cash(account),
                     decision, core_buys, excluded)
    summary["planned_orders"] += [{"side": "buy", **b} for b in buys]
    for buy in buys:
        # Reconcile actual cash, holdings and latest price before EACH buy.
        account = broker.account()
        validate_account(account)
        positions = broker.positions()
        assert_portfolio(positions)
        indicators = refresh_indicators(indicators, data, broker)
        updated = plan_buys(positions, indicators, decimal(account.equity), usable_cash(account),
                            decision, core_buys, excluded)
        match = next((p for p in updated if p["symbol"] == buy["symbol"]), None)
        if match is None:
            continue
        amount = min(buy["notional"], match["notional"])
        broker.execute(symbol=buy["symbol"], side="buy", amount=amount, window=window, summary=summary)
        excluded.add(buy["symbol"])
        LOG.info("BUY confirmed: %s USD %s", buy["symbol"], amount)
    account = broker.account()
    summary.update(status="completed", equity=str(account.equity),
                   pnl_day=str(decimal(account.equity)-decimal(account.last_equity)),
                   cash_remaining=str(account.cash), reason="Execution complete")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary = {"status": "error", "orders": []}
    code = 0
    try:
        config = Config.from_env()
        run(config, Broker(config.key, config.secret, config.paper),
            MarketData(config.key, config.secret, config.feed), summary)
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
        # Avoid four duplicate holiday/off-window messages from the four cron slots.
        if summary["status"] != "skipped":
            notify(serializable)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
