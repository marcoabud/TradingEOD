"""Per-asset data omissions with a strict, irreversible sell/buy phase barrier."""
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace

from .data import quote_price
from .quotes import QuoteBudget, QuoteFatalError, inspect_quote
from .safety import SafetyError, decimal, usable_cash, validate_account, quantity
from .strategy import SWING_UNIVERSE, StrategyDataError, swing_exit_reasons


class QuoteOmission(SafetyError):
    """Local quote validation failed BEFORE submission; no order was sent."""
    def __init__(self, symbols):
        self.failed_symbols = frozenset(symbols)
        super().__init__("Quote no longer valid before submission")


class QuoteSession:
    def __init__(self, data, broker, window, indicators, summary):
        self.data, self.broker, self.window = data, broker, window
        self.indicators = {s: replace(i, price=None) for s, i in indicators.items()}
        self.summary, self.budget, self.quotes = summary, QuoteBudget(), {}
        summary.update(data_issues=[], quote_checks=[], reserved_slots=0, reserved_cash="0.00",
                       buy_blocked=False, evaluation_outcome="evaluating")

    def issue(self, symbol, phase, reason, attempts=0):
        row = dict(symbol=symbol, phase=phase, reason=reason, attempts=attempts)
        if row not in self.summary["data_issues"]:
            self.summary["data_issues"].append(row)

    def fetch(self, symbols, phase):
        symbols = tuple(dict.fromkeys(symbols))
        if phase in {"exit_evaluation", "post_sell_valuation", "buy_refresh", "dry_buy_valuation"}:
            for symbol in set(self.indicators) - set(symbols):
                self.indicators[symbol] = replace(self.indicators[symbol], price=None)
                self.quotes.pop(symbol, None)
        try:
            batch = self.data.quote_batch(symbols, lambda: self.broker.clock().timestamp,
                                         budget=self.budget,
                                         deadline=self.window.closes-timedelta(minutes=5))
        except QuoteFatalError as exc:
            self.issue("ALL", phase, exc.cause)
            self.summary["quote_checks"].append(dict(phase=phase, checked_at=None, diagnostics=exc.diagnostics))
            raise
        if set(batch.prices) | set(batch.failures) != set(symbols) or set(batch.prices) & set(batch.failures):
            raise SafetyError("Quote result does not account for every requested symbol")
        for symbol in symbols:
            self.indicators[symbol] = replace(self.indicators[symbol], price=batch.prices.get(symbol))
            self.quotes.pop(symbol, None)
            if symbol in batch.prices:
                if symbol not in batch.quotes:
                    raise SafetyError("Validated quote has no auditable snapshot")
                self.quotes[symbol] = batch.quotes[symbol]
            else:
                self.issue(symbol, phase, batch.failures[symbol],
                           batch.diagnostics.get(symbol, {}).get("attempts", 0))
        times = [d["checked_at"] for d in batch.diagnostics.values() if d.get("checked_at")]
        self.summary["quote_checks"].append(dict(phase=phase, checked_at=max(times) if times else None,
                                                diagnostics=batch.diagnostics))
        self.summary.update(quote_budget_seconds=round(self.budget.used_seconds, 3),
                            quoted_symbols=[s for s, i in self.indicators.items() if i.price is not None],
                            indicators={s: asdict(i) for s, i in self.indicators.items()})
        return set(batch.failures)

    def guard(self, symbols, now):
        failed = set()
        diagnostics = {}
        for symbol in symbols:
            _, _, diag = inspect_quote(self.quotes.get(symbol), now, feed=self.summary["feed"])
            diagnostics[symbol] = dict(diag, checked_at=now.isoformat(), attempts=0, recovered=False)
            try:
                quote_price(self.quotes.get(symbol), now, symbol)
            except SafetyError:
                self.issue(symbol, "pre_submit", "quote_expired_before_submission")
                failed.add(symbol)
        self.summary["quote_checks"].append(dict(phase="pre_submit", checked_at=now.isoformat(),
                                                diagnostics=diagnostics))
        if failed:
            raise QuoteOmission(failed)


def ranked_candidates(indicators, excluded):
    return sorted((s for s in SWING_UNIVERSE if s not in excluded and indicators[s].rsi2 < 10),
                  key=lambda s: indicators[s].rsi2)


def select_slots(quotes, ranked, occupied):
    """Unknown eligibility reserves a slot; known trend rejection releases it."""
    remaining, selected, reserved = max(0, 2-len(set(occupied))), [], []
    pending = list(ranked)
    while remaining and pending:
        group, pending = pending[:remaining], pending[remaining:]
        failed = quotes.fetch(group, "entry_candidates")
        for symbol in group:
            ind = quotes.indicators[symbol]
            if symbol in failed:
                reserved.append(symbol)
                remaining -= 1
            elif ind.price > ind.sma200:
                selected.append(symbol)
                remaining -= 1
    return selected, reserved


