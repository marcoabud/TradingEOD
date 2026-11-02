"""Offline historical comparisons. This module never creates a broker or orders.

Adjusted bars represent synthetic total-return units, not executable raw shares.
Even timestamped historical marks model fills; neither input mode is a broker
replay. Runtime trading deliberately does not import this module.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd

from .safety import NY, aware
from .strategy import (DEFENSIVE_SYMBOL, OFFENSIVE_UNIVERSE, SWING_UNIVERSE,
                       CoreDecision, Indicators, compute_indicators, core_targets,
                       swing_entries, swing_exit_reasons)


UNIVERSE = (*OFFENSIVE_UNIVERSE, DEFENSIVE_SYMBOL, *SWING_UNIVERSE)
CORE = (*OFFENSIVE_UNIVERSE, DEFENSIVE_SYMBOL)
D = Decimal
SHARE = D("0.000000001")
CENT = D("0.01")
WARMUP = 250
VARIANTS = ("weekly", "daily_defensive", "trailing_core_5pct", "trailing_core_10pct")
TRAILING = {"trailing_core_5pct": .05, "trailing_core_10pct": .10}


class ResearchError(ValueError):
    """Inputs or simulated accounting cannot support a valid comparison."""


def number(value) -> Decimal:
    try:
        result = D(str(value))
    except Exception:
        raise ResearchError("Invalid numeric research input") from None
    if not result.is_finite():
        raise ResearchError("Non-finite research input")
    return result


def cents(value):
    return number(value).quantize(CENT, rounding=ROUND_DOWN)


def shares(value):
    return number(value).quantize(SHARE, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class Dataset:
    manifest: dict
    sessions: tuple[date, ...]
    bars: dict[str, pd.DataFrame]
    marks: dict[date, dict[str, float]]
    targets: dict[date, datetime]
    weekly: frozenset[date]


def _dates(series, label):
    try:
        values = [date.fromisoformat(str(v)) for v in series]
    except (ValueError, TypeError):
        raise ResearchError(f"Invalid {label} date") from None
    if any(str(v) != parsed.isoformat() for v, parsed in zip(series, values)):
        raise ResearchError(f"Noncanonical {label} date")
    return values


def _finite_frame(frame, names, label):
    try:
        values = frame[list(names)].to_numpy(dtype=float)
    except (ValueError, TypeError, KeyError):
        raise ResearchError(f"Invalid numeric columns: {label}") from None
    if not np.isfinite(values).all():
        raise ResearchError(f"Non-finite values: {label}")


def load_dataset(folder: Path) -> Dataset:
    """Validate full coverage and corporate-action price basis before simulating.

    All CSVs are checksum verified. Raw/split-only prices and mixed adjustment
    bases are refused: pretending they represent total returns biases SGOV and
    creates false moves around stock splits. No data are filled/interpolated.
    """
    folder = Path(folder)
    try:
        manifest_bytes = (folder / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, ValueError):
        raise ResearchError("Cannot read dataset manifest") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ResearchError("Unsupported research manifest")
    if manifest.get("adjustment") != "all" or manifest.get("price_basis") != "total_return_adjusted":
        raise ResearchError("Corporate-action-safe total-return-adjusted input required; raw prices refused")
    if not isinstance(manifest.get("source"), str) or not manifest["source"].strip():
        raise ResearchError("Dataset provenance source is required")
    if manifest.get("feed") not in ("sip", "iex", "delayed_sip"):
        raise ResearchError("Dataset feed must be explicit")
    try:
        retrieved = aware(manifest["retrieved_at"])
    except Exception:
        raise ResearchError("Dataset retrieval timestamp must be timezone-aware") from None
    if set(manifest.get("symbols", [])) != set(UNIVERSE) or len(manifest.get("symbols", [])) != len(UNIVERSE):
        raise ResearchError("Comparison requires the complete fixed 17-symbol universe")
    mode = manifest.get("execution_mode")
    required = {"daily.csv", "calendar.csv"}
    if mode == "window_marks":
        required.add("execution.csv")
        if manifest.get("execution_price_basis") != "total_return_adjusted":
            raise ResearchError("Execution marks and daily bars must share adjustment basis")
    elif mode != "adjusted_close_proxy":
        raise ResearchError("Unknown research execution mode")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != required:
        raise ResearchError("Manifest files must exactly describe the input CSVs")
    for name, digest in files.items():
        if not isinstance(digest, str) or len(digest) != 64:
            raise ResearchError("Invalid dataset digest")
        try:
            actual = hashlib.sha256((folder / name).read_bytes()).hexdigest()
        except OSError:
            raise ResearchError(f"Missing dataset file: {name}") from None
        if actual != digest:
            raise ResearchError(f"Dataset checksum mismatch: {name}")
    try:
        calendar = pd.read_csv(folder / "calendar.csv", dtype=str)
        daily = pd.read_csv(folder / "daily.csv")
    except Exception:
        raise ResearchError("Invalid input CSV") from None
    if not {"date", "open", "close"}.issubset(calendar.columns):
        raise ResearchError("Calendar requires date, open and close")
    session_list = _dates(calendar["date"], "calendar")
    if len(session_list) != len(set(session_list)) or session_list != sorted(session_list):
        raise ResearchError("Calendar sessions must be unique and ascending")
    if len(session_list) <= WARMUP:
        raise ResearchError("Dataset lacks 250 warmup sessions and evaluation data")
    targets = {}
    for row, day in zip(calendar.itertuples(index=False), session_list):
        try:
            opening, closing = aware(row.open).astimezone(NY), aware(row.close).astimezone(NY)
        except Exception:
            raise ResearchError("Calendar endpoints must be timezone-aware") from None
        if (opening.date() != day or closing.date() != day or closing <= opening
                or closing - opening < timedelta(minutes=30)):
            raise ResearchError("Inconsistent calendar session endpoints")
        if closing > retrieved:
            raise ResearchError("Dataset contains a session not complete at retrieval")
        targets[day] = closing - timedelta(minutes=15)
    required_daily = {"date", "symbol", "open", "high", "low", "close", "volume"}
    if not required_daily.issubset(daily.columns):
        raise ResearchError("Daily CSV missing required columns")
    daily["date"] = _dates(daily["date"], "daily")
    if daily.duplicated(["date", "symbol"]).any() or set(daily["symbol"]) != set(UNIVERSE):
        raise ResearchError("Duplicate daily bars or wrong symbol universe")
    _finite_frame(daily, ("open", "high", "low", "close", "volume"), "daily bars")
    if (daily[["open", "high", "low", "close"]] <= 0).any().any() or (daily.volume < 0).any():
        raise ResearchError("Invalid daily prices/volume")
    epsilon = daily.high.abs() * 1e-8
    if ((daily.high + epsilon < daily.low) | (daily.close > daily.high + epsilon)
            | (daily.close < daily.low - epsilon) | (daily.open > daily.high + epsilon)
            | (daily.open < daily.low - epsilon)).any():
        raise ResearchError("Inconsistent OHLC bar")
    bars = {}
    for symbol in UNIVERSE:
        frame = daily[daily.symbol == symbol].copy().set_index("date")
        if list(frame.index) != session_list:
            raise ResearchError(f"Missing, extra, or unordered calendar bars for {symbol}")
        bars[symbol] = frame
    if mode == "adjusted_close_proxy":
        marks = {day: {s: float(bars[s].loc[day, "close"]) for s in UNIVERSE} for day in session_list}
    else:
        marks = {day: {} for day in session_list}
        try:
            execution = pd.read_csv(folder / "execution.csv")
        except Exception:
            raise ResearchError("Invalid execution CSV") from None
        if not {"timestamp", "symbol", "price"}.issubset(execution.columns):
            raise ResearchError("Execution CSV requires timestamp, symbol and price")
        _finite_frame(execution, ("price",), "execution marks")
        for row in execution.itertuples(index=False):
            try:
                stamp = aware(row.timestamp).astimezone(NY)
            except Exception:
                raise ResearchError("Invalid execution timestamp") from None
            day = stamp.date()
            if (day not in marks or row.symbol not in UNIVERSE or row.price <= 0
                    or stamp != targets[day] or row.symbol in marks[day]):
                raise ResearchError("Duplicate, invalid or off-window execution mark")
            marks[day][row.symbol] = float(row.price)
        if any(set(v) != set(UNIVERSE) for v in marks.values()):
            raise ResearchError("Incomplete execution marks; no close fallback is allowed")
    weekly = frozenset(day for i, day in enumerate(session_list)
                       if i + 1 < len(session_list)
                       and session_list[i + 1].isocalendar()[:2] != day.isocalendar()[:2])
    # A terminal Friday is unambiguous; another terminal weekday needs the next
    # session in the supplied calendar to establish a holiday-shortened week.
    if session_list[-1].weekday() == 4:
        weekly = weekly | {session_list[-1]}
    manifest = {**manifest, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}
    return Dataset(manifest, tuple(session_list), bars, marks, targets, frozenset(weekly))


def prepare_signals(dataset: Dataset):
    """Use exactly 250 preceding complete bars; today's daily OHLC never enters."""
    result = {}
    for index in range(WARMUP, len(dataset.sessions)):
        day = dataset.sessions[index]
        result[day] = {symbol: compute_indicators(
            symbol, dataset.bars[symbol].iloc[index-WARMUP:index], dataset.marks[day][symbol],
        ) for symbol in UNIVERSE}
    return result


