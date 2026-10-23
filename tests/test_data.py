"""Calendar-aligned market data tests; every provider call is replaced by a fake."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from alpaca.data.enums import Adjustment, DataFeed
import numpy as np
import pandas as pd

from src.data import MarketData, quote_price, validate_bars
from src.quotes import QuoteBudget
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
        self.assertEqual(quote_price(self.quote(timestamp=self.now+timedelta(seconds=5)), self.now, "SPY"), 100)
        self.assertEqual(quote_price(self.quote(bid_price=99.75, ask_price=100.25), self.now, "SPY"), 100)

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

    def test_diagnostics_report_allowed_numeric_context_and_specific_cause(self):
        cases = (
            ({"timestamp": self.now-timedelta(seconds=91)}, "stale"),
            ({"timestamp": self.now+timedelta(seconds=6)}, "future_timestamp"),
            ({"bid_price": 101}, "crossed_market"),
            ({"bid_size": 0}, "empty_size"),
            ({"ask_price": 101}, "excessive_spread"),
        )
        for fields, cause in cases:
            with self.subTest(cause=cause), self.assertRaises(SafetyError) as raised:
                quote_price(self.quote(**fields), self.now, "SGOV")
            message = str(raised.exception)
            self.assertIn("SGOV", message)
            self.assertIn(f"cause={cause};", message)
            self.assertIn("age_seconds=", message)
            self.assertIn("bid=", message)
            self.assertIn("ask=", message)
        with self.assertRaises(SafetyError) as raised:
            quote_price(self.quote(timestamp=self.now-timedelta(seconds=91)), self.now, "SGOV")
        self.assertIn("age_seconds=91; bid=99.9; ask=100.1", str(raised.exception))

    def test_malformed_quote_diagnostics_never_echo_raw_values_or_objects(self):
        secret = "private://token-secret-do-not-print"
        for fields in ({"timestamp": secret}, {"bid_price": secret}, {"ask_size": secret}):
            with self.subTest(fields=fields), self.assertRaises(SafetyError) as raised:
                quote_price(self.quote(**fields), self.now, "SGOV")
            self.assertNotIn(secret, str(raised.exception))
            self.assertNotIn("namespace", str(raised.exception))

    def test_empty_quote_scope_has_no_provider_or_clock_call(self):
        data = object.__new__(MarketData)
        data.feed = DataFeed.SIP
        data.client = Mock()
        clock = Mock(side_effect=AssertionError("Clock is unnecessary without quotes"))
        self.assertEqual(data.prices((), clock), {})
        data.client.get_stock_latest_quote.assert_not_called()
        clock.assert_not_called()

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
        self.assertEqual(result["SPY"].price, 130)
        self.assertEqual(result["QQQ"].price, 130)
        self.assertEqual(self.data.prices.call_args.args[0], ("SPY", "QQQ"))
        request = self.data.client.get_stock_bars.call_args.args[0]
        self.assertEqual(request.adjustment, Adjustment.ALL)
        self.assertIsNone(request.limit)
        self.assertEqual(request.asof, "2026-10-16")
        self.assertEqual(request.end.astimezone(NY).date(), self.day)

    def test_total_bar_count_cannot_hide_short_history_for_one_symbol(self):
        self.response({"SPY": bars(self.dates), "QQQ": bars(self.dates[:-1])})
        with self.assertRaises(SafetyError):
            self.data.load(("SPY", "QQQ"), self.window, self.broker)
        self.data.prices.assert_not_called()  # Every history is checked first.

    def test_empty_quote_scope_preserves_all_history_with_explicit_null_prices(self):
        self.response({"SPY": bars(self.dates), "SGOV": bars(self.dates)})
        result = self.data.load(("SPY", "SGOV"), self.window, self.broker, quote_symbols=())
        self.data.prices.assert_not_called()
        self.broker.clock.assert_not_called()
        self.assertEqual(set(result), {"SPY", "SGOV"})
        self.assertTrue(all(value.price is None for value in result.values()))
        self.assertEqual(result["SGOV"].close, 125.9)

    def test_quote_subset_does_not_request_sgov_or_fill_its_price_from_close(self):
        self.response({"SPY": bars(self.dates), "SGOV": bars(self.dates)})
        result = self.data.load(("SPY", "SGOV"), self.window, self.broker, quote_symbols=("SPY",))
        self.assertEqual(self.data.prices.call_args.args[0], ("SPY",))
        self.assertEqual(result["SPY"].price, 130)
        self.assertIsNone(result["SGOV"].price)
        self.assertEqual(result["SGOV"].close, 125.9)

    def test_unquoted_history_still_must_pass_full_validation(self):
        self.response({"SPY": bars(self.dates), "SGOV": bars(self.dates[:-1])})
        with self.assertRaises(SafetyError):
            self.data.load(("SPY", "SGOV"), self.window, self.broker, quote_symbols=("SPY",))
        self.data.prices.assert_not_called()

    def test_quote_scope_must_be_unique_subset_of_historical_symbols(self):
        for scope in (("SGOV",), ("SPY", "SPY")):
            with self.subTest(scope=scope), self.assertRaises(SafetyError):
                self.data.load(("SPY", "QQQ"), self.window, self.broker, quote_symbols=scope)
        self.data.client.get_stock_bars.assert_not_called()
        self.broker.calendar.assert_not_called()

    def test_required_quote_failure_is_not_suppressed(self):
        self.response({"SPY": bars(self.dates), "SGOV": bars(self.dates)})
        self.data.prices.side_effect = SafetyError("Stale/invalid quote: SGOV")
        with self.assertRaisesRegex(SafetyError, "SGOV"):
            self.data.load(("SPY", "SGOV"), self.window, self.broker, quote_symbols=("SGOV",))
        self.assertEqual(self.data.prices.call_args.args[0], ("SGOV",))

    def test_insufficient_calendar_or_missing_symbol_fails(self):
        self.broker.calendar.return_value = [NS(date=d) for d in self.dates[-249:]]
        with self.assertRaises(SafetyError):
            self.data.load(("SPY",), self.window, self.broker)
        self.data.client.get_stock_bars.assert_not_called()
        self.broker.calendar.return_value = [NS(date=d) for d in self.dates]
        self.response({"SPY": bars(self.dates)})
        with self.assertRaises(SafetyError):
            self.data.load(("SPY", "QQQ"), self.window, self.broker)


class QuoteBatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 22, 15, 45, tzinfo=NY)
        self.data = object.__new__(MarketData)
        self.data.feed = DataFeed.IEX
        self.data.client = Mock()
        self.data.quote_transport = Mock(return_value={
            "SPY": {"t": self.now.isoformat(), "bp": 99.9, "ap": 100.1, "bs": 10, "as": 20},
            "SGOV": {"t": (self.now-timedelta(seconds=173)).isoformat(),
                     "bp": 99.9, "ap": 100.1, "bs": 10, "as": 20},
        })

    def test_new_batch_api_preserves_symbol_failures_without_sdk_latest_call(self):
        batch = self.data.quote_batch(("SPY", "SGOV"), lambda: self.now,
                                     budget=QuoteBudget(max_attempts=1),
                                     deadline=self.now+timedelta(minutes=10))
        self.assertEqual(batch.prices, {"SPY": 100})
        self.assertEqual(batch.failures, {"SGOV": "stale"})
        self.assertEqual(self.data.quote_transport.call_args.kwargs["feed"], "iex")
        self.data.client.get_stock_latest_quote.assert_not_called()
        self.assertEqual(quote_price(batch.quotes["SPY"], self.now, "SPY"), 100)
        with self.assertRaisesRegex(SafetyError, "stale"):
            quote_price(batch.quotes["SPY"], self.now+timedelta(seconds=91), "SPY")

    def test_exhausted_budget_does_not_fall_back_to_sdk_or_previous_close(self):
        clock = Mock(side_effect=AssertionError("No Clock call when budget is exhausted"))
        batch = self.data.quote_batch(("SPY",), clock, budget=QuoteBudget(seconds=0),
                                     deadline=self.now+timedelta(minutes=10))
        self.assertEqual(batch.prices, {})
        self.assertEqual(batch.failures, {"SPY": "budget_exhausted"})
        self.assertEqual(batch.quotes, {})
        self.data.quote_transport.assert_not_called()
        self.data.client.get_stock_latest_quote.assert_not_called()
        clock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
