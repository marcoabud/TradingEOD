"""Deterministic examples for indicators, causal timing, and strategy boundaries."""

from dataclasses import replace
import math
import unittest

import numpy as np
import pandas as pd

from src.strategy import (
    OFFENSIVE_UNIVERSE, SWING_UNIVERSE, Indicators, StrategyDataError,
    compute_indicators, core_targets, momentum_score, require_current_price, swing_entries,
    swing_exit_reasons, swing_exits, wilder_atr, wilder_rsi,
)


def indicator(symbol="AAPL", **changes):
    base = Indicators(
        symbol=symbol, price=110, close=110, sma5=115, sma200=100,
        rsi2=5, atr14=4, r60=0.1, r120=0.2, vol60=0.2,
    )
    return replace(base, **changes)


def core_fixture():
    return {symbol: indicator(symbol) for symbol in (*OFFENSIVE_UNIVERSE, "SGOV")}


class WilderIndicatorsTests(unittest.TestCase):
    def test_rsi_manual_seed_and_recurrence(self):
        # Changes +2,-1,+3,-2. First avg gain=1, loss=.5; then 2,.25;
        # then 1,1.125. RS=8/9, RSI=800/17, not a rolling two-day RSI.
        self.assertAlmostEqual(wilder_rsi([10, 12, 11, 14, 12], 2), 800 / 17)

    def test_rsi_flat_only_up_and_only_down(self):
        self.assertEqual(wilder_rsi([10, 10, 10, 10]), 50)
        self.assertEqual(wilder_rsi([10, 11, 12, 13]), 100)
        self.assertEqual(wilder_rsi([13, 12, 11, 10]), 0)

    def test_atr_manual_gap_and_smoothing(self):
        # TRs: 2,4,5,2. Seed over three =11/3; next =(22/3+2)/3=28/9.
        self.assertAlmostEqual(wilder_atr(
            [11, 14, 13, 10], [9, 12, 8, 8], [10, 13, 9, 9], 3,
        ), 28 / 9)

    def test_wilder_rejects_missing_and_invalid_observations(self):
        for closes in ([10, 11], [10, np.nan, 11], [10, 0, 11]):
            with self.subTest(closes=closes), self.assertRaises(StrategyDataError):
                wilder_rsi(closes)
        with self.assertRaises(StrategyDataError):
            wilder_atr([11, 12], [9], [10, 11], 2)
        with self.assertRaises(StrategyDataError):
            wilder_atr([11, 12], [9, 10], [12, 11], 2)


class HistoricalCalculationTests(unittest.TestCase):
    def setUp(self):
        closes = np.arange(1, 251, dtype=float) + 100
        self.frame = pd.DataFrame(
            {"high": closes + 2, "low": closes - 2, "close": closes},
            index=pd.bdate_range("2024-01-02", periods=250),
        )

    def test_exact_lookbacks_and_sample_annualized_volatility(self):
        result = compute_indicators("SPY", self.frame, 351)
        self.assertAlmostEqual(result.r60, 350 / 290 - 1)
        self.assertAlmostEqual(result.r120, 350 / 230 - 1)
        self.assertEqual(result.sma5, 348)
        self.assertEqual(result.sma200, (151 + 350) / 2)
        returns = np.arange(291, 351) / np.arange(290, 350) - 1
        expected_var = sum((r - sum(returns) / 60) ** 2 for r in returns) / 59
        self.assertAlmostEqual(result.vol60, math.sqrt(expected_var * 252))

    def test_quote_does_not_change_completed_bar_indicators(self):
        original = self.frame.copy(deep=True)
        low = compute_indicators("SPY", self.frame, 100)
        high = compute_indicators("SPY", self.frame, 500)
        self.assertEqual(replace(low, price=500), high)
        pd.testing.assert_frame_equal(self.frame, original)

    def test_historical_only_indicators_keep_price_absent(self):
        historical = compute_indicators("SPY", self.frame)
        quoted = compute_indicators("SPY", self.frame, 351)
        self.assertIsNone(historical.price)
        self.assertEqual(historical.close, 350)
        self.assertEqual(replace(historical, price=351), quoted)
        with self.assertRaisesRegex(StrategyDataError, "current quote required"):
            require_current_price(historical)

    def test_optional_price_rejects_invalid_values_instead_of_substituting_close(self):
        for price in (float("nan"), float("inf"), 0, -1, "invalid", True):
            with self.subTest(price=price):
                with self.assertRaises(StrategyDataError):
                    compute_indicators("SPY", self.frame, price)
                with self.assertRaises(StrategyDataError):
                    indicator("SPY", price=price)
        self.assertEqual(require_current_price(indicator(price=110.5)), 110.5)

    def test_bad_history_fails_closed(self):
        missing = self.frame.copy()
        missing.iloc[-1, missing.columns.get_loc("close")] = np.nan
        duplicate = self.frame.copy()
        duplicate.index = [self.frame.index[0]] * len(duplicate)
        for frame in (self.frame.iloc[:-1], self.frame.iloc[::-1], missing, duplicate):
            with self.subTest(frame_length=len(frame)), self.assertRaises(StrategyDataError):
                compute_indicators("SPY", frame, 350)
        with self.assertRaises(StrategyDataError):
            compute_indicators("SPY", self.frame, float("inf"))


