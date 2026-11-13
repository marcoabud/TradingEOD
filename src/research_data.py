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
import time

import pandas as pd
import numpy as np
import requests
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

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


def intraday_marks(frame, day, opens, closes, daily_close):
    """Reference prices at close-30min (signal) and close-15min (execution).

    A 15-minute bar starting exactly at the reference time gives its open (the
    first consolidated trade of that interval). Without one, the close of the
    latest earlier regular-session bar that day is used; with no intraday bar
    at all, the validated daily close. Both substitutions are counted.
    """
    result, kinds = [], []
    session = frame[(frame.index >= opens) & (frame.index < closes)] if frame is not None else None
    for minutes in (30, 15):
        at = closes - timedelta(minutes=minutes)
        exact = session[session.index == at] if session is not None else None
        earlier = session[session.index < at] if session is not None else None
        if exact is not None and len(exact):
            price, kind = float(exact["open"].iloc[0]), "exact"
        elif earlier is not None and len(earlier):
            price, kind = float(earlier["close"].iloc[-1]), "earlier_bar"
        else:
            price, kind = float(daily_close), "daily_close"
        if not np.isfinite(price) or price <= 0:
            raise SafetyError(f"Invalid intraday reference price: {day}")
        result.append(price)
        kinds.append(kind)
    return result, kinds


def export_intraday(output, *, client, calendar_rows, end, pause=time.sleep):
    """Add intraday.csv to an exported dataset: same feed, adjustment and asof."""
    daily = pd.read_csv(output/"daily.csv")
    rows = sorted(calendar_rows, key=lambda r: r["date"])
    days = [date.fromisoformat(r["date"]) for r in rows]
    bounds = {d: (session_time(r["open"], d), session_time(r["close"], d)) for r, d in zip(rows, days)}
    closes = {(r.symbol, r.date): r.close for r in daily.itertuples(index=False)}
    records, counts = [], {"exact": 0, "earlier_bar": 0, "daily_close": 0}
    # Quarterly requests bound memory; the SDK follows pagination inside each.
    cursor = days[0]
    while cursor <= days[-1]:
        stop = min(days[-1], cursor+timedelta(days=90))
        frame = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=list(SYMBOLS), timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=midnight(cursor), end=midnight(stop+timedelta(days=1))-timedelta(microseconds=1),
            adjustment=Adjustment.ALL, feed=DataFeed.SIP, asof=end.isoformat(),
        )).df
        present = set(frame.index.get_level_values("symbol")) if len(frame) else set()
        for symbol in SYMBOLS:
            bars = frame.xs(symbol, level="symbol") if symbol in present else None
            if bars is not None:
                bars = bars.copy()
                bars.index = bars.index.tz_convert(NY)
            for day in (d for d in days if cursor <= d <= stop):
                (signal, execution), kinds = intraday_marks(
                    bars, day, *bounds[day], closes[(symbol, day.isoformat())])
                for kind in kinds:
                    counts[kind] += 1
                records.append({"date": day.isoformat(), "symbol": symbol,
                                "signal_price": signal, "execution_price": execution})
        cursor = stop+timedelta(days=1)
        pause(3)  # Stay well under the provider's per-minute request limit.
    pd.DataFrame(records).sort_values(["date", "symbol"]).to_csv(output/"intraday.csv", index=False)
    meta = {"schema_version": 1, "feed": "sip", "adjustment": "all", "bar_minutes": 15,
            "signal_minutes_before_close": 30, "execution_minutes_before_close": 15,
            "reference_sources": counts,
            "sha256": hashlib.sha256((output/"intraday.csv").read_bytes()).hexdigest(),
            "limitations": ["Bar opens are first trades of the interval, not NBBO midpoints or fills",
                            "Substituted references are counted in reference_sources"]}
    (output/"intraday.json").write_text(json.dumps(meta, indent=2)+"\n", encoding="utf-8")
    return meta


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intraday", action="store_true",
                        help="also export close-30min and close-15min reference prices")
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
            intraday = (export_intraday(args.output, client=client, calendar_rows=rows, end=args.end)
                        if args.intraday else None)
        finally:
            client._session.close()
        print(json.dumps({"status": "exported", "sessions": manifest["sessions"],
                          "symbols": len(SYMBOLS), "execution_mode": manifest["execution_mode"],
                          "intraday_reference_sources": intraday["reference_sources"] if intraday else None}))
        return 0
    except Exception as exc:
        # Provider exceptions can carry URLs/credentials. Only fixed/sanitized
        # safety messages and exception class names are allowed in hosted logs.
        print(json.dumps({"status": "blocked", "reason": str(exc) if isinstance(exc, SafetyError)
                          else f"Historical export failed: {type(exc).__name__}"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
