"""Validated historical bars and scoped fresh quotes; missing prices stay absent."""
from dataclasses import replace
from datetime import timedelta
import re

import numpy as np
import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

from .broker import bound_client
from .safety import NY, SafetyError, aware, midnight
from .strategy import compute_indicators


def validate_bars(frame, expected_dates, symbol):
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise SafetyError(f"Missing/timezone-ambiguous bars: {symbol}")
    frame = frame.copy()
    frame.index = frame.index.tz_convert(NY).date
    frame = frame.loc[frame.index.isin(expected_dates)].sort_index()
    if list(frame.index) != list(expected_dates) or not frame.index.is_unique:
        raise SafetyError(f"Missing, stale or duplicate daily sessions: {symbol}")
    cols = ["open", "high", "low", "close", "volume"]
    if not set(cols).issubset(frame.columns):
        raise SafetyError(f"Incomplete OHLCV: {symbol}")
    a = frame[cols].to_numpy(dtype=float)
    if not np.isfinite(a).all() or (a <= 0).any():
        raise SafetyError(f"Invalid OHLCV: {symbol}")
    if ((frame.high < frame[["open", "close", "low"]].max(axis=1)) |
            (frame.low > frame[["open", "close"]].min(axis=1))).any():
        raise SafetyError(f"Inconsistent OHLC: {symbol}")
    return frame


def _quote_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if np.isfinite(number) else None


def _quote_error(symbol, cause, age, bid, ask):
    # Only recognized ticker syntax, numeric fields and fixed cause codes are
    # allowed in diagnostics. Never expose quote objects, URLs or exception text.
    ticker = symbol if isinstance(symbol, str) and re.fullmatch(r"[A-Z0-9.\-]{1,16}", symbol) else "unknown_symbol"
    def number(value):
        return f"{value:.6g}" if value is not None else "unavailable"
    return SafetyError(
        f"Stale/invalid quote: {ticker} (cause={cause}; age_seconds={number(age)}; "
        f"bid={number(bid)}; ask={number(ask)})"
    )


def quote_price(quote, now, symbol):
    bid = _quote_number(getattr(quote, "bid_price", None))
    ask = _quote_number(getattr(quote, "ask_price", None))
    try:
        age = (aware(now) - aware(quote.timestamp)).total_seconds()
    except (AttributeError, TypeError, ValueError, OverflowError, SafetyError):
        raise _quote_error(symbol, "invalid_timestamp", None, bid, ask) from None
    if age < -5:
        raise _quote_error(symbol, "future_timestamp", age, bid, ask)
    if age > 90:
        raise _quote_error(symbol, "stale", age, bid, ask)
    if bid is None or ask is None:
        raise _quote_error(symbol, "invalid_price", age, bid, ask)
    if bid <= 0 or ask <= 0:
        raise _quote_error(symbol, "nonpositive_price", age, bid, ask)
    if bid > ask:
        raise _quote_error(symbol, "crossed_market", age, bid, ask)
    sizes = [_quote_number(getattr(quote, name, None)) for name in ("bid_size", "ask_size")]
    if any(size is None for size in sizes):
        raise _quote_error(symbol, "invalid_size", age, bid, ask)
    if min(sizes) <= 0:
        raise _quote_error(symbol, "empty_size", age, bid, ask)
    midpoint = bid / 2 + ask / 2
    if (ask - bid) / midpoint > 0.005:
        raise _quote_error(symbol, "excessive_spread", age, bid, ask)
    return midpoint


class MarketData:
    def __init__(self, key, secret, feed="sip"):
        if feed not in ("sip", "iex"):
            raise SafetyError("ALPACA_DATA_FEED must be sip or iex")
        self.feed = DataFeed(feed)
        self.client = bound_client(StockHistoricalDataClient(key, secret))

    def prices(self, symbols, now):
        symbols = list(symbols)
        if not symbols:
            return {}
        quotes = self.client.get_stock_latest_quote(StockLatestQuoteRequest(
            symbol_or_symbols=symbols, feed=self.feed))
        now = now() if callable(now) else now
        if set(symbols) - quotes.keys():
            raise SafetyError("Missing latest quotes")
        return {s: quote_price(quotes[s], now, s) for s in symbols}

    def load(self, symbols, window, broker, *, quote_symbols=None):
        """Validate all history, then attach only explicitly requested quotes.

        None preserves the all-symbol quote behavior; an empty iterable fetches
        history alone. Unquoted indicators retain price=None, never last close.
        """
        symbols = tuple(symbols)
        if not symbols or len(set(symbols)) != len(symbols):
            raise SafetyError("Historical symbol universe must be nonempty and unique")
        requested_quotes = symbols if quote_symbols is None else tuple(quote_symbols)
        if len(set(requested_quotes)) != len(requested_quotes) or set(requested_quotes) - set(symbols):
            raise SafetyError("Quote symbols must be a unique subset of the historical universe")
        sessions = broker.calendar(window.day-timedelta(days=550), window.day-timedelta(days=1))
        dates = sorted({s.date for s in sessions})[-250:]
        if len(dates) != 250:
            raise SafetyError("Calendar has fewer than 250 completed sessions")
        # alpaca-py automatically follows next_page_token. Do not set global
        # limit=250: that would truncate a multi-symbol request, not each symbol.
        response = self.client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=list(symbols), timeframe=TimeFrame.Day,
            start=midnight(dates[0]), end=midnight(window.day),
            adjustment=Adjustment.ALL, feed=self.feed, asof=window.day.isoformat()))
        frame = response.df
        if frame.empty or not isinstance(frame.index, pd.MultiIndex):
            raise SafetyError("Historical bars missing")
        indicators = {}
        for symbol in symbols:
            if symbol not in frame.index.get_level_values("symbol"):
                raise SafetyError(f"Historical bars missing: {symbol}")
            clean = validate_bars(frame.xs(symbol, level="symbol"), dates, symbol)
            indicators[symbol] = compute_indicators(symbol, clean)
        # Complete validation of every history precedes any required quote call.
        if requested_quotes:
            prices = self.prices(requested_quotes, lambda: broker.clock().timestamp)
            for symbol in requested_quotes:
                indicators[symbol] = replace(indicators[symbol], price=prices[symbol])
        return indicators
