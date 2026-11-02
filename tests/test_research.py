"""Offline research accounting, causal inputs and fail-closed data validation."""
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src.research import (CORE, UNIVERSE, Dataset, Holding, Portfolio, ResearchError,
                          compare, load_dataset, metrics, prepare_signals, simulate)
from src.safety import NY
from src.strategy import Indicators, swing_exit_reasons


def fixture_dataset(count=12):
    warmup = [x.date() for x in pd.bdate_range(end="2026-10-23", periods=250)]
    future = [x.date() for x in pd.bdate_range(start="2026-10-26", periods=count)]
    days = tuple(warmup+future)
    bars = {s: pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100.,
                            "volume": 1000}, index=pd.Index(days, name="date")) for s in UNIVERSE}
    marks = {d: {s: 100. for s in UNIVERSE} for d in days}
    targets = {d: datetime.combine(d, time(15, 45), NY) for d in days}
    return Dataset({"execution_mode": "adjusted_close_proxy"}, days, bars, marks, targets,
                   frozenset(d for d in days if d.weekday() == 4))


def fixture_signals(dataset):
    result = {}
    for i, day in enumerate(dataset.sessions[250:], start=250):
        result[day] = {s: Indicators(s, dataset.marks[day][s], 110, 110, 90, 50, 4,
                                    .1 if s == "SPY" else .08 if s == "QQQ" else .01,
                                    .2 if s == "SPY" else .1, .2, dataset.sessions[i-1].isoformat())
                       for s in UNIVERSE}
    return result


