"""GET-only, credential-sanitized research dataset export; never trading I/O.

The adjusted close is a research proxy, not an executable 15:45 quote. A fixed
surviving universe is used; neither results nor this export activate a strategy.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import numpy as np
import requests
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from .data import validate_bars
from .safety import NY, SafetyError, midnight, session_time
from .strategy import DEFENSIVE_SYMBOL, OFFENSIVE_UNIVERSE, SWING_UNIVERSE

SYMBOLS = (*OFFENSIVE_UNIVERSE, DEFENSIVE_SYMBOL, *SWING_UNIVERSE)


class ReadOnlySession(requests.Session):
    def request(self, method, url, **kwargs):
        if method.upper() != "GET":
            raise SafetyError("Research exporter permits GET only")
        if not (url.startswith("https://data.alpaca.markets/v2/stocks/bars") or
                url == "https://paper-api.alpaca.markets/v2/calendar"):
            raise SafetyError("Research exporter URL outside allowlist")
        kwargs.update(timeout=(5, 30), allow_redirects=False)
        return super().request(method, url, **kwargs)


def export_dataset(start, end, output, *, client, calendar_rows, retrieved_at=None):
    """Validate full shared calendar coverage; fail on holes, never fill prices."""
    if start > end or (end-start).days > 365*10:
        raise SafetyError("Research date range invalid or longer than ten years")
    if output.exists():
        raise SafetyError("Research output must be a new directory")
    rows = sorted(calendar_rows, key=lambda r: r["date"])
    dates = [date.fromisoformat(row["date"]) for row in rows]
    if not dates or len(set(dates)) != len(dates) or any(not start <= d <= end for d in dates):
        raise SafetyError("Research calendar missing, duplicate or out of range")
    pieces = []
    cursor = start
    # SDK follows pagination within each bounded yearly request. ALL adjusts
    # splits, cash dividends and spin-offs; asof only maps ticker identities.
    while cursor <= end:
        stop = min(end, cursor+timedelta(days=364))
        response = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=list(SYMBOLS), timeframe=TimeFrame.Day,
            start=midnight(cursor), end=midnight(stop+timedelta(days=1))-timedelta(microseconds=1),
            adjustment=Adjustment.ALL, feed=DataFeed.SIP, asof=end.isoformat(),
        ))
        pieces.append(response.df)
        cursor = stop+timedelta(days=1)
    frame = pd.concat(pieces)
    if not isinstance(frame.index, pd.MultiIndex) or frame.empty:
        raise SafetyError("Research bars missing or malformed")
    daily = []
    for symbol in SYMBOLS:
        if symbol not in frame.index.get_level_values("symbol"):
            raise SafetyError(f"Research symbol missing: {symbol}")
        raw = frame.xs(symbol, level="symbol")
        try:
            clean = validate_bars(raw, dates, symbol)
        except SafetyError as exc:
            # Fixed market fields only. Surface the precise data defect without
            # provider bodies, URLs or credentials; never silently repair bars.
            columns = ["open", "high", "low", "close", "volume"]
            if set(columns).issubset(raw.columns):
                values = raw[columns].astype(float)
                bad = (~np.isfinite(values) | (values <= 0)).any(axis=1)
                invalid = values.loc[bad]
                if len(invalid):
                    sample = []
                    for timestamp, row in invalid.head(5).iterrows():
                        sample.append({"date": timestamp.tz_convert(NY).date().isoformat(),
                                       **{k: float(v) if np.isfinite(v) else None for k, v in row.items()}})
                    detail = {"symbol": symbol, "invalid_bars": len(invalid),
                              "last_invalid": invalid.index[-1].tz_convert(NY).date().isoformat(),
                              "samples": sample}
                    raise SafetyError("Historical data rejected: " + json.dumps(detail, allow_nan=False)) from None
            raise exc
        clean = clean[["open", "high", "low", "close", "volume"]].copy()
        clean.index.name = "date"
        clean = clean.reset_index()
        clean.insert(1, "symbol", symbol)
        daily.append(clean)
    calendar = pd.DataFrame([
        {"date": day.isoformat(), "open": session_time(row["open"], day).isoformat(),
         "close": session_time(row["close"], day).isoformat()}
        for row, day in zip(rows, dates)
    ])
    if any(session_time(r["open"], d) >= session_time(r["close"], d) for r, d in zip(rows, dates)):
        raise SafetyError("Research calendar open/close invalid")
    output.mkdir(parents=True)
    pd.concat(daily).sort_values(["date", "symbol"]).to_csv(output/"daily.csv", index=False)
    calendar.to_csv(output/"calendar.csv", index=False)
    manifest = {
        "schema_version": 1, "source": "Alpaca StockHistoricalDataClient", "feed": "sip",
        "adjustment": "all", "retrieved_at": (retrieved_at or datetime.now(timezone.utc)).isoformat(),
        "symbols": list(SYMBOLS), "price_basis": "total_return_adjusted",
        "execution_mode": "adjusted_close_proxy",
        "start": start.isoformat(), "end": end.isoformat(), "sessions": len(dates),
        "limitations": ["Fixed current universe has survivorship/selection bias",
                         "Adjusted closing marks are not 15:45 executable prices",
                         "Point-in-time corporate action vintages are not available"],
        "files": {name: hashlib.sha256((output/name).read_bytes()).hexdigest()
                  for name in ("daily.csv", "calendar.csv")},
    }
    (output/"manifest.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 8, 4))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 30))
    parser.add_argument("--output", type=Path, default=Path("research_data"))
    args = parser.parse_args(argv)
    try:
        if args.end >= datetime.now(NY).date():
            raise SafetyError("Research requires completed historical sessions")
        key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise SafetyError("Research credentials missing")
        with ReadOnlySession() as session:
            response = session.get("https://paper-api.alpaca.markets/v2/calendar",
                params={"start": args.start.isoformat(), "end": args.end.isoformat()},
                headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
            if response.status_code != 200:
                raise SafetyError(f"Research calendar HTTP {response.status_code}")
            rows = response.json()
        client = StockHistoricalDataClient(key, secret)
        client._session.close()
        client._session = ReadOnlySession()
        client._retry = 0
        try:
            manifest = export_dataset(args.start, args.end, args.output, client=client, calendar_rows=rows)
        finally:
            client._session.close()
        print(json.dumps({"status": "exported", "sessions": manifest["sessions"],
                          "symbols": len(SYMBOLS), "execution_mode": manifest["execution_mode"]}))
        return 0
    except Exception as exc:
        # Provider exceptions can carry URLs/credentials. Only fixed/sanitized
        # safety messages and exception class names are allowed in hosted logs.
        print(json.dumps({"status": "blocked", "reason": str(exc) if isinstance(exc, SafetyError)
                          else f"Historical export failed: {type(exc).__name__}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