@dataclass
class Holding:
    qty: Decimal
    entry: Decimal
    bought: date


@dataclass
class Portfolio:
    cash: Decimal
    positions: dict[str, Holding] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)
    cost: Decimal = D(0)
    gross_traded: Decimal = D(0)

    def value(self, prices, symbols=UNIVERSE):
        return sum((h.qty * number(prices[s]) for s, h in self.positions.items() if s in symbols), D(0))

    def equity(self, prices):
        return self.cash + self.value(prices)

    def sell(self, day, symbol, qty, prices, cost_rate, reason):
        holding = self.positions[symbol]
        if holding.bought == day:
            raise ResearchError("ANTI_PDT: simulated same-day buy cannot be sold")
        qty = shares(qty)
        if qty <= 0 or qty > holding.qty:
            raise ResearchError("Invalid simulated sell quantity")
        gross = qty * number(prices[symbol])
        friction = gross * cost_rate
        self.cash += gross - friction
        holding.qty -= qty
        if holding.qty == 0:
            del self.positions[symbol]
        self._record(day, symbol, "sell", qty, gross, friction, reason)

    def buy(self, day, symbol, budget, prices, cost_rate, reason):
        if any(t["date"] == day.isoformat() and t["symbol"] == symbol and t["side"] == "sell"
               for t in self.trades[-len(UNIVERSE)*2:]):
            raise ResearchError("Simulated same-session reentry is forbidden")
        px = number(prices[symbol])
        gross_limit = cents(min(number(budget), max(D(0), self.cash - D(1)) / (1 + cost_rate)))
        if gross_limit < 1:
            return
        qty = shares(gross_limit / px)
        if qty <= 0:
            return
        gross, friction = qty * px, qty * px * cost_rate
        if gross + friction > self.cash:
            raise ResearchError("Simulated buy exceeds available cash")
        old = self.positions.get(symbol)
        # Match the broker's average execution price used by the ATR stop.
        # Friction reduces cash/equity; it must not raise the entry-price stop.
        entry_cost = gross + (old.qty * old.entry if old else D(0))
        total_qty = qty + (old.qty if old else D(0))
        self.positions[symbol] = Holding(total_qty, entry_cost / total_qty, day)
        self.cash -= gross + friction
        self._record(day, symbol, "buy", qty, gross, friction, reason)

    def _record(self, day, symbol, side, qty, gross, friction, reason):
        self.cost += friction
        self.gross_traded += gross
        self.trades.append({"date": day.isoformat(), "symbol": symbol, "side": side,
                            "qty": str(qty), "gross_notional": float(gross),
                            "cost": float(friction), "reason": reason})


