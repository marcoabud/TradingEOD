import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from src.research_data import ReadOnlySession, SYMBOLS, export_dataset, main
from src.safety import SafetyError


class ResearchExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name)/"dataset"
        self.days = [date(2026, 10, 28), date(2026, 10, 29)]
        self.calendar = [{"date": d.isoformat(), "open": "09:30", "close": "16:00"} for d in self.days]
        index = pd.MultiIndex.from_tuples([(s, pd.Timestamp(d, tz="America/New_York"))
                                          for s in SYMBOLS for d in self.days], names=["symbol", "timestamp"])
        self.frame = pd.DataFrame(dict(open=100, high=102, low=99, close=101, volume=100), index=index)
        self.client = Mock()
        self.client.get_stock_bars.return_value = NS(df=self.frame)

    def export(self):
        return export_dataset(self.days[0], self.days[-1], self.output,
                              client=self.client, calendar_rows=self.calendar)

    def test_adjusted_sip_dataset_and_checksums(self):
        result = self.export()
        req = self.client.get_stock_bars.call_args.args[0]
        self.assertEqual(req.adjustment.value, "all")
        self.assertEqual(req.feed.value, "sip")
        self.assertEqual(req.asof, "2026-10-29")
        self.assertEqual(req.end.astimezone(timezone.utc).microsecond, 999999)
        self.assertEqual(result["execution_mode"], "adjusted_close_proxy")
        for name, digest in result["files"].items():
            self.assertEqual(hashlib.sha256((self.output/name).read_bytes()).hexdigest(), digest)
        self.assertEqual(len(pd.read_csv(self.output/"daily.csv")), 34)

    def test_calendar_hole_never_fills_prices(self):
        self.client.get_stock_bars.return_value = NS(df=self.frame.drop(self.frame.index[0]))
        with self.assertRaisesRegex(SafetyError, "Missing, stale"):
            self.export()
        self.assertFalse(self.output.exists())

    def test_existing_dataset_never_overwritten(self):
        self.output.mkdir()
        with self.assertRaisesRegex(SafetyError, "new directory"):
            self.export()
        self.client.get_stock_bars.assert_not_called()

    def test_invalid_bar_reports_exact_safe_date_and_field_without_repair(self):
        self.frame.loc[("SGOV", pd.Timestamp(self.days[0], tz="America/New_York")), "volume"] = 0
        with self.assertRaisesRegex(SafetyError, '"last_invalid": "2026-10-28"') as raised:
            self.export()
        self.assertIn('"volume": 0.0', str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_read_only_transport_rejects_mutations_and_account_endpoints(self):
        with ReadOnlySession() as session, patch("requests.Session.request") as request:
            for method, url in (("POST", "https://data.alpaca.markets/v2/stocks/bars"),
                                ("GET", "https://paper-api.alpaca.markets/v2/orders"),
                                ("GET", "https://paper-api.alpaca.markets/v2/account")):
                with self.subTest(url=url), self.assertRaises(SafetyError):
                    session.request(method, url)
            request.assert_not_called()

    def test_upstream_errors_are_not_exposed_in_hosted_logs(self):
        with patch.dict("os.environ", {"ALPACA_API_KEY":"x", "ALPACA_SECRET_KEY":"y"}, clear=True), \
                patch("src.research_data.ReadOnlySession.get", side_effect=RuntimeError("secret-provider-url")), \
                patch("builtins.print") as printed:
            self.assertEqual(main(["--output", str(self.output)]), 1)
        self.assertNotIn("secret-provider-url", str(printed.call_args))


if __name__ == "__main__":
    unittest.main()
