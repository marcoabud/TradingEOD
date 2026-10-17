"""Validated historical bars and fresh quotes. Missing observations never get filled."""
from datetime import timedelta

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


def quote_price(quote, now, symbol):
    age = (aware(now) - aware(quote.timestamp)).total_seconds()
    bid, ask = float(quote.bid_price), float(quote.ask_price)
    if not (-5 <= age <= 90) or not np.isfinite([bid, ask]).all() or not 0 < bid <= ask:
        raise SafetyError(f"Stale/invalid quote: {symbol}")
    sizes = [float(quote.bid_size), float(quote.ask_size)]
    if not np.isfinite(sizes).all() or min(sizes) <= 0 or (ask-bid)/((ask+bid)/2) > 0.005:
        raise SafetyError(f"Empty or excessive spread: {symbol}")
    return (bid + ask) / 2


class MarketData:
    def __init__(self, key, secret, feed="sip"):
        if feed not in ("sip", "iex"):
            raise SafetyError("ALPACA_DATA_FEED must be sip or iex")
        self.feed = DataFeed(feed)
        self.client = bound_client(StockHistoricalDataClient(key, secret))

    def prices(self, symbols, now):
        symbols = list(symbols)
        quotes = self.client.get_stock_latest_quote(StockLatestQuoteRequest(
            symbol_or_symbols=symbols, feed=self.feed))
        now = now() if callable(now) else now
        if set(symbols) - quotes.keys():
            raise SafetyError("Missing latest quotes")
        return {s: quote_price(quotes[s], now, s) for s in symbols}

    def load(self, symbols, window, broker):
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
        prices = self.prices(symbols, lambda: broker.clock().timestamp)
        indicators = {}
        for symbol in symbols:
            if symbol not in frame.index.get_level_values("symbol"):
                raise SafetyError(f"Historical bars missing: {symbol}")
            clean = validate_bars(frame.xs(symbol, level="symbol"), dates, symbol)
            indicators[symbol] = compute_indicators(symbol, clean, prices[symbol])
        return indicators