def simulate(dataset: Dataset, signals: Mapping[date, dict[str, Indicators]], variant: str,
             cost_bps: float = 5, initial_equity: float = 4000):
    if variant not in VARIANTS or not math.isfinite(cost_bps) or not 0 <= cost_bps < 1000:
        raise ResearchError("Invalid variant or transaction cost")
    if not math.isfinite(initial_equity) or initial_equity <= 0:
        raise ResearchError("Initial equity must be positive")
    p = Portfolio(number(initial_equity))
    rate = number(cost_bps) / 10000
    rows, exits = [], []
    unit_index = peak = 1.0
    previous_prices = None
    for day in dataset.sessions[WARMUP:]:
        prices, ind = dataset.marks[day], signals[day]
        start_positions = set(p.positions)
        offensive = set(p.positions) & set(OFFENSIVE_UNIVERSE)
        if offensive and previous_prices:
            before = p.value(previous_prices, OFFENSIVE_UNIVERSE)
            unit_index *= float(p.value(prices, OFFENSIVE_UNIVERSE) / before)
            peak = max(peak, unit_index)
        elif not offensive:
            unit_index = peak = 1.0
        drawdown = max(0.0, 1.0-unit_index/peak)
        scheduled = day in dataset.weekly
        current_regime = core_targets(ind)
        decision = current_regime if scheduled else None
        trigger = "weekly" if scheduled else "hold"
        if variant == "daily_defensive" and offensive and current_regime.regime == "RISK-OFF":
            decision, trigger = current_regime, "daily_defensive"
        elif variant in TRAILING and offensive and drawdown >= TRAILING[variant]:
            decision = CoreDecision("RISK-OFF", {DEFENSIVE_SYMBOL: .7}, {}, ("CORE_TRAILING",))
            trigger = variant
        equity = p.equity(prices)
        sells, eligible_core, sold = {}, set(), set()
        if decision:
            for symbol in CORE:
                holding = p.positions.get(symbol)
                value = holding.qty * number(prices[symbol]) if holding else D(0)
                target = number(decision.targets.get(symbol, 0)) * equity
                diff = target - value
                if holding and abs(diff) / equity < D(".03"):
                    continue
                if diff > 0:
                    eligible_core.add(symbol)
                elif holding and diff < 0 and holding.bought < day:
                    qty = holding.qty if not target else shares(-diff / number(prices[symbol]))
                    if qty > 0:
                        sells[symbol] = (min(qty, holding.qty), trigger)
        for symbol in SWING_UNIVERSE:
            holding = p.positions.get(symbol)
            if holding and holding.bought < day:
                reasons = swing_exit_reasons(ind[symbol], float(holding.entry))
                if reasons:
                    sells[symbol] = (holding.qty, ",".join(reasons))
        exit_basket = {}
        for symbol, (qty, reason) in sells.items():
            if decision and decision.regime == "RISK-OFF" and symbol in OFFENSIVE_UNIVERSE:
                exit_basket[symbol] = {"price": prices[symbol], "notional": float(qty*number(prices[symbol]))}
            p.sell(day, symbol, qty, prices, rate, reason)
            sold.add(symbol)
        if exit_basket:
            exits.append({"date": day.isoformat(), "trigger": trigger, "basket": exit_basket})
        # Every sell is fully simulated before allocating any buy; cash, equity
        # and block room are refreshed after each modeled fill and its costs.
        if decision:
            for symbol, weight in decision.targets.items():
                if symbol not in eligible_core or symbol in sold:
                    continue
                equity = p.equity(prices)
                room = max(D(0), D(".70")*equity - p.value(prices, CORE))
                need = max(D(0), number(weight)*equity - p.value(prices, (symbol,)))
                p.buy(day, symbol, min(room, need), prices, rate, trigger)
        occupied = set(p.positions) & set(SWING_UNIVERSE)
        chosen = swing_entries(ind, occupied, excluded_symbols=start_positions | sold)
        for symbol in chosen:
            equity = p.equity(prices)
            room = max(D(0), D(".30")*equity - p.value(prices, SWING_UNIVERSE))
            p.buy(day, symbol, min(D(".15")*equity, room), prices, rate, "swing_entry")
        if p.cash < 0 or len(set(p.positions) & set(SWING_UNIVERSE)) > 2:
            raise ResearchError("Simulated cash/slot invariant violated")
        if not offensive and set(p.positions) & set(OFFENSIVE_UNIVERSE):
            unit_index = peak = 1.0
        close_prices = {s: float(dataset.bars[s].loc[day, "close"]) for s in UNIVERSE}
        closing_equity = p.equity(close_prices)
        rows.append({"date": day.isoformat(), "equity": float(closing_equity), "cash": float(p.cash),
                     "cost_cumulative": float(p.cost), "turnover_cumulative": float(p.gross_traded),
                     "core_value": float(p.value(close_prices, CORE)),
                     "offensive_value": float(p.value(close_prices, OFFENSIVE_UNIVERSE)),
                     "swing_value": float(p.value(close_prices, SWING_UNIVERSE)),
                     "regime_signal": current_regime.regime, "action": trigger,
                     "core_trailing_drawdown": drawdown,
                     "positions": {s: str(h.qty) for s, h in p.positions.items()}})
        previous_prices = prices
    return {"variant": variant, "cost_bps_each_side": cost_bps, "initial_equity": initial_equity,
            "rows": rows, "trades": p.trades, "defensive_exits": exits}


