#!/usr/bin/env python3
"""
swing_live_bar_updater.py
-------------------------

Live multi-timeframe bar-cache updater for the Swing Desk.

Purpose:
- Read the live swing universe from swing_results/live_swing_universe.csv.
- Pull latest Alpaca bars for Daily / 1H / 15m / 5m.
- Write separate live Swing cache folders:
    /opt/strategy-discovery/data/live_swing_daily
    /opt/strategy-discovery/data/live_swing_1h
    /opt/strategy-discovery/data/live_swing_15m
    /opt/strategy-discovery/data/live_swing_5m
- Keep intraday caches regular-session only: 09:30 <= bar_start < 16:00 ET.
- Does not touch day-trade production files and does not route orders.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import requests

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


VERSION = "swing_live_bar_updater_v1.1.1_progress_timeout"

PROJECT_DIR = Path(os.getenv("ELITE_PROJECT_DIR", Path(__file__).resolve().parent)).resolve()
DATA_BASE_URL = "https://data.alpaca.markets/v2"

DEFAULT_SYMBOLS_FILE = PROJECT_DIR / "swing_results" / "live_swing_universe.csv"
DEFAULT_SUMMARY_FILE = PROJECT_DIR / "swing_results" / "swing_live_bar_update_summary.json"

DEFAULT_DAILY_ROOT = Path(os.getenv("SWING_LIVE_DAILY_ROOT", "/opt/strategy-discovery/data/live_swing_daily"))
DEFAULT_HOURLY_ROOT = Path(os.getenv("SWING_LIVE_HOURLY_ROOT", "/opt/strategy-discovery/data/live_swing_1h"))
DEFAULT_M15_ROOT = Path(os.getenv("SWING_LIVE_M15_ROOT", "/opt/strategy-discovery/data/live_swing_15m"))
DEFAULT_M5_ROOT = Path(os.getenv("SWING_LIVE_M5_ROOT", "/opt/strategy-discovery/data/live_swing_5m"))


def et_tz():
    if ZoneInfo:
        return ZoneInfo("America/New_York")
    return timezone.utc


def now_et() -> datetime:
    return datetime.now(et_tz())


def utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def clean_symbol(value: Any) -> str:
    sym = str(value or "").strip().upper()
    if not sym or sym in {"SYMBOL", "TICKER"}:
        return ""
    if any(ch in sym for ch in ["^", "=", "/", " "]):
        return ""
    if "." in sym:
        return ""
    if len(sym) > 8:
        return ""
    if not re.match(r"^[A-Z][A-Z0-9]*$", sym):
        return ""
    return sym


def safe_symbol_filename(symbol: str) -> str:
    # Current scanner rejects dot/slash class symbols, but keep this safe anyway.
    return re.sub(r"[^A-Z0-9._-]", "_", symbol.upper())


def chunked(items: Sequence[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), max(1, size)):
        yield list(items[i : i + max(1, size)])


def credentials() -> Tuple[str, str]:
    key = (
        os.getenv("ALPACA_API_KEY")
        or os.getenv("APCA_API_KEY_ID")
        or os.getenv("ALPACA_KEY_ID")
        or ""
    ).strip()
    secret = (
        os.getenv("ALPACA_SECRET_KEY")
        or os.getenv("APCA_API_SECRET_KEY")
        or os.getenv("ALPACA_API_SECRET")
        or ""
    ).strip()
    if not key or not secret:
        raise RuntimeError("Missing Alpaca credentials. Load ALPACA_API_KEY/ALPACA_SECRET_KEY or APCA_API_KEY_ID/APCA_API_SECRET_KEY.")
    return key, secret


def headers() -> Dict[str, str]:
    key, secret = credentials()
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def request_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 60, retries: int = 3) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers(), params=params or {}, timeout=timeout)
            if r.status_code == 429 and attempt < retries:
                time.sleep(2.0 * attempt)
                continue
            if r.status_code >= 400:
                body = r.text[:500] if getattr(r, "text", None) else ""
                raise RuntimeError(f"HTTP {r.status_code}: {body}")
            return r.json() if r.content else {}
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
                continue
    raise RuntimeError(str(last_error) if last_error else "request failed")


def read_symbols(path: Path) -> List[str]:
    if not path.exists():
        raise FileNotFoundError(f"symbols file not found: {path}")

    symbols: List[str] = []
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        raw = data.get("symbols", data) if isinstance(data, dict) else data
        if isinstance(raw, list):
            for item in raw:
                sym = clean_symbol(item.get("symbol") if isinstance(item, dict) else item)
                if sym:
                    symbols.append(sym)
    else:
        with path.open(newline="", errors="ignore") as f:
            sample = f.read(4096)
            f.seek(0)
            has_header = "symbol" in sample.lower().splitlines()[0] if sample.splitlines() else False
            if has_header:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = clean_symbol(row.get("symbol") or row.get("ticker") or row.get("Symbol") or row.get("Ticker"))
                    if sym:
                        symbols.append(sym)
            else:
                reader2 = csv.reader(f)
                for row in reader2:
                    if not row:
                        continue
                    sym = clean_symbol(row[0])
                    if sym:
                        symbols.append(sym)

    out = sorted(dict.fromkeys(symbols))
    if not out:
        raise RuntimeError(f"no valid symbols found in {path}")
    return out


def fetch_bars(
    symbols: Sequence[str],
    timeframe: str,
    start: datetime,
    end: datetime,
    feed: str,
    chunk_size: int,
    adjustment: str,
    request_timeout: int,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, str]]:
    all_bars: Dict[str, List[Dict[str, Any]]] = {s: [] for s in symbols}
    errors: Dict[str, str] = {}

    chunks = list(chunked(list(symbols), chunk_size))
    total_chunks = len(chunks)

    for chunk_idx, chunk in enumerate(chunks, start=1):
        page_token = None
        pages = 0
        first_sym = chunk[0] if chunk else ""
        last_sym = chunk[-1] if chunk else ""
        print(
            f"  Fetch {timeframe} chunk [{chunk_idx}/{total_chunks}] "
            f"symbols={len(chunk)} {first_sym}-{last_sym}",
            flush=True,
        )

        while True:
            params: Dict[str, Any] = {
                "symbols": ",".join(chunk),
                "timeframe": timeframe,
                "start": utc_iso(start),
                "end": utc_iso(end),
                "adjustment": adjustment,
                "feed": feed,
                "limit": 10000,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token

            try:
                data = request_json(f"{DATA_BASE_URL}/stocks/bars", params=params, timeout=request_timeout)
            except Exception as exc:
                errors[",".join(chunk[:5])] = repr(exc)
                break

            bars_by_symbol = data.get("bars", {}) if isinstance(data, dict) else {}
            if isinstance(bars_by_symbol, dict):
                for sym, rows in bars_by_symbol.items():
                    cs = clean_symbol(sym)
                    if not cs:
                        continue
                    if isinstance(rows, list):
                        all_bars.setdefault(cs, []).extend([r for r in rows if isinstance(r, dict)])

            pages += 1
            page_token = data.get("next_page_token") if isinstance(data, dict) else None
            if not page_token:
                break
            if pages >= 40:
                errors[",".join(chunk[:5])] = "max pagination pages reached"
                break

    return all_bars, errors


def bars_to_df(rows: List[Dict[str, Any]], regular_session_only: bool) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    mapping = {
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "vw": "vwap",
        "n": "trade_count",
    }
    for old, new in mapping.items():
        if old in df.columns and new not in df.columns:
            df[new] = df[old]

    if "t" in df.columns:
        ts_utc = pd.to_datetime(df["t"], utc=True, errors="coerce")
    elif "timestamp_utc" in df.columns:
        ts_utc = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
    else:
        ts_utc = pd.to_datetime(df.index, utc=True, errors="coerce")

    df["timestamp_utc"] = ts_utc
    try:
        ts_et = ts_utc.dt.tz_convert("America/New_York")
    except Exception:
        ts_et = ts_utc
    df["timestamp_et"] = ts_et.dt.strftime("%Y-%m-%d %H:%M:%S%z")
    df["date_et"] = ts_et.dt.strftime("%Y-%m-%d")

    try:
        bar_time = ts_et.dt.time
        df["is_regular_session"] = (
            (bar_time >= pd.to_datetime("09:30").time())
            & (bar_time < pd.to_datetime("16:00").time())
        )
    except Exception:
        df["is_regular_session"] = True

    if regular_session_only:
        df = df[df["is_regular_session"] == True].copy()

    for col in ["open", "high", "low", "close", "volume", "vwap", "trade_count"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["timestamp_utc", "open", "high", "low", "close"])
    if df.empty:
        return df

    cols = [
        "timestamp_utc",
        "timestamp_et",
        "date_et",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
        "trade_count",
        "is_regular_session",
    ]
    for col in cols:
        if col not in df.columns:
            df[col] = None

    return df[cols].sort_values("timestamp_utc").drop_duplicates("timestamp_utc").reset_index(drop=True)


def write_frame(root: Path, symbol: str, df: pd.DataFrame) -> bool:
    if df.empty:
        return False
    root.mkdir(parents=True, exist_ok=True)
    out_path = root / f"{safe_symbol_filename(symbol)}.parquet"
    df.to_parquet(out_path, index=False)
    return True


def latest_info(df: pd.DataFrame) -> Tuple[str, str]:
    if df.empty:
        return "", ""
    latest_date = ""
    latest_time = ""
    try:
        latest_date = str(df["date_et"].dropna().iloc[-1])
    except Exception:
        pass
    try:
        ts = pd.to_datetime(df["timestamp_utc"].iloc[-1], utc=True, errors="coerce")
        if pd.notna(ts):
            if ZoneInfo:
                latest_time = ts.tz_convert("America/New_York").strftime("%Y-%m-%d %H:%M:%S")
            else:
                latest_time = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        pass
    return latest_date, latest_time


def update_timeframe(
    symbols: Sequence[str],
    label: str,
    timeframe: str,
    root: Path,
    lookback_days: int,
    feed: str,
    chunk_size: int,
    regular_session_only: bool,
    adjustment: str,
    request_timeout: int,
) -> Dict[str, Any]:
    start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    end = datetime.now(timezone.utc)

    print(f"Updating {label}: timeframe={timeframe} symbols={len(symbols)} lookback_days={lookback_days} root={root}")

    bars_by_symbol, errors = fetch_bars(
        symbols=symbols,
        timeframe=timeframe,
        start=start,
        end=end,
        feed=feed,
        chunk_size=chunk_size,
        adjustment=adjustment,
        request_timeout=request_timeout,
    )

    processed_ok = 0
    empty = 0
    latest_date = ""
    latest_time = ""

    for idx, sym in enumerate(symbols, start=1):
        if idx == 1 or idx % 100 == 0 or idx == len(symbols):
            print(f"  [{idx}/{len(symbols)}] {label} {sym}", flush=True)

        df = bars_to_df(bars_by_symbol.get(sym, []), regular_session_only=regular_session_only)
        if df.empty:
            empty += 1
            continue

        if write_frame(root, sym, df):
            processed_ok += 1
            d, t = latest_info(df)
            if d > latest_date:
                latest_date = d
            if t > latest_time:
                latest_time = t

    return {
        "label": label,
        "timeframe": timeframe,
        "root": str(root),
        "lookback_days": lookback_days,
        "regular_session_only": bool(regular_session_only),
        "processed_ok": processed_ok,
        "empty": empty,
        "errors": dict(list(errors.items())[:30]),
        "error_count": len(errors),
        "latest_date_et": latest_date,
        "latest_time_et": latest_time,
    }


def run(args: argparse.Namespace) -> Dict[str, Any]:
    symbols = read_symbols(Path(args.symbols_file))
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]

    print("=== SWING LIVE BAR UPDATER ===")
    print(f"Version: {VERSION}")
    print(f"Symbols: {len(symbols)}")
    print(f"Feed: {args.feed.upper()} adjustment={args.adjustment}")
    print("Regular session only for intraday caches: True")

    results: Dict[str, Any] = {}

    if not args.skip_daily:
        results["daily"] = update_timeframe(
            symbols,
            label="daily",
            timeframe="1Day",
            root=Path(args.daily_root),
            lookback_days=args.daily_lookback_days,
            feed=args.feed,
            chunk_size=args.daily_chunk_size,
            regular_session_only=False,
            adjustment=args.adjustment,
            request_timeout=args.request_timeout,
        )

    if not args.skip_hourly:
        results["hourly"] = update_timeframe(
            symbols,
            label="1h",
            timeframe="1Hour",
            root=Path(args.hourly_root),
            lookback_days=args.hourly_lookback_days,
            feed=args.feed,
            chunk_size=args.hourly_chunk_size,
            regular_session_only=True,
            adjustment=args.adjustment,
            request_timeout=args.request_timeout,
        )

    if not args.skip_m15:
        results["m15"] = update_timeframe(
            symbols,
            label="15m",
            timeframe="15Min",
            root=Path(args.m15_root),
            lookback_days=args.m15_lookback_days,
            feed=args.feed,
            chunk_size=args.m15_chunk_size,
            regular_session_only=True,
            adjustment=args.adjustment,
            request_timeout=args.request_timeout,
        )

    if not args.skip_m5:
        results["m5"] = update_timeframe(
            symbols,
            label="5m",
            timeframe="5Min",
            root=Path(args.m5_root),
            lookback_days=args.m5_lookback_days,
            feed=args.feed,
            chunk_size=args.m5_chunk_size,
            regular_session_only=True,
            adjustment=args.adjustment,
            request_timeout=args.request_timeout,
        )

    processed_ok_any = set()
    for key in ["daily", "hourly", "m15", "m5"]:
        root = None
        if key == "daily":
            root = Path(args.daily_root)
        elif key == "hourly":
            root = Path(args.hourly_root)
        elif key == "m15":
            root = Path(args.m15_root)
        elif key == "m5":
            root = Path(args.m5_root)
        if root and root.exists():
            for p in root.glob("*.parquet"):
                processed_ok_any.add(p.stem.upper())

    summary = {
        "version": VERSION,
        "generated_at_et": now_et().isoformat(timespec="seconds"),
        "symbols_file": str(args.symbols_file),
        "symbols_requested": len(symbols),
        "symbols_with_any_cache": len(processed_ok_any),
        "feed": args.feed,
        "adjustment": args.adjustment,
        "regular_session_only": True,
        "results": results,
    }

    out = Path(args.summary_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Saved: {out}")
    print("Done.")
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Update live Swing Desk Daily/1H/15m/5m bar caches from Alpaca")
    p.add_argument("--symbols-file", default=str(DEFAULT_SYMBOLS_FILE))
    p.add_argument("--summary-file", default=str(DEFAULT_SUMMARY_FILE))
    p.add_argument("--feed", default=os.getenv("ALPACA_DATA_FEED", os.getenv("ALPACA_FEED", "sip")).lower(), choices=["sip", "iex"])
    p.add_argument("--adjustment", default="raw", choices=["raw", "split", "dividend", "all"])

    p.add_argument("--daily-root", default=str(DEFAULT_DAILY_ROOT))
    p.add_argument("--hourly-root", default=str(DEFAULT_HOURLY_ROOT))
    p.add_argument("--m15-root", default=str(DEFAULT_M15_ROOT))
    p.add_argument("--m5-root", default=str(DEFAULT_M5_ROOT))

    p.add_argument("--daily-lookback-days", type=int, default=int(os.getenv("SWING_DAILY_LOOKBACK_DAYS", "460")))
    p.add_argument("--hourly-lookback-days", type=int, default=int(os.getenv("SWING_HOURLY_LOOKBACK_DAYS", "90")))
    p.add_argument("--m15-lookback-days", type=int, default=int(os.getenv("SWING_M15_LOOKBACK_DAYS", "20")))
    p.add_argument("--m5-lookback-days", type=int, default=int(os.getenv("SWING_M5_LOOKBACK_DAYS", "10")))

    p.add_argument("--daily-chunk-size", type=int, default=int(os.getenv("SWING_UPDATER_DAILY_CHUNK_SIZE", "20")))
    p.add_argument("--hourly-chunk-size", type=int, default=int(os.getenv("SWING_UPDATER_HOURLY_CHUNK_SIZE", "25")))
    p.add_argument("--m15-chunk-size", type=int, default=int(os.getenv("SWING_UPDATER_M15_CHUNK_SIZE", "25")))
    p.add_argument("--m5-chunk-size", type=int, default=int(os.getenv("SWING_UPDATER_M5_CHUNK_SIZE", "15")))
    p.add_argument("--request-timeout", type=int, default=int(os.getenv("SWING_UPDATER_REQUEST_TIMEOUT", "45")))

    p.add_argument("--limit-symbols", type=int, default=None, help="Testing only.")
    p.add_argument("--skip-daily", action="store_true")
    p.add_argument("--skip-hourly", action="store_true")
    p.add_argument("--skip-m15", action="store_true")
    p.add_argument("--skip-m5", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
