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
from .quotes import LatestQuoteTransport, QuoteBatch, QuoteBudget, collect_quotes, inspect_quote
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
    # Validation thresholds are identical for both feeds. This legacy helper
    # has no feed selection or transport; the batch API carries provenance.
    price, cause, diagnostic = inspect_quote(quote, now, feed="iex")
    if cause is not None:
        raise _quote_error(symbol, cause, diagnostic["age_seconds"], diagnostic["bid"], diagnostic["ask"])
    return price


class MarketData:
    def __init__(self, key, secret, feed="sip", *, quote_transport=None):
        if feed not in ("sip", "iex"):
            raise SafetyError("ALPACA_DATA_FEED must be sip or iex")
        self.feed = DataFeed(feed)
        self.client = bound_client(StockHistoricalDataClient(key, secret))
        self.quote_transport = (quote_transport if quote_transport is not None
                                else LatestQuoteTransport(key, secret))

    def quote_batch(self, symbols, now, *, budget: QuoteBudget, deadline) -> QuoteBatch:
        """Use a shared active-time budget; preserve failures per symbol."""
        return collect_quotes(symbols, now, feed=self.feed.value, transport=self.quote_transport,
                              budget=budget, deadline=deadline)

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
