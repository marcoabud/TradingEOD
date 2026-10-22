"""Pure, causal signal calculations; this module cannot submit orders.

Daily frames must contain completed exchange sessions only.  The caller owns
calendar, recency, corporate-action adjustments, and quote validation.  At
15:45 New York time, today's final daily close does not exist: momentum, RSI,
ATR and moving averages therefore end at the preceding completed session.
The live quote is used only for swing price-vs-average/stop comparisons.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


OFFENSIVE_UNIVERSE = ("SPY", "QQQ", "SMH", "GLD")
SWING_UNIVERSE = (
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "LLY",
    "COST", "JPM", "BRK.B", "V",
)
DEFENSIVE_SYMBOL = "SGOV"
MIN_HISTORY = 250
TRADING_DAYS_PER_YEAR = 252


class StrategyDataError(ValueError):
    """A signal cannot be safely calculated from the provided observations."""


@dataclass(frozen=True)
class Indicators:
    symbol: str
    price: float | None
    close: float
    sma5: float
    sma200: float
    rsi2: float
    atr14: float
    r60: float
    r120: float
    vol60: float
    as_of: str = ""

    def __post_init__(self) -> None:
        if self.price is not None:
            require_current_price(self)
        for name in ("close", "sma5", "sma200", "rsi2", "atr14", "r60", "r120", "vol60"):
            if not math.isfinite(float(getattr(self, name))):
                raise StrategyDataError(f"{self.symbol}: non-finite {name}")
        if min(self.close, self.sma5, self.sma200) <= 0:
            raise StrategyDataError(f"{self.symbol}: prices and averages must be positive")
        if not 0 <= self.rsi2 <= 100 or self.atr14 < 0 or self.vol60 < 0:
            raise StrategyDataError(f"{self.symbol}: invalid indicator range")


@dataclass(frozen=True)
class CoreDecision:
    regime: str
    targets: dict[str, float]
    scores: dict[str, float]
    reasons: tuple[str, ...]


def require_current_price(indicator: Indicators) -> float:
    """Require an explicit current quote; a completed close is never a fallback.

    The market-data layer certifies quote freshness before populating price.
    Historical-only indicators intentionally retain price=None until then.
    """
    if indicator.price is None:
        raise StrategyDataError(f"{indicator.symbol}: current quote required")
    try:
        price = float(indicator.price)
    except (TypeError, ValueError, OverflowError):
        raise StrategyDataError(f"{indicator.symbol}: invalid current price") from None
    if isinstance(indicator.price, bool) or not math.isfinite(price) or price <= 0:
        raise StrategyDataError(f"{indicator.symbol}: current price must be finite and positive")
    return price


def _numeric_array(values: Sequence[float], name: str) -> np.ndarray:
    try:
        result = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise StrategyDataError(f"{name}: non-numeric observations") from exc
    if result.ndim != 1 or not np.isfinite(result).all():
        raise StrategyDataError(f"{name}: expected a finite one-dimensional series")
    return result


def _wilder_last(values: np.ndarray, period: int) -> float:
    """Seed with an arithmetic mean, then (previous*(n-1)+current)/n."""
    if not isinstance(period, int) or isinstance(period, bool) or period < 1:
        raise StrategyDataError("Wilder period must be a positive integer")
    if len(values) < period:
        raise StrategyDataError(f"Wilder smoothing requires at least {period} observations")
    average = float(np.mean(values[:period]))
    for value in values[period:]:
        average = (average * (period - 1) + float(value)) / period
    return average


def wilder_rsi(closes: Sequence[float], period: int = 2) -> float:
    """Wilder RSI; flat prices return 50, only gains 100, only losses 0.

    The initial mean uses `period` changes, requiring `period + 1` closes.
    Reference: Fidelity Technical Indicator Guide, Relative Strength Index.
    """
    values = _numeric_array(closes, "RSI closes")
    if (values <= 0).any():
        raise StrategyDataError("RSI closes must be positive")
    changes = np.diff(values)
    average_gain = _wilder_last(np.maximum(changes, 0.0), period)
    average_loss = _wilder_last(np.maximum(-changes, 0.0), period)
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def wilder_atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14,
) -> float:
    """Wilder ATR, seeded from the first `period` true ranges.

    The first observation has no previous close and uses high-low. Later TRs
    are max(high-low, abs(high-previous_close), abs(low-previous_close)).
    Reference: Fidelity Technical Indicator Guide, Average True Range.
    """
    high = _numeric_array(highs, "ATR highs")
    low = _numeric_array(lows, "ATR lows")
    close = _numeric_array(closes, "ATR closes")
    if not (len(high) == len(low) == len(close)) or not len(close):
        raise StrategyDataError("ATR series must have equal non-zero lengths")
    if (low <= 0).any() or (high < low).any() or (close < low).any() or (close > high).any():
        raise StrategyDataError("ATR contains invalid high/low/close observations")
    true_range = high - low
    true_range[1:] = np.maximum.reduce([
        true_range[1:], np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1]),
    ])
    return _wilder_last(true_range, period)


def compute_indicators(symbol: str, daily: pd.DataFrame, current_price: float | None = None) -> Indicators:
    """Calculate signals from at least 250 ascending, complete daily bars.

    Returns are close[-1]/close[-61 or -121]-1. Volatility is the sample
    standard deviation (ddof=1) of the last 60 simple daily returns, times
    sqrt(252). No input is mutated, interpolated, filled, or silently sorted.
    Omitted current_price remains None; historical close is never substituted.
    """
    if not isinstance(daily, pd.DataFrame) or len(daily) < MIN_HISTORY:
        raise StrategyDataError(f"{symbol}: at least {MIN_HISTORY} completed daily bars required")
    if isinstance(daily.index, pd.MultiIndex):
        raise StrategyDataError(f"{symbol}: select one symbol before computing indicators")
    if not daily.index.is_unique or not daily.index.is_monotonic_increasing:
        raise StrategyDataError(f"{symbol}: daily bars must be unique and ascending")
    if not {"close", "high", "low"}.issubset(daily.columns):
        raise StrategyDataError(f"{symbol}: missing high, low, or close columns")
    close = _numeric_array(daily["close"].to_numpy(), f"{symbol} closes")
    high = _numeric_array(daily["high"].to_numpy(), f"{symbol} highs")
    low = _numeric_array(daily["low"].to_numpy(), f"{symbol} lows")
    if (close <= 0).any():
        raise StrategyDataError(f"{symbol}: closes must be positive")
    try:
        quote = None if current_price is None else float(current_price)
    except (TypeError, ValueError, OverflowError):
        raise StrategyDataError(f"{symbol}: invalid current price") from None
    if isinstance(current_price, bool):
        raise StrategyDataError(f"{symbol}: invalid current price")
    recent = close[-61:]
    returns = recent[1:] / recent[:-1] - 1.0
    return Indicators(
        symbol=symbol,
        price=quote,
        close=float(close[-1]),
        sma5=float(np.mean(close[-5:])),
        sma200=float(np.mean(close[-200:])),
        rsi2=wilder_rsi(close, period=2),
        atr14=wilder_atr(high, low, close, period=14),
        r60=float(close[-1] / close[-61] - 1.0),
        r120=float(close[-1] / close[-121] - 1.0),
        vol60=float(np.std(returns, ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)),
        as_of=str(daily.index[-1]),
    )


def momentum_score(indicator: Indicators) -> float:
    """Zero/near-zero offensive volatility is an invalid rank, not infinity."""
    if indicator.vol60 <= 1e-12:
        raise StrategyDataError(f"{indicator.symbol}: offensive volatility is zero or too small")
    return (0.60 * indicator.r60 + 0.40 * indicator.r120) / indicator.vol60


def _require(indicators: Mapping[str, Indicators], symbols: Iterable[str]) -> None:
    missing = sorted(set(symbols) - indicators.keys())
    if missing:
        raise StrategyDataError(f"Missing indicators: {', '.join(missing)}")
    for symbol in symbols:
        if indicators[symbol].symbol != symbol:
            raise StrategyDataError(f"Indicator symbol does not match mapping key: {symbol}")


def core_targets(
    indicators: Mapping[str, Indicators],
    defensive_symbol: str = DEFENSIVE_SYMBOL,
    core_weight: float = 0.70,
) -> CoreDecision:
    """Return weekly core weights; the caller enforces the rebalance calendar.

    SPY close equal to SMA200 is conservatively risk-off. Equal best R60 and
    refuge R60 passes the user's 'not lower than' rule. Tied scores preserve
    OFFENSIVE_UNIVERSE order, making repeated executions deterministic.
    """
    if not math.isfinite(core_weight) or not 0 < core_weight <= 1:
        raise StrategyDataError("Core weight must be in (0, 1]")
    if defensive_symbol in OFFENSIVE_UNIVERSE:
        raise StrategyDataError("Defensive symbol must be distinct from offensive universe")
    _require(indicators, (*OFFENSIVE_UNIVERSE, defensive_symbol))
    scores = {symbol: momentum_score(indicators[symbol]) for symbol in OFFENSIVE_UNIVERSE}
    ranked = sorted(OFFENSIVE_UNIVERSE, key=lambda symbol: -scores[symbol])
    reasons = []
    if indicators["SPY"].close <= indicators["SPY"].sma200:
        reasons.append("SPY_AT_OR_BELOW_SMA200")
    if indicators[ranked[0]].r60 < indicators[defensive_symbol].r60:
        reasons.append("BEST_R60_BELOW_DEFENSIVE")
    if reasons:
        return CoreDecision("RISK-OFF", {defensive_symbol: core_weight}, scores, tuple(reasons))
    return CoreDecision(
        "RISK-ON", {symbol: core_weight / 2 for symbol in ranked[:2]}, scores,
        ("SPY_ABOVE_SMA200_AND_BEST_R60_NOT_BELOW_DEFENSIVE",),
    )


def swing_exit_reasons(indicator: Indicators, entry_price: float) -> tuple[str, ...]:
    """Return all triggered exits; the execution layer must still enforce PDT."""
    if not math.isfinite(float(entry_price)) or float(entry_price) <= 0:
        raise StrategyDataError(f"{indicator.symbol}: entry price must be finite and positive")
    price = require_current_price(indicator)
    reasons = []
    if price > indicator.sma5:
        reasons.append("PRICE_ABOVE_SMA5")
    if indicator.rsi2 > 70:
        reasons.append("RSI2_ABOVE_70")
    if price < float(entry_price) - 2.5 * indicator.atr14:
        reasons.append("CATASTROPHE_ATR_STOP")
    if price < indicator.sma200:
        reasons.append("PRICE_BELOW_SMA200")
    return tuple(reasons)


def swing_exits(
    indicators: Mapping[str, Indicators], entry_prices: Mapping[str, float],
) -> dict[str, tuple[str, ...]]:
    """Plan full exits for provided swing holdings; no order side effects."""
    _require(indicators, entry_prices)
    result = {}
    for symbol, entry_price in entry_prices.items():
        reasons = swing_exit_reasons(indicators[symbol], entry_price)
        if reasons:
            result[symbol] = reasons
    return result


def swing_entries(
    indicators: Mapping[str, Indicators],
    occupied_symbols: Iterable[str],
    max_slots: int = 2,
    excluded_symbols: Iterable[str] = (),
    universe: Sequence[str] = SWING_UNIVERSE,
) -> list[str]:
    """Select lowest-RSI candidates for free slots; ties follow universe order.

    Pass actual holdings AFTER confirmed exits as occupied_symbols. Protected
    or unfilled exits still occupy slots. Excluded symbols can include today's
    exits to prevent a churn/reentry loop. No entry uses RSI from an open bar.
    """
    if not isinstance(max_slots, int) or isinstance(max_slots, bool) or not 0 <= max_slots <= 2:
        raise StrategyDataError("Swing allocation supports between zero and two slots")
    if len(set(universe)) != len(universe):
        raise StrategyDataError("Swing universe contains duplicates")
    occupied = set(occupied_symbols)
    free_slots = max(0, max_slots - len(occupied))
    if free_slots == 0:
        return []
    _require(indicators, universe)
    excluded = occupied | set(excluded_symbols)
    candidates = [
        symbol for symbol in universe
        if symbol not in excluded
        and indicators[symbol].rsi2 < 10
        and require_current_price(indicators[symbol]) > indicators[symbol].sma200
    ]
    candidates.sort(key=lambda symbol: indicators[symbol].rsi2)
    return candidates[:free_slots]
