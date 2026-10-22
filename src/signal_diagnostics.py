"""Reconstruct signals from completed bars and historical quotes, with no orders.

This is a flat-account hypothetical evaluation, not a trading entry point or a
claim that a fill would have occurred. It never reads account/positions/orders.
"""
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

from .bot import required_quote_symbols, UNIVERSE
from .broker import Broker
from .data import MarketData, quote_price
from .safety import NY, SafetyError, Window, aware, session_time
from .strategy import StrategyDataError, SWING_UNIVERSE, core_targets, swing_entries


def reconstruct_signals(quote_report, environ, *, broker_factory=Broker, data_factory=MarketData):
    result = {"status": "blocked", "read_only": True,
              "assumption": "Flat account; historical reconstruction; no orders or fills"}
    try:
        if environ.get("ALPACA_PAPER", "true").strip().lower() not in ("", "true"):
            raise SafetyError("Signal diagnostic supports paper only")
        if quote_report.get("status") != "completed":
            raise SafetyError("Quote reconstruction is incomplete")
        day = date.fromisoformat(quote_report["session_date"])
        reference = aware(quote_report["reconstruction_time"])
        if reference.astimezone(NY).date() != day:
            raise SafetyError("Reconstruction timestamp does not match New York session date")
        key, secret = environ["ALPACA_API_KEY"], environ["ALPACA_SECRET_KEY"]
        broker = broker_factory(key, secret, True)
        friday = day + timedelta(days=4-day.weekday())
        calendar = {s.date: s for s in broker.calendar(day, max(day, friday))}
        if day not in calendar:
            raise SafetyError("No calendar session for reconstruction")
        session = calendar[day]
        window = Window(day, session_time(session.open, day), session_time(session.close, day),
                        not any(day < d <= friday for d in calendar))
        if not window.accepts_start(reference):
            raise SafetyError("Reconstruction timestamp is outside calendar start window")
        data = data_factory(key, secret, "iex")
        indicators = data.load(UNIVERSE, window, broker, quote_symbols=())
        decision = core_targets(indicators) if window.weekly_rebalance else None
        needed = required_quote_symbols(indicators, {}, decision)
        result.update(history_as_of=indicators["SPY"].as_of,
                      weekly_rebalance=window.weekly_rebalance,
                      regime=decision.regime if decision else "HOLD_UNTIL_WEEK_END",
                      rsi2={s: round(indicators[s].rsi2, 6) for s in SWING_UNIVERSE},
                      required_quotes=list(needed), quote_failures={})
        for symbol in needed:
            q = quote_report["historical_iex"].get(symbol, {})
            try:
                quote = SimpleNamespace(timestamp=q.get("timestamp"),
                                        bid_price=q.get("bid_price"), ask_price=q.get("ask_price"),
                                        bid_size=q.get("bid_size"), ask_size=q.get("ask_size"))
                price = quote_price(quote, reference, symbol)
                indicators[symbol] = replace(indicators[symbol], price=price)
            except SafetyError as exc:
                result["quote_failures"][symbol] = str(exc)
        if result["quote_failures"]:
            result["reason"] = "A required historical quote failed the unchanged live validation rules"
            return result
        result.update(status="completed", hypothetical_swing_entries=swing_entries(indicators, ()),
                      hypothetical_core_targets=decision.targets if decision else {},
                      reason="Signals evaluated from completed bars and required historical quotes; no orders")
    except (SafetyError, StrategyDataError) as exc:
        result["reason"] = str(exc)
    except Exception as exc:
        result["reason"] = f"Historical signal diagnostic failed ({type(exc).__name__})"
    return result