def metrics(run, baseline, dataset, start=0, end=None):
    rows = run["rows"][start:end]
    base_rows = baseline["rows"][start:end]
    if not rows or len(rows) != len(base_rows):
        raise ResearchError("Comparison segment is empty or misaligned")
    initial = run["rows"][start-1]["equity"] if start else run["initial_equity"]
    base_initial = baseline["rows"][start-1]["equity"] if start else baseline["initial_equity"]
    curve = np.array([initial, *[r["equity"] for r in rows]], dtype=float)
    base_curve = np.array([base_initial, *[r["equity"] for r in base_rows]], dtype=float)
    normalized, base_normalized = curve/initial*4000, base_curve/base_initial*4000
    changes, base_changes = np.diff(normalized), np.diff(base_normalized)
    cumulative_cost_before = run["rows"][start-1]["cost_cumulative"] if start else 0
    cumulative_turn_before = run["rows"][start-1]["turnover_cumulative"] if start else 0
    begin, finish = rows[0]["date"], rows[-1]["date"]
    trades = [t for t in run["trades"] if begin <= t["date"] <= finish]
    rebounds, followups = [], []
    for event in run["defensive_exits"]:
        if not begin <= event["date"] <= finish:
            continue
        index = dataset.sessions.index(date.fromisoformat(event["date"]))
        if index+5 >= len(dataset.sessions) or dataset.sessions[index+5].isoformat() > finish:
            continue
        later = dataset.marks[dataset.sessions[index+5]]
        total = sum(x["notional"] for x in event["basket"].values())
        rebound = sum(x["notional"]*(later[s]/x["price"]-1) for s, x in event["basket"].items())/total
        rebounds.append(rebound)
        followups.append({"date": event["date"], "after_5_sessions_return_pct": rebound*100})
    return {"start": begin, "end": finish, "sessions": len(rows),
            "start_equity": initial, "end_equity": float(curve[-1]),
            "net_return_pct": float((curve[-1]/initial-1)*100),
            "annualized_return_pct": float(((curve[-1]/initial)**(252/len(rows))-1)*100),
            "maximum_drawdown_pct": float(np.max(1-curve/np.maximum.accumulate(curve))*100),
            "transaction_cost_usd": rows[-1]["cost_cumulative"]-cumulative_cost_before,
            "gross_turnover_usd": rows[-1]["turnover_cumulative"]-cumulative_turn_before,
            "turnover_over_mean_equity": (rows[-1]["turnover_cumulative"]-cumulative_turn_before)/float(curve.mean()),
            "trade_count": len(trades), "sell_count": sum(t["side"] == "sell" for t in trades),
            "mean_offensive_weight": float(np.mean([r["offensive_value"]/r["equity"] for r in rows])),
            "net_return_difference_vs_weekly_pp": float((curve[-1]/initial-base_curve[-1]/base_initial)*100),
            "pnl_difference_on_weekly_loss_days_per_4000": float((changes-base_changes)[base_changes < 0].sum()),
            "defensive_exits_with_5_session_followup": len(rebounds),
            "exits_followed_by_positive_5_session_rebound": sum(x > 0 for x in rebounds),
            "mean_5_session_underlying_return_pct": float(np.mean(rebounds)*100) if rebounds else None,
            "exit_followups": followups}


