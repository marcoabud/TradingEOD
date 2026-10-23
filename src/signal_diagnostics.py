"""Reconstruct signals from completed bars and historical quotes, with no orders.

This is a flat-account hypothetical evaluation, not a trading entry point or a
claim that a fill would have occurred. It never reads account/positions/orders.
"""
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

from .bot import UNIVERSE
from .broker import Broker
from .data import MarketData, quote_price
from .execution import ranked_candidates, select_slots
from .safety import NY, SafetyError, Window, aware, session_time
from .strategy import StrategyDataError, SWING_UNIVERSE, core_targets


class HistoricalQuotes:
    """Read only recorded quotes, through the live candidate-selection contract.

    This adapter does not own an HTTP client or broker and cannot submit orders.
    An omitted candidate is not replaced by a lower-ranked candidate. A quote
    proving price below SMA200 can release its slot, as in the live evaluator.
    """
    def __init__(self, indicators, rows, reference, result):
        self.indicators = {s: replace(i, price=None) for s, i in indicators.items()}
        self.rows, self.reference, self.result = rows, reference, result

    def fetch(self, symbols, phase):
        failed = set()
        for symbol in symbols:
            if symbol not in self.result["required_quotes"]:
                self.result["required_quotes"].append(symbol)
            q = self.rows.get(symbol, {})
            if not isinstance(q, dict):
                raise SafetyError("Malformed historical quote result")
            if "error" in q or q.get("http_status", 200) != 200:
                # Entitlement, transport and malformed-response failures are
                # global errors in the live collector, not per-symbol omissions.
                raise SafetyError("Required historical quote request failed")
            quote = SimpleNamespace(timestamp=q.get("timestamp"),
                                    bid_price=q.get("bid_price"), ask_price=q.get("ask_price"),
                                    bid_size=q.get("bid_size"), ask_size=q.get("ask_size"))
            try:
                price = quote_price(quote, self.reference, symbol)
                self.indicators[symbol] = replace(self.indicators[symbol], price=price)
            except SafetyError as exc:
                self.indicators[symbol] = replace(self.indicators[symbol], price=None)
                self.result["quote_failures"][symbol] = str(exc)
                failed.add(symbol)
        return failed


def reconstruct_signals(quote_report, environ, *, broker_factory=Broker, data_factory=MarketData):
    result = {"status": "blocked", "read_only": True,
              "assumption": "Flat account; historical reconstruction; no orders or fills",
              "limitation": "One historical snapshot cannot reproduce retries, later quote refreshes or fills"}
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
        result.update(history_as_of=indicators["SPY"].as_of,
                      weekly_rebalance=window.weekly_rebalance,
                      regime=decision.regime if decision else "HOLD_UNTIL_WEEK_END",
                      rsi2={s: round(indicators[s].rsi2, 6) for s in SWING_UNIVERSE},
                      required_quotes=[], quote_failures={})
        quotes = HistoricalQuotes(indicators, quote_report["historical_iex"], reference, result)
        core_failed = quotes.fetch(decision.targets if decision else (), "core_targets")
        selected, reserved = select_slots(quotes, ranked_candidates(indicators, ()), ())
        result.update(status="degraded" if result["quote_failures"] else "completed",
                      hypothetical_swing_entries=selected,
                      hypothetical_core_targets=decision.targets if decision and not core_failed else {},
                      hypothetical_core_signal_targets=decision.targets if decision else {},
                      core_status="frozen" if core_failed else "ready" if decision else "not_due",
                      reserved_slots=len(reserved), reserved_symbols=reserved,
                      reserved_equity_weight=round(len(reserved) * .15, 2),
                      reason="Hypothetical signals with per-symbol quote omissions; no orders or fills")
    except (SafetyError, StrategyDataError) as exc:
        result["reason"] = str(exc)
    except Exception as exc:
        result["reason"] = f"Historical signal diagnostic failed ({type(exc).__name__})"
    return result