class CoreTests(unittest.TestCase):
    def test_core_decision_needs_no_live_quotes_in_either_regime(self):
        quoted = core_fixture()
        historical = {symbol: replace(value, price=None) for symbol, value in quoted.items()}
        self.assertEqual(core_targets(historical), core_targets(quoted))
        self.assertEqual(core_targets(historical).regime, "RISK-ON")
        historical["SPY"] = replace(historical["SPY"], close=99)
        decision = core_targets(historical)
        self.assertEqual(decision.regime, "RISK-OFF")
        self.assertEqual(decision.targets, {"SGOV": .70})
        self.assertIsNone(historical["SGOV"].price)

    def test_score_and_deterministic_equal_score_ranking(self):
        values = core_fixture()
        self.assertAlmostEqual(momentum_score(values["SPY"]), .7)
        decision = core_targets(values)
        self.assertEqual(decision.regime, "RISK-ON")
        self.assertEqual(decision.targets, {"SPY": .35, "QQQ": .35})
        self.assertAlmostEqual(sum(decision.targets.values()), .70)

    def test_spy_sma_equality_is_risk_off_even_if_live_quote_has_rallied(self):
        values = core_fixture()
        values["SPY"] = indicator("SPY", close=100, price=150)
        decision = core_targets(values)
        self.assertEqual(decision.regime, "RISK-OFF")
        self.assertEqual(decision.targets, {"SGOV": .70})
        self.assertIn("SPY_AT_OR_BELOW_SMA200", decision.reasons)

    def test_only_best_score_r60_compares_to_refuge(self):
        values = core_fixture()
        values["QQQ"] = indicator("QQQ", r60=.02, r120=.5, vol60=.01)
        values["SGOV"] = indicator("SGOV", r60=.03)
        self.assertEqual(core_targets(values).regime, "RISK-OFF")
        values["QQQ"] = replace(values["QQQ"], r60=.03)
        self.assertEqual(core_targets(values).regime, "RISK-ON")

    def test_top_two_targets_and_alternative_defensive(self):
        values = core_fixture()
        values["SMH"] = indicator("SMH", r60=.3)
        values["GLD"] = indicator("GLD", r60=.2)
        self.assertEqual(core_targets(values).targets, {"SMH": .35, "GLD": .35})
        values["BIL"] = indicator("BIL", r60=.4)
        self.assertEqual(core_targets(values, "BIL").targets, {"BIL": .70})

    def test_zero_volatility_or_missing_universe_fails_closed(self):
        values = core_fixture()
        values["SMH"] = indicator("SMH", vol60=0)
        with self.assertRaises(StrategyDataError):
            core_targets(values)
        del values["SMH"]
        with self.assertRaises(StrategyDataError):
            core_targets(values)


class SwingTests(unittest.TestCase):
    def test_swing_comparisons_reject_missing_current_quotes(self):
        values = {symbol: indicator(symbol) for symbol in SWING_UNIVERSE}
        values["AAPL"] = indicator("AAPL", price=None)
        with self.assertRaisesRegex(StrategyDataError, "AAPL: current quote required"):
            swing_entries(values, [])
        with self.assertRaisesRegex(StrategyDataError, "AAPL: current quote required"):
            swing_exit_reasons(values["AAPL"], 110)
        with self.assertRaisesRegex(StrategyDataError, "AAPL: current quote required"):
            swing_exits(values, {"AAPL": 110})

    def test_exit_thresholds_are_strict(self):
        # All equalities: price==SMA5==SMA200==entry-2.5*ATR and RSI==70.
        position = indicator(price=100, sma5=100, sma200=100, rsi2=70, atr14=4)
        self.assertEqual(swing_exit_reasons(position, 110), ())
        below = replace(position, price=99.99)
        self.assertEqual(swing_exit_reasons(below, 110), (
            "CATASTROPHE_ATR_STOP", "PRICE_BELOW_SMA200",
        ))
        above = replace(position, price=100.01, rsi2=70.01)
        self.assertEqual(swing_exit_reasons(above, 110), ("PRICE_ABOVE_SMA5", "RSI2_ABOVE_70"))

    def test_only_provided_held_positions_are_examined_for_exits(self):
        values = {"AAPL": indicator("AAPL", rsi2=80), "MSFT": indicator("MSFT")}
        self.assertEqual(swing_exits(values, {"AAPL": 110, "MSFT": 110}), {
            "AAPL": ("RSI2_ABOVE_70",),
        })
        with self.assertRaises(StrategyDataError):
            swing_exits(values, {"NVDA": 100})

    def test_entry_thresholds_slots_and_deterministic_ties(self):
        values = {symbol: indicator(symbol, rsi2=10) for symbol in SWING_UNIVERSE}
        values["AAPL"] = indicator("AAPL", price=100, rsi2=1)  # SMA equality fails.
        values["MSFT"] = indicator("MSFT", rsi2=3)
        values["NVDA"] = indicator("NVDA", rsi2=3)
        values["AMZN"] = indicator("AMZN", rsi2=2)
        values["SPY"] = indicator("SPY", rsi2=0)  # Core ETF cannot enter a swing slot.
        self.assertEqual(swing_entries(values, []), ["AMZN", "MSFT"])
        self.assertEqual(swing_entries(values, ["COST"]), ["AMZN"])
        self.assertEqual(swing_entries(values, ["COST", "V"]), [])
        self.assertEqual(swing_entries(values, [], excluded_symbols=["AMZN"]), ["MSFT", "NVDA"])

    def test_held_candidate_occupies_slot_and_cannot_be_reentered(self):
        values = {symbol: indicator(symbol) for symbol in SWING_UNIVERSE}
        self.assertEqual(swing_entries(values, ["AAPL"]), ["MSFT"])
        with self.assertRaises(StrategyDataError):
            swing_entries(values, [], max_slots=3)

    def test_missing_candidate_data_prevents_partial_universe_ranking(self):
        with self.assertRaises(StrategyDataError):
            swing_entries({"AAPL": indicator("AAPL")}, [])


if __name__ == "__main__":
    unittest.main()