LIMITATIONS = [
    "Simulación exploratoria con precios ajustados; no representa fills del broker ni prueba rentabilidad.",
    "El universo elegido hoy introduce sesgo de selección/supervivencia; no se modelan activos desaparecidos.",
    "El OHLC ajustado representa unidades sintéticas de retorno total e incluye supuestos de reinversión; las fracciones no son acciones históricas raw literales.",
    "El proveedor puede revisar ajustes por eventos corporativos; el manifest y los hashes conservan la muestra evaluada.",
    "El proxy de cierre observa y ejecuta al mismo cierre ajustado; no reproduce las 15:45 ni certifica retornos ejecutables.",
    "Incluso las marcas de la ventana carecen de profundidad bid/ask y fills individuales; el coste constante por lado es sensibilidad, no slippage observado.",
    "Los fills simulados son completos e inmediatos; no reproducen caídas del servicio, cotizaciones omitidas, filtros de spread, liquidación, rechazos o fills parciales.",
    "No se selecciona ni activa una estrategia. Los umbrales de 5% y 10% son comparaciones fijas, no inferidas de los snapshots de octubre.",
    "Los tramos desarrollo/OOS son cronológicos y conservan posiciones; no se ajustan parámetros. OOS no es independiente de la selección del universo.",
    "Solo se miden observaciones diarias. La máxima caída intradía y el retroceso intermedio de ganancias se desconocen.",
]


