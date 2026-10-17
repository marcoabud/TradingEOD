"""Calendar-aligned market data tests; every provider call is replaced by a fake."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from alpaca.data.enums import Adjustment, DataFeed
import numpy as np
import pandas as pd

from src.data import MarketData, quote_price, validate_bars
from src.safety import NY, SafetyError, Window


def bars(dates):
    close = 100 + np.arange(len(dates), dtype=float) / 10
    return pd.DataFrame({
        "open": close - .1, "high": close + 1, "low": close - 1,
        "close": close, "volume": np.full(len(dates), 1000),
    }, index=pd.DatetimeIndex(dates).tz_localize(NY))


class BarValidationTests(unittest.TestCase):
    def setUp(self):
        self.dates = list(pd.bdate_range(end="2026-10-15", periods=250).date)
        self.frame = bars(self.dates)

    def test_exact_calendar_sessions_sorted_and_current_bar_omitted(self):
        today = bars([date(2026, 10, 16)])
        frame = pd.concat([self.frame, today]).iloc[::-1]
        result = validate_bars(frame, self.dates, "SPY")
        self.assertEqual(len(result), 250)
        self.assertEqual(list(result.index), self.dates)
        self.assertNotIn(date(2026, 10, 16), result.index)
        self.assertEqual(len(frame), 251)  # Input remains unchanged.

    def test_missing_recent_stale_and_duplicate_sessions_fail(self):
        stale = pd.concat([bars([self.dates[0] - timedelta(days=1)]), self.frame.iloc[:-1]])
        duplicate = pd.concat([self.frame, self.frame.iloc[[-1]]])
        for frame in (self.frame.iloc[:-1], self.frame.drop(self.frame.index[100]), stale, duplicate):
            with self.subTest(rows=len(frame)), self.assertRaises(SafetyError):
                validate_bars(frame, self.dates, "SPY")

    def test_timezone_is_required_and_utc_daily_stamp_maps_to_new_york(self):
        self.assertEqual(len(validate_bars(self.frame.tz_convert("UTC"), self.dates, "SPY")), 250)
        with self.assertRaises(SafetyError):
            validate_bars(self.frame.tz_localize(None), self.dates, "SPY")

    def test_bad_ohlcv_and_missing_columns_fail(self):
        frames = [self.frame.drop(columns="volume")]
        for column, value in (("close", np.nan), ("volume", 0), ("low", -1), ("high", 1), ("open", np.inf)):
            bad = self.frame.copy()
            bad.iloc[-1, bad.columns.get_loc(column)] = value
            frames.append(bad)
        for frame in frames:
            with self.subTest(columns=list(frame.columns)), self.assertRaises(SafetyError):
                validate_bars(frame, self.dates, "SPY")


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 16, 15, 45, tzinfo=NY)

    def quote(self, **kwargs):
        fields = dict(timestamp=self.now, bid_price=99.9, ask_price=100.1, bid_size=10, ask_size=20)
        fields.update(kwargs)
        return NS(**fields)

    def test_fresh_midpoint(self):
        self.assertEqual(quote_price(self.quote(), self.now, "SPY"), 100)
        self.assertEqual(quote_price(self.quote(timestamp=self.now-timedelta(seconds=90)), self.now, "SPY"), 100)

    def test_stale_future_crossed_empty_and_wide_quotes_fail(self):
        failures = [
            {"timestamp": self.now-timedelta(seconds=91)},
            {"timestamp": self.now+timedelta(seconds=6)},
            {"timestamp": self.now.replace(tzinfo=None)},
            {"bid_price": 101}, {"bid_price": 0}, {"ask_price": np.inf},
            {"bid_price": np.nan}, {"bid_size": 0}, {"ask_size": -1},
            {"bid_price": 99, "ask_price": 101},
        ]
        for values in failures:
            with self.subTest(values=values), self.assertRaises(SafetyError):
                quote_price(self.quote(**values), self.now, "SPY")

    def test_non_finite_quote_sizes_fail(self):
        for values in ({"bid_size": np.nan}, {"ask_size": np.inf}):
            with self.subTest(values=values), self.assertRaises(SafetyError):
                quote_price(self.quote(**values), self.now, "SPY")

    def test_missing_latest_quote_fails_for_whole_batch(self):
        data = object.__new__(MarketData)
        data.feed = DataFeed.SIP
        data.client = Mock()
        data.client.get_stock_latest_quote.return_value = {"SPY": self.quote()}
        with self.assertRaises(SafetyError):
            data.prices(["SPY", "QQQ"], self.now)


class DataLoadTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 10, 16)
        self.dates = list(pd.bdate_range(end="2026-10-15", periods=260).date)
        self.window = Window(self.day, datetime(2026, 10, 16, 9, 30, tzinfo=NY),
                             datetime(2026, 10, 16, 16, tzinfo=NY), True)
        self.data = object.__new__(MarketData)
        self.data.feed = DataFeed.SIP
        self.data.client = Mock()
        self.data.prices = Mock(return_value={"SPY": 130, "QQQ": 130})
        self.broker = Mock()
        self.broker.calendar.return_value = [NS(date=d) for d in self.dates]
        self.broker.clock.return_value = NS(timestamp=self.window.target)

    def response(self, frames):
        self.data.client.get_stock_bars.return_value = NS(df=pd.concat(frames, names=["symbol", "timestamp"]))

    def test_per_symbol_250_sessions_adjusted_data_and_no_global_limit(self):
        frame = bars(self.dates + [self.day])
        self.response({"SPY": frame, "QQQ": frame})
        result = self.data.load(("SPY", "QQQ"), self.window, self.broker)
        self.assertEqual(set(result), {"SPY", "QQQ"})
        self.assertEqual(result["SPY"].as_of, "2026-10-15")
        self.assertEqual(result["SPY"].close, 125.9)
        request = self.data.client.get_stock_bars.call_args.args[0]
        self.assertEqual(request.adjustment, Adjustment.ALL)
        self.assertIsNone(request.limit)
        self.assertEqual(request.asof, "2026-10-16")
        self.assertEqual(request.end.astimezone(NY).date(), self.day)

    def test_total_bar_count_cannot_hide_short_history_for_one_symbol(self):
        self.response({"SPY": bars(self.dates), "QQQ": bars(self.dates[:-1])})
        with self.assertRaises(SafetyError):
            self.data.load(("SPY", "QQQ"), self.window, self.broker)

    def test_insufficient_calendar_or_missing_symbol_fails(self):
        self.broker.calendar.return_value = [NS(date=d) for d in self.dates[-249:]]
        with self.assertRaises(SafetyError):
            self.data.load(("SPY",), self.window, self.broker)
        self.data.client.get_stock_bars.assert_not_called()
        self.broker.calendar.return_value = [NS(date=d) for d in self.dates]
        self.response({"SPY": bars(self.dates)})
        with self.assertRaises(SafetyError):
            self.data.load(("SPY", "QQQ"), self.window, self.broker)


if __name__ == "__main__":
    unittest.main()