def write_fixture(folder, dataset, *, window=False):
    rows = []
    for symbol, frame in dataset.bars.items():
        for day, row in frame.iterrows():
            rows.append({"date": day.isoformat(), "symbol": symbol, **row.to_dict()})
    pd.DataFrame(rows).to_csv(folder/"daily.csv", index=False)
    pd.DataFrame([{"date": d.isoformat(), "open": datetime.combine(d, time(9, 30), NY).isoformat(),
                   "close": (dataset.targets[d]+timedelta(minutes=15)).isoformat()}
                  for d in dataset.sessions]).to_csv(folder/"calendar.csv", index=False)
    names = ["daily.csv", "calendar.csv"]
    if window:
        pd.DataFrame([{"timestamp": dataset.targets[d].isoformat(), "symbol": s, "price": p}
                      for d, values in dataset.marks.items() for s, p in values.items()]
                     ).to_csv(folder/"execution.csv", index=False)
        names.append("execution.csv")
    manifest = {"schema_version": 1, "source": "Synthetic test fixture; not market evidence",
                "feed": "sip", "adjustment": "all", "price_basis": "total_return_adjusted",
                "retrieved_at": "2027-01-01T00:00:00+00:00", "symbols": list(UNIVERSE),
                "execution_mode": "window_marks" if window else "adjusted_close_proxy",
                "execution_price_basis": "total_return_adjusted",
                "files": {name: hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in names}}
    (folder/"manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def rehash(folder, manifest):
    manifest["files"] = {name: hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in manifest["files"]}
    (folder/"manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class InputTests(unittest.TestCase):
    def test_complete_adjusted_fixture_and_hashes_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_fixture(folder, fixture_dataset())
            dataset = load_dataset(folder)
            self.assertEqual(len(dataset.sessions), 262)
            self.assertEqual(set(dataset.bars), set(UNIVERSE))
            self.assertEqual(len(dataset.manifest["manifest_sha256"]), 64)

    def test_raw_or_mixed_basis_is_rejected(self):
        for adjustment, basis in (("raw", "raw"), ("split", "total_return_adjusted"), ("all", "raw")):
            with self.subTest(adjustment=adjustment, basis=basis), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                manifest = write_fixture(folder, fixture_dataset())
                manifest.update(adjustment=adjustment, price_basis=basis)
                rehash(folder, manifest)
                with self.assertRaisesRegex(ResearchError, "Corporate-action"):
                    load_dataset(folder)

    def test_modified_input_without_updated_digest_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_fixture(folder, fixture_dataset())
            with (folder/"daily.csv").open("a") as f:
                f.write("\n")
            with self.assertRaisesRegex(ResearchError, "checksum mismatch"):
                load_dataset(folder)

    def test_missing_bar_is_not_forward_filled(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            manifest = write_fixture(folder, fixture_dataset())
            frame = pd.read_csv(folder/"daily.csv").iloc[1:]
            frame.to_csv(folder/"daily.csv", index=False)
            rehash(folder, manifest)
            with self.assertRaisesRegex(ResearchError, "Missing, extra"):
                load_dataset(folder)

    def test_duplicate_bars_and_invalid_ohlc_fail(self):
        for kind in ("duplicate", "ohlc", "nan"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                manifest = write_fixture(folder, fixture_dataset())
                frame = pd.read_csv(folder/"daily.csv")
                if kind == "duplicate":
                    frame = pd.concat([frame, frame.iloc[:1]])
                elif kind == "ohlc":
                    frame.loc[0, "low"] = 500
                else:
                    frame.loc[0, "close"] = float("nan")
                frame.to_csv(folder/"daily.csv", index=False)
                rehash(folder, manifest)
                with self.assertRaises(ResearchError):
                    load_dataset(folder)

    def test_incomplete_session_and_naive_calendar_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            manifest = write_fixture(folder, fixture_dataset())
            manifest["retrieved_at"] = "2020-01-01T00:00:00+00:00"
            rehash(folder, manifest)
            with self.assertRaisesRegex(ResearchError, "not complete"):
                load_dataset(folder)
            manifest["retrieved_at"] = "2027-01-01T00:00:00+00:00"
            frame = pd.read_csv(folder/"calendar.csv")
            frame.loc[0, "open"] = "2025-01-01T09:30:00"
            frame.to_csv(folder/"calendar.csv", index=False)
            rehash(folder, manifest)
            with self.assertRaisesRegex(ResearchError, "timezone-aware"):
                load_dataset(folder)

    def test_window_marks_follow_early_close_and_dst(self):
        dataset = fixture_dataset()
        early = dataset.sessions[254]
        dataset.targets[early] = datetime.combine(early, time(12, 45), NY)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            manifest = write_fixture(folder, dataset, window=True)
            loaded = load_dataset(folder)
            self.assertEqual(loaded.targets[early].hour, 12)
            self.assertNotEqual(loaded.targets[date(2026, 10, 30)].utcoffset(),
                                loaded.targets[date(2026, 11, 2)].utcoffset())
            frame = pd.read_csv(folder/"execution.csv")
            frame.loc[0, "timestamp"] = (dataset.targets[dataset.sessions[0]] + timedelta(minutes=1)).isoformat()
            frame.to_csv(folder/"execution.csv", index=False)
            rehash(folder, manifest)
            with self.assertRaisesRegex(ResearchError, "off-window"):
                load_dataset(folder)

    def test_missing_window_mark_has_no_close_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            manifest = write_fixture(folder, fixture_dataset(), window=True)
            frame = pd.read_csv(folder/"execution.csv").iloc[1:]
            frame.to_csv(folder/"execution.csv", index=False)
            rehash(folder, manifest)
            with self.assertRaisesRegex(ResearchError, "no close fallback"):
                load_dataset(folder)


class CausalityAndAccountingTests(unittest.TestCase):
    def test_today_and_future_bars_do_not_change_prior_bar_indicators(self):
        dataset = fixture_dataset(3)
        first = dataset.sessions[250]
        before = prepare_signals(dataset)[first]["SPY"]
        for s in UNIVERSE:
            dataset.bars[s].loc[first:, ["open", "high", "low", "close"]] *= 10
        after = prepare_signals(dataset)[first]["SPY"]
        self.assertEqual(asdict(before), asdict(after))
        self.assertEqual(before.as_of, dataset.sessions[249].isoformat())

    def test_same_day_buy_cannot_be_sold(self):
        day = date(2026, 10, 30)
        portfolio = Portfolio(D(4000))
        prices = {"SPY": 100}
        portfolio.buy(day, "SPY", D(1400), prices, D(".001"), "test")
        with self.assertRaisesRegex(ResearchError, "ANTI_PDT"):
            portfolio.sell(day, "SPY", portfolio.positions["SPY"].qty, prices, D(".001"), "test")

    def test_same_day_sale_cannot_be_reentered(self):
        day = date(2026, 10, 30)
        portfolio = Portfolio(D(0), {"SPY": Holding(D(10), D(100), day-timedelta(days=1))})
        portfolio.sell(day, "SPY", D(10), {"SPY": 100}, D(0), "exit")
        with self.assertRaisesRegex(ResearchError, "reentry"):
            portfolio.buy(day, "SPY", 900, {"SPY": 100}, D(0), "test")

    def test_fees_are_charged_and_fractional_buy_never_borrows(self):
        portfolio = Portfolio(D("10.05"))
        portfolio.buy(date(2026, 10, 30), "SPY", D(1000), {"SPY": 753.42}, D(".001"), "test")
        self.assertGreaterEqual(portfolio.cash, D(1))
        self.assertGreater(portfolio.cost, 0)
        self.assertLess(portfolio.positions["SPY"].qty, 1)
        self.assertEqual(portfolio.positions["SPY"].qty.as_tuple().exponent, -9)
        self.assertAlmostEqual(float(portfolio.equity({"SPY": 753.42})+portfolio.cost), 10.05)

    def test_friction_changes_cash_but_not_average_fill_price_or_atr_stop(self):
        # 97.55 is above the real 100 - 2.5*1 stop, but below a fee-inflated
        # 100.10 - 2.5*1 stop. Cost scenarios must not create a new sell signal.
        day = date(2026, 10, 30)
        indicator = Indicators("AAPL", 97.55, 100, 101, 90, 50, 1,
                               .1, .1, .2, "2026-10-29")
        free, costly = Portfolio(D(4000)), Portfolio(D(4000))
        for portfolio, rate in ((free, D(0)), (costly, D(".001"))):
            portfolio.buy(day, "AAPL", 600, {"AAPL": 100}, rate, "test")
            self.assertEqual(portfolio.positions["AAPL"].entry, D(100))
            self.assertEqual(swing_exit_reasons(indicator, float(portfolio.positions["AAPL"].entry)), ())
        self.assertLess(costly.cash, free.cash)
        self.assertGreater(costly.cost, free.cost)

    def test_daily_defensive_exits_before_friday_but_does_not_reenter_before_friday(self):
        dataset = fixture_dataset(12)
        signals = fixture_signals(dataset)
        tuesday = date(2026, 11, 3)
        signals[tuesday]["SPY"] = replace(signals[tuesday]["SPY"], close=80)
        base = simulate(dataset, signals, "weekly", 0)
        daily = simulate(dataset, signals, "daily_defensive", 0)
        self.assertFalse(any(t["date"] == tuesday.isoformat() for t in base["trades"]))
        exits = [t for t in daily["trades"] if t["date"] == tuesday.isoformat()]
        self.assertEqual([t["side"] for t in exits], ["sell", "sell", "buy"])
        self.assertEqual(exits[-1]["symbol"], "SGOV")
        self.assertFalse(any(t["side"] == "buy" and t["symbol"] in ("SPY", "QQQ")
                             and "2026-11-04" <= t["date"] <= "2026-11-05" for t in daily["trades"]))

    def test_fixed_trailing_scenarios_diverge_at_their_known_thresholds(self):
        dataset = fixture_dataset(8)
        for s in ("SPY", "QQQ"):
            dataset.marks[date(2026, 11, 2)][s] = 105
            dataset.marks[date(2026, 11, 3)][s] = 99
        signals = fixture_signals(dataset)
        five = simulate(dataset, signals, "trailing_core_5pct", 0)
        ten = simulate(dataset, signals, "trailing_core_10pct", 0)
        self.assertTrue(any(t["date"] == "2026-11-03" and t["side"] == "sell" for t in five["trades"]))
        self.assertFalse(any(t["date"] == "2026-11-03" and t["side"] == "sell" for t in ten["trades"]))

    def test_cash_and_two_swing_slots_survive_cost_scenarios(self):
        dataset = fixture_dataset()
        signals = fixture_signals(dataset)
        for values in signals.values():
            for symbol in ("AAPL", "MSFT", "NVDA"):
                values[symbol] = replace(values[symbol], rsi2=1)
        for cost in (0, 1, 5, 10):
            with self.subTest(cost=cost):
                run = simulate(dataset, signals, "weekly", cost)
                for row in run["rows"]:
                    self.assertGreaterEqual(row["cash"], 0)
                    self.assertLessEqual(len(set(row["positions"])-set(CORE)), 2)
                for day in dataset.sessions:
                    trades = [t for t in run["trades"] if t["date"] == day.isoformat()]
                    sides = [t["side"] for t in trades]
                    if "buy" in sides:
                        self.assertNotIn("sell", sides[sides.index("buy"):])

    def test_flat_market_higher_costs_reduce_results(self):
        dataset = fixture_dataset()
        signals = fixture_signals(dataset)
        free = simulate(dataset, signals, "weekly", 0)
        costly = simulate(dataset, signals, "weekly", 10)
        self.assertEqual(free["rows"][-1]["equity"], 4000)
        self.assertLess(costly["rows"][-1]["equity"], 4000)
        self.assertGreater(costly["rows"][-1]["cost_cumulative"], 0)

    def test_oos_returns_use_segment_start_equity_not_full_history(self):
        dataset = fixture_dataset(4)
        days = dataset.sessions[250:]
        def run(values):
            return {"initial_equity": 4000, "trades": [], "defensive_exits": [],
                    "rows": [{"date": d.isoformat(), "equity": value,
                              "cost_cumulative": 0, "turnover_cumulative": 0,
                              "offensive_value": 0} for d, value in zip(days, values)]}
        variant, baseline = run([4100, 4000, 4200, 4100]), run([4100, 4000, 4000, 3900])
        result = metrics(variant, baseline, dataset, 2)
        self.assertEqual(result["start_equity"], 4000)
        self.assertAlmostEqual(result["net_return_pct"], 2.5)
        self.assertAlmostEqual(result["net_return_difference_vs_weekly_pp"], 5.)
        self.assertAlmostEqual(result["maximum_drawdown_pct"], (1-4100/4200)*100)

    def test_four_snapshots_cannot_create_oos_comparison(self):
        with self.assertRaisesRegex(ResearchError, "60 development"):
            compare(fixture_dataset(4))


if __name__ == "__main__":
    unittest.main()