def compare(dataset: Dataset, costs=(1., 5., 10.), initial_equity=4000.):
    if not costs or len(set(costs)) != len(costs):
        raise ResearchError("Provide distinct transaction cost scenarios")
    count = len(dataset.sessions)-WARMUP
    split = int(count*.60)
    if split < 60 or count-split < 60:
        raise ResearchError("Need at least 60 development and 60 out-of-sample sessions after warmup")
    signals = prepare_signals(dataset)
    runs, comparison = [], []
    for cost in costs:
        cost_runs = [simulate(dataset, signals, variant, cost, initial_equity) for variant in VARIANTS]
        baseline = cost_runs[0]
        for run in cost_runs:
            for segment, lo, hi in (("full", 0, None), ("development", 0, split), ("out_of_sample", split, None)):
                comparison.append({"variant": run["variant"], "cost_bps_each_side": cost,
                                   "segment": segment, **metrics(run, baseline, dataset, lo, hi)})
        runs.extend(cost_runs)
    report = {"schema_version": 1, "research_only": True, "runtime_changes": False,
              "execution_mode": dataset.manifest["execution_mode"], "provenance": dataset.manifest,
              "warmup_sessions": WARMUP, "evaluation_sessions": count,
              "development_fraction": .60, "out_of_sample_start": dataset.sessions[WARMUP+split].isoformat(),
              "variants": list(VARIANTS), "cost_scenarios_bps_each_side": list(costs),
              "limitations": LIMITATIONS, "comparison": comparison}
    return report, runs


def write_results(output: Path, report, runs):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output/"comparison.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    rows = [r for r in report["comparison"] if r["segment"] == "out_of_sample"]
    lines = ["# Comparación histórica exploratoria — TradingEOD", "",
             "**Investigación únicamente. Ninguna variante se activó en el bot.**", "",
             f"Modo de precios: `{report['execution_mode']}`. Inicio fuera de muestra: {report['out_of_sample_start']}.",
             "Los rendimientos descuentan el coste por lado indicado. Son simulados; no son fills de Alpaca.", "",
             "| Variante | Coste/lado (pb) | Retorno OOS | Caída máxima OOS | Rotación/equity | Operaciones |",
             "|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(f"| {row['variant']} | {row['cost_bps_each_side']:g} | {row['net_return_pct']:.2f}% | "
                     f"{row['maximum_drawdown_pct']:.2f}% | {row['turnover_over_mean_equity']:.2f} | {row['trade_count']} |")
    lines.extend(["", "La comparación completa, los costes, diferencias en días negativos del caso base y salidas seguidas de rebote están en `comparison.json`.",
                  "", "## Límites", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    (output/"REPORT.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    for run in runs:
        name = f"{run['variant']}_{run['cost_bps_each_side']:g}bps.json"
        (output/name).write_text(json.dumps(run, indent=2, allow_nan=False), encoding="utf-8")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir()) if p.is_file()}
    (output/"RESULTS.sha256.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Research-only TradingEOD comparison; no broker or notifications")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--initial-equity", type=float, default=4000.)
    parser.add_argument("--cost-bps", nargs="+", type=float, default=[1., 5., 10.])
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Output directory already exists; choose a new destination")
    try:
        dataset = load_dataset(args.input)
        report, runs = compare(dataset, args.cost_bps, args.initial_equity)
        write_results(args.output, report, runs)
    except (ResearchError, OSError, ValueError) as exc:
        parser.exit(2, f"Research stopped: {exc}\n")
    print(f"Research comparison saved: {args.output}; no trading configuration changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
