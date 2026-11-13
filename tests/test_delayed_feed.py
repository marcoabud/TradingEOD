"""Delayed consolidated quote feed and the intraday research references."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

import pandas as pd

from src.bot import Config
from src.data import MarketData, quote_price
from src.quotes import QuoteSnapshot, inspect_quote, max_quote_age
from src.research import ResearchError, prepare_signals, with_intraday
from src.research_data import SYMBOLS, export_dataset, export_intraday, intraday_marks
from src.safety import NY, SafetyError

NOW = datetime(2026, 11, 13, 20, 45, tzinfo=timezone.utc)


def quote(age, bid=100.0, ask=100.1):
    return QuoteSnapshot(NOW-timedelta(seconds=age), bid, ask, 5, 5)


class DelayedFeedTests(unittest.TestCase):
    def test_delayed_quote_is_valid_only_around_its_fixed_delay(self):
        self.assertEqual(max_quote_age("delayed_sip"), 990)
        for age, cause in ((900, None), (930, None), (989, None), (991, "stale"),
                           (845, None), (800, "delay_not_honored"), (5, "delay_not_honored")):
            price, got, diag = inspect_quote(quote(age), NOW, feed="delayed_sip")
            self.assertEqual(got, cause, age)
            self.assertEqual(diag["feed"], "delayed_sip")
        self.assertAlmostEqual(inspect_quote(quote(900), NOW, feed="delayed_sip")[0], 100.05)

    def test_live_feeds_keep_the_ninety_second_rule(self):
        for feed in ("iex", "sip"):
            self.assertIsNone(inspect_quote(quote(60), NOW, feed=feed)[1])
            self.assertEqual(inspect_quote(quote(900), NOW, feed=feed)[1], "stale")

    def test_spread_and_size_rules_are_unchanged_for_delayed_quotes(self):
        self.assertEqual(inspect_quote(quote(900, 100, 101), NOW, feed="delayed_sip")[1], "excessive_spread")
        empty = QuoteSnapshot(NOW-timedelta(seconds=900), 100, 100.1, 0, 5)
        self.assertEqual(inspect_quote(empty, NOW, feed="delayed_sip")[1], "empty_size")

    def test_quote_price_carries_the_feed(self):
        self.assertAlmostEqual(quote_price(quote(900), NOW, "AAPL", "delayed_sip"), 100.05)
        with self.assertRaises(SafetyError):
            quote_price(quote(900), NOW, "AAPL")

    def test_market_data_uses_sip_history_with_delayed_quotes(self):
        data = MarketData("k", "s", "delayed_sip", quote_transport=Mock())
        self.assertEqual((data.feed.value, data.history_feed.value), ("delayed_sip", "sip"))
        env = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ALPACA_DATA_FEED": "delayed_sip"}
        self.assertEqual(Config.from_env(env).feed, "delayed_sip")
        with self.assertRaises(SafetyError):
            MarketData("k", "s", "otc", quote_transport=Mock())


class IntradayReferenceTests(unittest.TestCase):
    DAY = date(2026, 10, 28)

    def bars(self, times):
        index = pd.DatetimeIndex([datetime.combine(self.DAY, datetime.strptime(t, "%H:%M").time(), NY)
                                  for t in times])
        return pd.DataFrame({"open": [float(i+10) for i in range(len(times))],
                             "close": [float(i+10.5) for i in range(len(times))]}, index=index)

    def marks(self, times):
        opens = datetime.combine(self.DAY, datetime.strptime("09:30", "%H:%M").time(), NY)
        closes = datetime.combine(self.DAY, datetime.strptime("16:00", "%H:%M").time(), NY)
        return intraday_marks(self.bars(times) if times else None, self.DAY, opens, closes, 99.0)

    def test_exact_bars_give_their_opens(self):
        self.assertEqual(self.marks(["15:15", "15:30", "15:45"]), ([11.0, 12.0], ["exact", "exact"]))

    def test_missing_bar_uses_latest_earlier_close_and_then_daily_close(self):
        self.assertEqual(self.marks(["15:15", "15:30"]), ([11.0, 11.5], ["exact", "earlier_bar"]))
        self.assertEqual(self.marks(["08:00", "16:15"]), ([99.0, 99.0], ["daily_close", "daily_close"]))
        self.assertEqual(self.marks(None), ([99.0, 99.0], ["daily_close", "daily_close"]))

    def test_export_and_research_round_trip(self):
        days = list(pd.bdate_range(end="2026-10-29", periods=320).date)
        calendar = [{"date": d.isoformat(), "open": "09:30", "close": "16:00"} for d in days]
        daily_index = pd.MultiIndex.from_tuples(
            [(s, pd.Timestamp(d, tz="America/New_York")) for s in SYMBOLS for d in days],
            names=["symbol", "timestamp"])
        daily = pd.DataFrame(dict(open=100.0, high=102.0, low=99.0, close=101.0, volume=100.0), index=daily_index)

        def intraday_frame(request):
            start, end = request.start.astimezone(NY).date(), request.end.astimezone(NY).date()
            rows = [(s, pd.Timestamp(datetime.combine(d, datetime.strptime(t, "%H:%M").time()), tz="America/New_York"))
                    for s in SYMBOLS for d in days if start <= d <= end for t in ("15:30", "15:45")]
            frame = pd.DataFrame(dict(open=[100.5 if ts.minute == 30 else 100.8 for _, ts in rows],
                                      close=100.9), index=pd.MultiIndex.from_tuples(rows, names=["symbol", "timestamp"]))
            return NS(df=frame)

        client = Mock()
        client.get_stock_bars.side_effect = lambda request: (
            NS(df=daily[[request.start.astimezone(NY).date() <= ts.date() <= request.end.astimezone(NY).date()
                         for _, ts in daily.index]])
            if request.timeframe.unit_value.value == "Day" else intraday_frame(request))
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/"dataset"
            export_dataset(days[0], days[-1], output, client=client, calendar_rows=calendar,
                           retrieved_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
            meta = export_intraday(output, client=client, calendar_rows=calendar, end=days[-1], pause=lambda _: None)
            self.assertEqual(meta["reference_sources"], {"exact": 2*len(days)*len(SYMBOLS), "earlier_bar": 0, "daily_close": 0})
            from src.research import load_dataset
            base = load_dataset(output)
            delayed, live = with_intraday(base, output, "delayed"), with_intraday(base, output, "live")
            day = days[-1]
            self.assertEqual(delayed.marks[day]["AAPL"], 100.8)
            self.assertEqual(delayed.signal_marks[day]["AAPL"], 100.5)
            self.assertEqual(live.signal_marks[day]["AAPL"], 100.8)
            self.assertEqual(prepare_signals(delayed)[day]["AAPL"].price, 100.5)
            self.assertEqual(prepare_signals(live)[day]["AAPL"].price, 100.8)
            self.assertIn("delayed", delayed.manifest["execution_mode"])
            (output/"intraday.csv").write_text("tampered", encoding="utf-8")
            with self.assertRaises(ResearchError):
                with_intraday(base, output, "delayed")


if __name__ == "__main__":
    unittest.main()