def execute_strategy(config, broker, data, summary, window, account, positions, indicators, decision, claim):
    # Local import keeps the existing pure allocation helpers available to callers.
    from .bot import CORE, ZERO, assert_portfolio, plan_buys, plan_sells, reserved_cash_amount

    q = QuoteSession(data, broker, window, indicators, summary)
    core_active = decision is not None
    core_started = False
    summary["core_status"] = "ready" if core_active else "not_due"

    def freeze_core():
        nonlocal core_active
        core_active = False
        summary["core_status"] = "incomplete" if core_started else "frozen"

    def mark_core_started():
        nonlocal core_started
        core_started = True
        # Submission can be partial or uncertain. Only the successful end of
        # the complete sequence may replace this status with "completed".
        summary["core_status"] = "incomplete"

    def held_failures(failed, current):
        if failed & set(current):
            summary["buy_blocked"] = True

    def core_symbols(current):
        return tuple(s for s in CORE if s in current or decision and s in decision.targets)

    def record_reserve(current, current_account, reserved, cash=None):
        try:
            amount = reserved_cash_amount(current, q.indicators, decimal(current_account.equity),
                                          usable_cash(current_account) if cash is None else cash, len(reserved))
        except StrategyDataError:
            amount = None
        summary.update(reserved_slots=len(reserved), reserved_symbols=list(reserved),
                       reserved_cash=str(amount) if amount is not None else "unavailable")

    needed = list(positions) + (list(decision.targets) if decision else [])
    failed = q.fetch(needed, "exit_evaluation")
    held_failures(failed, positions)
    if core_active and failed & set(core_symbols(positions)):
        freeze_core()
    bought_today, sold_today = broker.traded_symbols(window.day, "buy"), broker.traded_symbols(window.day, "sell")
    valid_positions = {s: p for s, p in positions.items() if s not in failed}
    sells, core_buys, protected = plan_sells(valid_positions, q.indicators, decimal(account.equity),
                                           decision if core_active else None, bought_today)
    summary["protected"] = protected
    summary["planned_orders"] = [{"symbol": s, "side": "sell", **v} for s, v in sells.items()]
    # Freeze the RSI ordering and exclusions before any orders. Eligibility and
    # actual free slots are checked only after confirmed exits.
    ranked = ranked_candidates(indicators, bought_today | sold_today | set(sells) | set(positions))
    broker.assert_time(window)
    if config.dry_run:
        current = dict(positions)
        cash = usable_cash(account)
        for symbol, sale in sells.items():
            p = current[symbol]
            cash += sale["qty"] * decimal(q.indicators[symbol].price)
            left = decimal(p.qty)-sale["qty"]
            if left <= 0:
                del current[symbol]
            else:
                current[symbol] = SimpleNamespace(symbol=symbol, qty=left, avg_entry_price=p.avg_entry_price)
        if not summary["buy_blocked"]:
            chosen, reserved = select_slots(q, ranked, set(current) & set(SWING_UNIVERSE))
            failed = q.fetch(list(current) + chosen + (list(decision.targets) if core_active else []), "dry_buy_valuation")
            held_failures(failed, current)
            if core_active and failed & set(core_symbols(current)):
                freeze_core()
            reserved += [s for s in chosen if s in failed]
            chosen = [s for s in chosen if s not in failed]
            record_reserve(current, account, reserved, cash)
            if not summary["buy_blocked"]:
                buys = plan_buys(current, q.indicators, decimal(account.equity), cash,
                                 decision if core_active else None, core_buys, bought_today | sold_today | set(sells),
                                 entry_symbols=chosen, reserved_slots=len(reserved))
                summary["planned_orders"] += [{"side": "buy", **b} for b in buys]
        summary.update(status="dry_run", evaluation_outcome="degraded" if summary["data_issues"] else "planned",
                       reason="Hypothetical plan only; no submitted orders or actual sale proceeds")
        return
    if not claim(account.id, window.day, config.paper):
        summary.update(status="skipped", reason="Persistent daily claim already exists; no same-day retry")
        return
    summary["session_claimed"] = True
    sold = {}
    for symbol, sale in sells.items():
        if symbol in CORE and not core_active:
            continue
        current = broker.positions()
        assert_portfolio(current)
        group = core_symbols(current) if symbol in CORE else (symbol,)
        failed = q.fetch(group, "sell_refresh")
        held_failures(failed, current)
        if symbol in CORE and failed:
            freeze_core()
            continue
        if symbol in failed or symbol not in current:
            continue
        refreshed_account = broker.account()
        validate_account(refreshed_account)
        if symbol in CORE:
            revised, _, _ = plan_sells({s: p for s, p in current.items() if s in CORE}, q.indicators,
                                       decimal(refreshed_account.equity), decision, broker.traded_symbols(window.day, "buy"))
            if symbol not in revised:
                continue
            qty = min(sale["qty"], revised[symbol]["qty"], quantity(current[symbol].qty))
        else:
            if not swing_exit_reasons(q.indicators[symbol], float(current[symbol].avg_entry_price)):
                continue
            qty = min(sale["qty"], quantity(current[symbol].qty))
        before = decimal(current[symbol].qty)
        records_before = len(summary["orders"])
        try:
            broker.execute(symbol=symbol, side="sell", amount=qty, window=window, summary=summary,
                           pre_submit=lambda now, g=group: q.guard(g, now))
        except QuoteOmission as exc:
            held_failures(exc.failed_symbols, current)
            if symbol in CORE:
                freeze_core()
            continue
        except Exception:
            if symbol in CORE and len(summary["orders"]) > records_before:
                mark_core_started()
                freeze_core()
            raise
        if symbol in CORE:
            mark_core_started()
        sold[symbol] = (before, qty)
    # Any order exception above propagates. Only local pre-submission data
    # omissions may continue; a rejected/partial/uncertain order never can.
    broker.assert_no_open_orders()
    current = broker.positions()
    assert_portfolio(current)
    for symbol, (before, qty) in sold.items():
        actual = decimal(current[symbol].qty) if symbol in current else ZERO
        if actual > before-qty or symbol in SWING_UNIVERSE and symbol in current:
            raise SafetyError("Position has not reconciled after confirmed sale")
    account = broker.account()
    validate_account(account)
    failed = q.fetch(list(current) + (list(decision.targets) if core_active else []), "post_sell_valuation")
    held_failures(failed, current)
    if core_active and failed & set(core_symbols(current)):
        freeze_core()
    excluded = broker.traded_symbols(window.day, "buy") | broker.traded_symbols(window.day, "sell") | set(sells)
    chosen, reserved = [], []
    if not summary["buy_blocked"]:
        chosen, reserved = select_slots(q, [s for s in ranked if s not in excluded and s not in current],
                                        set(current) & set(SWING_UNIVERSE))
    record_reserve(current, account, reserved)
    buys = []
    if not summary["buy_blocked"]:
        buys = plan_buys(current, q.indicators, decimal(account.equity), usable_cash(account),
                         decision if core_active else None, core_buys, excluded,
                         entry_symbols=chosen, reserved_slots=len(reserved))
    summary["planned_orders"] += [{"side": "buy", **b} for b in buys]
    for buy in buys:
        symbol = buy["symbol"]
        if summary["buy_blocked"] or buy["block"] == "core" and not core_active:
            continue
        account = broker.account()
        validate_account(account)
        current = broker.positions()
        assert_portfolio(current)
        group = tuple(dict.fromkeys(list(current) + [symbol] +
                      (list(decision.targets) if core_active else [])))
        failed = q.fetch(group, "buy_refresh")
        held_failures(failed, current)
        if core_active and failed & set(core_symbols(current)):
            freeze_core()
        if symbol in failed and buy["block"] == "swing":
            reserved.append(symbol)
        if summary["buy_blocked"] or symbol in failed or buy["block"] == "core" and not core_active:
            continue
        updated = plan_buys(current, q.indicators, decimal(account.equity), usable_cash(account),
                            decision if core_active else None, core_buys, excluded,
                            entry_symbols=[symbol] if buy["block"] == "swing" else [],
                            reserved_slots=len(reserved))
        match = next((p for p in updated if p["symbol"] == symbol), None)
        if match is None:
            continue
        guard_group = tuple(dict.fromkeys(list(current) + [symbol] +
                            (list(decision.targets) if buy["block"] == "core" else [])))
        records_before = len(summary["orders"])
        try:
            broker.execute(symbol=symbol, side="buy", amount=min(buy["notional"], match["notional"]),
                           window=window, summary=summary, pre_submit=lambda now, g=guard_group: q.guard(g, now))
        except QuoteOmission as exc:
            # No submission occurred; keep this allocation in cash and never
            # promote a replacement candidate from the frozen ranking.
            held_failures(exc.failed_symbols, current)
            if buy["block"] == "core":
                freeze_core()
            else:
                reserved.append(symbol)
            continue
        except Exception:
            if buy["block"] == "core" and len(summary["orders"]) > records_before:
                mark_core_started()
                freeze_core()
            raise
        if buy["block"] == "core":
            mark_core_started()
        excluded.add(symbol)
    account = broker.account()
    current = broker.positions()
    assert_portfolio(current)
    record_reserve(current, account, reserved)
    if core_active:
        summary["core_status"] = "completed"
    degraded = bool(summary["data_issues"])
    outcome = "data_omissions" if degraded else "orders_confirmed" if summary["orders"] else "no_orders_required"
    summary.update(status="degraded" if degraded else "completed", evaluation_outcome=outcome,
                   equity=str(account.equity), pnl_day=str(decimal(account.equity)-decimal(account.last_equity)),
                   cash_remaining=str(account.cash),
                   reason="Session complete with data omissions; inspect affected assets" if degraded else
                          "Orders confirmed" if summary["orders"] else "No orders required by evaluated signals and holdings")
