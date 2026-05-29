#!/usr/bin/env python3
"""
aggregate_1min_to_swing_bars.py

Convert raw 1-minute OHLCV parquet files into lightweight swing-timeframe bars:
- Daily candles for swing setup detection
- 1H candles for swing confirmation
- Optional 15m / 5m candles for future entry refinement

Design:
- Reads one ticker file at a time to stay VPS-safe.
- Does not modify production scanner/signal files.
- Intended input: folders like /opt/strategy-discovery/data/sp500_raw_1min_2y
- Intended output:
    /opt/strategy-discovery/data/sp500_swing_daily
    /opt/strategy-discovery/data/sp500_swing_1h

Version: aggregate_1min_to_swing_bars_v1.0.1_timestamp_utc_fix
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd


VERSION = "aggregate_1min_to_swing_bars_v1.0.1_timestamp_utc_fix"
REQUIRED_OHLC_COLUMNS = ("open", "high", "low", "close")


@dataclass
class FileResult:
    symbol: str
    source_file: str
    status: str
    input_rows: int = 0
    regular_rows: int = 0
    daily_rows: int = 0
    hourly_rows: int = 0
    m15_rows: int = 0
    m5_rows: int = 0
    daily_file: str = ""
    hourly_file: str = ""
    m15_file: str = ""
    m5_file: str = ""
    error: str = ""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Aggregate 1-minute parquet data into Daily and 1H swing bars. "
            "Reads one ticker at a time; safe for long VPS tmux runs."
        )
    )

    p.add_argument("--input-root", required=True, help="Folder containing raw 1-minute parquet files.")
    p.add_argument("--daily-root", required=True, help="Output folder for daily parquet files.")
    p.add_argument("--hourly-root", required=True, help="Output folder for 1H parquet files.")

    p.add_argument("--make-15m", action="store_true", help="Also create 15-minute bars.")
    p.add_argument("--m15-root", default="", help="Output folder for 15m bars. Required if --make-15m.")
    p.add_argument("--make-5m", action="store_true", help="Also create 5-minute bars.")
    p.add_argument("--m5-root", default="", help="Output folder for 5m bars. Required if --make-5m.")

    p.add_argument("--symbols", default="", help="Optional comma-separated symbol filter.")
    p.add_argument("--symbols-file", default="", help="Optional file containing symbols in first column/line.")
    p.add_argument("--limit-files", type=int, default=0, help="Limit files for staged testing. 0 = all.")
    p.add_argument("--skip-existing", action="store_true", help="Skip files if required output files already exist.")
    p.add_argument("--progress", action="store_true", help="Print progress for each file.")
    p.add_argument("--progress-every", type=int, default=25, help="Print progress every N files even without --progress.")

    p.add_argument("--timezone", default="America/New_York", help="Timezone used for ET regular-session aggregation.")
    p.add_argument(
        "--naive-timezone",
        default="UTC",
        help=(
            "Timezone to assume when source timestamps are tz-naive. "
            "Use UTC for Alpaca-style files; use America/New_York if your files are already ET but naive."
        ),
    )
    p.add_argument("--regular-start", default="09:30", help="Regular-session start HH:MM in ET. Default 09:30.")
    p.add_argument("--regular-end", default="16:00", help="Regular-session end HH:MM in ET, exclusive. Default 16:00.")
    p.add_argument("--min-regular-rows", type=int, default=60, help="Minimum regular-session 1m rows required per ticker.")

    p.add_argument(
        "--summary-file",
        default="",
        help="Optional summary JSON path. Default: <daily-root>/../swing_bar_aggregation_summary.json",
    )

    return p.parse_args()


def parse_clock(s: str) -> dtime:
    try:
        hh, mm = s.strip().split(":")[:2]
        return dtime(int(hh), int(mm))
    except Exception as exc:
        raise argparse.ArgumentTypeError(f"Invalid time '{s}'. Expected HH:MM") from exc


def normalize_symbol_from_path(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"(?i)(_?1min|_?1_min|_?minute|_raw|_bars)$", "", stem)
    stem = re.sub(r"(?i)(_?1min.*)$", "", stem)
    stem = stem.replace(".", "-").upper()
    return stem


def read_symbols_file(path: str) -> set[str]:
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"symbols-file not found: {path}")

    symbols: set[str] = set()
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        first = re.split(r"[,;\s]+", line)[0].strip().upper()
        if first and first != "SYMBOL":
            symbols.add(first)
    return symbols


def build_file_list(input_root: Path, symbols_filter: set[str], limit_files: int) -> List[Path]:
    if not input_root.exists():
        raise FileNotFoundError(f"input-root does not exist: {input_root}")

    files = sorted(input_root.glob("*.parquet"))
    if symbols_filter:
        files = [p for p in files if normalize_symbol_from_path(p) in symbols_filter]
    if limit_files and limit_files > 0:
        files = files[:limit_files]
    return files


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    original_cols = list(out.columns)

    rename: Dict[str, str] = {}
    lower_map = {str(c).strip().lower(): c for c in original_cols}

    aliases = {
        "timestamp": ["timestamp", "timestamp_utc", "timestamp_et", "time", "datetime", "date_time", "bar_time", "t"],
        "open": ["open", "o"],
        "high": ["high", "h"],
        "low": ["low", "l"],
        "close": ["close", "c", "price"],
        "volume": ["volume", "vol", "v"],
        "trade_count": ["trade_count", "tradecount", "n"],
        "vwap": ["vwap", "vw", "bar_vwap"],
    }

    for target, names in aliases.items():
        for name in names:
            if name in lower_map:
                rename[lower_map[name]] = target
                break

    out = out.rename(columns=rename)

    # Some project parquet files store time as timestamp_utc plus date_et/time_et.
    # v1.0.1 explicitly accepts timestamp_utc. If no timestamp-like column exists,
    # rebuild a timezone-aware ET timestamp from date_et + time_et when available.
    if "timestamp" not in out.columns:
        date_col = next((c for c in out.columns if str(c).strip().lower() == "date_et"), None)
        time_col = next((c for c in out.columns if str(c).strip().lower() == "time_et"), None)
        if date_col is not None and time_col is not None:
            combined = out[date_col].astype(str).str.strip() + " " + out[time_col].astype(str).str.strip()
            rebuilt = pd.to_datetime(combined, errors="coerce")
            try:
                rebuilt = rebuilt.dt.tz_localize("America/New_York", nonexistent="shift_forward", ambiguous="NaT")
            except Exception:
                pass
            out["timestamp"] = rebuilt

    if "timestamp" not in out.columns:
        idx = out.index
        if isinstance(idx, pd.DatetimeIndex):
            out = out.reset_index().rename(columns={out.index.name or "index": "timestamp"})
        else:
            for c in out.columns:
                if str(c).lower() in ("index", "__index_level_0__"):
                    parsed = pd.to_datetime(out[c], errors="coerce")
                    if parsed.notna().sum() > len(out) * 0.5:
                        out = out.rename(columns={c: "timestamp"})
                        break

    for col in REQUIRED_OHLC_COLUMNS:
        if col not in out.columns:
            raise ValueError(f"Missing required OHLC column '{col}'. Found columns: {list(out.columns)[:20]}")

    if "volume" not in out.columns:
        out["volume"] = 0

    keep_cols = ["timestamp", "open", "high", "low", "close", "volume"]
    optional = [c for c in ["trade_count", "vwap"] if c in out.columns]
    keep_cols += optional

    if "timestamp" not in out.columns:
        raise ValueError(f"Missing timestamp column/index. Original columns: {original_cols[:20]}")

    out = out[keep_cols].copy()

    for col in ["open", "high", "low", "close", "volume"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out.dropna(subset=["timestamp", "open", "high", "low", "close"])
    out = out[(out["high"] >= out["low"]) & (out["open"] > 0) & (out["close"] > 0)]

    return out


def add_et_time_columns(df: pd.DataFrame, timezone: str, naive_timezone: str) -> pd.DataFrame:
    out = df.copy()
    ts = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.loc[ts.notna()].copy()
    ts = ts.loc[ts.notna()]

    try:
        if getattr(ts.dt, "tz", None) is None:
            ts = ts.dt.tz_localize(naive_timezone, nonexistent="shift_forward", ambiguous="NaT")
        ts_et = ts.dt.tz_convert(timezone)
    except Exception:
        ts = pd.to_datetime(out["timestamp"], errors="coerce", utc=True)
        out = out.loc[ts.notna()].copy()
        ts = ts.loc[ts.notna()]
        ts_et = ts.dt.tz_convert(timezone)

    out["timestamp_et"] = ts_et
    out["date_et"] = ts_et.dt.date.astype(str)
    out["time_et"] = ts_et.dt.time
    out["minute_of_day_et"] = ts_et.dt.hour * 60 + ts_et.dt.minute
    return out


def filter_regular_session(df: pd.DataFrame, start: dtime, end: dtime) -> pd.DataFrame:
    start_min = start.hour * 60 + start.minute
    end_min = end.hour * 60 + end.minute
    out = df[(df["minute_of_day_et"] >= start_min) & (df["minute_of_day_et"] < end_min)].copy()
    out = out.sort_values("timestamp_et")
    return out


def aggregate_daily(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    grouped = df.groupby("date_et", sort=True)
    daily = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        regular_minutes=("close", "size"),
        first_timestamp_et=("timestamp_et", "first"),
        last_timestamp_et=("timestamp_et", "last"),
    ).reset_index()

    daily.insert(0, "symbol", symbol)
    daily["timestamp_et"] = pd.to_datetime(daily["date_et"] + " 00:00:00").dt.tz_localize("America/New_York")
    daily = daily[
        [
            "symbol",
            "date_et",
            "timestamp_et",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "regular_minutes",
            "first_timestamp_et",
            "last_timestamp_et",
        ]
    ]
    return daily


def aggregate_intraday_timeframe(
    df: pd.DataFrame,
    symbol: str,
    timeframe_minutes: int,
    label: str,
    regular_start: dtime,
) -> pd.DataFrame:
    start_min = regular_start.hour * 60 + regular_start.minute
    out = df.copy()
    out["bar_index"] = ((out["minute_of_day_et"] - start_min) // timeframe_minutes).astype(int)
    out = out[out["bar_index"] >= 0].copy()

    grouped = out.groupby(["date_et", "bar_index"], sort=True)
    bars = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        bars=("close", "size"),
        timestamp_et=("timestamp_et", "first"),
        end_timestamp_et=("timestamp_et", "last"),
    ).reset_index()

    bars.insert(0, "symbol", symbol)
    bars["timeframe"] = label
    bars["bar_start_minute_et"] = start_min + bars["bar_index"].astype(int) * timeframe_minutes
    bars["bar_start_time_et"] = bars["bar_start_minute_et"].apply(lambda x: f"{int(x)//60:02d}:{int(x)%60:02d}")
    bars = bars[
        [
            "symbol",
            "date_et",
            "timestamp_et",
            "end_timestamp_et",
            "timeframe",
            "bar_index",
            "bar_start_time_et",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "bars",
        ]
    ]
    return bars


def output_paths(
    symbol: str,
    daily_root: Path,
    hourly_root: Path,
    m15_root: Optional[Path],
    m5_root: Optional[Path],
) -> Tuple[Path, Path, Optional[Path], Optional[Path]]:
    return (
        daily_root / f"{symbol}_Daily.parquet",
        hourly_root / f"{symbol}_1H.parquet",
        (m15_root / f"{symbol}_15Min.parquet") if m15_root else None,
        (m5_root / f"{symbol}_5Min.parquet") if m5_root else None,
    )


def required_outputs_exist(symbol: str, args: argparse.Namespace) -> bool:
    daily_root = Path(args.daily_root)
    hourly_root = Path(args.hourly_root)
    m15_root = Path(args.m15_root) if args.make_15m else None
    m5_root = Path(args.m5_root) if args.make_5m else None
    daily_file, hourly_file, m15_file, m5_file = output_paths(symbol, daily_root, hourly_root, m15_root, m5_root)

    paths = [daily_file, hourly_file]
    if m15_file is not None:
        paths.append(m15_file)
    if m5_file is not None:
        paths.append(m5_file)
    return all(p.exists() and p.stat().st_size > 0 for p in paths)


def process_file(path: Path, args: argparse.Namespace, regular_start: dtime, regular_end: dtime) -> FileResult:
    symbol = normalize_symbol_from_path(path)
    daily_root = Path(args.daily_root)
    hourly_root = Path(args.hourly_root)
    m15_root = Path(args.m15_root) if args.make_15m else None
    m5_root = Path(args.m5_root) if args.make_5m else None

    result = FileResult(symbol=symbol, source_file=str(path), status="UNKNOWN")

    if args.skip_existing and required_outputs_exist(symbol, args):
        result.status = "SKIPPED_EXISTING"
        return result

    try:
        raw = pd.read_parquet(path)
        result.input_rows = int(len(raw))
        if raw.empty:
            result.status = "SKIPPED_EMPTY"
            return result

        df = normalize_columns(raw)
        if df.empty:
            result.status = "SKIPPED_NO_VALID_ROWS"
            return result

        df = add_et_time_columns(df, args.timezone, args.naive_timezone)
        regular = filter_regular_session(df, regular_start, regular_end)
        result.regular_rows = int(len(regular))

        if len(regular) < int(args.min_regular_rows):
            result.status = "SKIPPED_TOO_FEW_REGULAR_ROWS"
            return result

        daily = aggregate_daily(regular, symbol)
        hourly = aggregate_intraday_timeframe(regular, symbol, 60, "1H", regular_start)

        daily_file, hourly_file, m15_file, m5_file = output_paths(symbol, daily_root, hourly_root, m15_root, m5_root)

        daily.to_parquet(daily_file, index=False, compression="snappy")
        hourly.to_parquet(hourly_file, index=False, compression="snappy")

        result.daily_rows = int(len(daily))
        result.hourly_rows = int(len(hourly))
        result.daily_file = str(daily_file)
        result.hourly_file = str(hourly_file)

        if args.make_15m:
            if not m15_root or not m15_file:
                raise ValueError("--m15-root is required when --make-15m is used")
            m15 = aggregate_intraday_timeframe(regular, symbol, 15, "15Min", regular_start)
            m15.to_parquet(m15_file, index=False, compression="snappy")
            result.m15_rows = int(len(m15))
            result.m15_file = str(m15_file)

        if args.make_5m:
            if not m5_root or not m5_file:
                raise ValueError("--m5-root is required when --make-5m is used")
            m5 = aggregate_intraday_timeframe(regular, symbol, 5, "5Min", regular_start)
            m5.to_parquet(m5_file, index=False, compression="snappy")
            result.m5_rows = int(len(m5))
            result.m5_file = str(m5_file)

        result.status = "OK"
        return result

    except Exception as exc:
        result.status = "ERROR"
        result.error = repr(exc)
        return result


def write_summary(summary_path: Path, summary: Dict) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = summary_path.with_suffix(summary_path.suffix + ".tmp")
    tmp.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    tmp.replace(summary_path)


def build_summary(
    args: argparse.Namespace,
    input_root: Path,
    daily_root: Path,
    hourly_root: Path,
    summary_path: Path,
    results: Sequence[FileResult],
    started: float,
    done: bool,
) -> Dict:
    status_counts: Dict[str, int] = {}
    file_errors: Dict[str, str] = {}
    for r in results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1
        if r.error:
            file_errors[r.symbol] = r.error

    return {
        "version": VERSION,
        "run_time": datetime.now().isoformat(timespec="seconds"),
        "done": bool(done),
        "elapsed_seconds": round(time.time() - started, 2),
        "input_root": str(input_root),
        "daily_root": str(daily_root),
        "hourly_root": str(hourly_root),
        "m15_root": str(args.m15_root) if args.make_15m else "",
        "m5_root": str(args.m5_root) if args.make_5m else "",
        "limit_files": int(args.limit_files or 0),
        "files_seen": len(results),
        "processed_ok": sum(1 for r in results if r.status == "OK"),
        "skipped_existing": sum(1 for r in results if r.status == "SKIPPED_EXISTING"),
        "errors": sum(1 for r in results if r.status == "ERROR"),
        "status_counts": status_counts,
        "input_rows": int(sum(r.input_rows for r in results)),
        "regular_rows": int(sum(r.regular_rows for r in results)),
        "daily_rows": int(sum(r.daily_rows for r in results)),
        "hourly_rows": int(sum(r.hourly_rows for r in results)),
        "m15_rows": int(sum(r.m15_rows for r in results)),
        "m5_rows": int(sum(r.m5_rows for r in results)),
        "file_errors": file_errors,
        "results_tail": [asdict(r) for r in results[-10:]],
        "summary_file": str(summary_path),
        "note": "Daily + 1H are intended swing-scanner inputs. Raw 1m remains source only.",
    }


def main() -> int:
    args = parse_args()

    input_root = Path(args.input_root)
    daily_root = Path(args.daily_root)
    hourly_root = Path(args.hourly_root)

    if args.make_15m and not args.m15_root:
        print("ERROR: --m15-root is required with --make-15m", file=sys.stderr)
        return 2
    if args.make_5m and not args.m5_root:
        print("ERROR: --m5-root is required with --make-5m", file=sys.stderr)
        return 2

    daily_root.mkdir(parents=True, exist_ok=True)
    hourly_root.mkdir(parents=True, exist_ok=True)
    if args.make_15m:
        Path(args.m15_root).mkdir(parents=True, exist_ok=True)
    if args.make_5m:
        Path(args.m5_root).mkdir(parents=True, exist_ok=True)

    symbols_filter: set[str] = set()
    if args.symbols.strip():
        symbols_filter.update(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    if args.symbols_file:
        symbols_filter.update(read_symbols_file(args.symbols_file))

    files = build_file_list(input_root, symbols_filter, int(args.limit_files or 0))
    regular_start = parse_clock(args.regular_start)
    regular_end = parse_clock(args.regular_end)

    summary_path = Path(args.summary_file) if args.summary_file else daily_root.parent / "swing_bar_aggregation_summary.json"

    print("=== SWING BAR AGGREGATION ===")
    print(f"Version: {VERSION}")
    print(f"Input:  {input_root}")
    print(f"Daily:  {daily_root}")
    print(f"Hourly: {hourly_root}")
    if args.make_15m:
        print(f"15m:    {Path(args.m15_root)}")
    if args.make_5m:
        print(f"5m:     {Path(args.m5_root)}")
    print(f"Files:  {len(files)}")
    print(f"Session: {args.regular_start}-{args.regular_end} ET")
    print("Mode: one-file-at-a-time, VPS-safe")
    sys.stdout.flush()

    started = time.time()
    results: List[FileResult] = []

    for i, path in enumerate(files, start=1):
        res = process_file(path, args, regular_start, regular_end)
        results.append(res)

        should_print = (
            args.progress
            or i == 1
            or i == len(files)
            or (args.progress_every and i % args.progress_every == 0)
            or res.status == "ERROR"
        )
        if should_print:
            if res.status == "OK":
                extra = f"{res.symbol}: daily={res.daily_rows}, 1H={res.hourly_rows}"
                if args.make_15m:
                    extra += f", 15m={res.m15_rows}"
                if args.make_5m:
                    extra += f", 5m={res.m5_rows}"
                print(f"[{i}/{len(files)}] OK {extra}")
            elif res.status == "SKIPPED_EXISTING":
                print(f"[{i}/{len(files)}] SKIP existing {res.symbol}")
            else:
                msg = f"[{i}/{len(files)}] {res.status} {res.symbol}"
                if res.error:
                    msg += f" :: {res.error}"
                print(msg)
            sys.stdout.flush()

        if i % 25 == 0 or i == len(files):
            partial = build_summary(args, input_root, daily_root, hourly_root, summary_path, results, started, done=(i == len(files)))
            write_summary(summary_path, partial)

    final_summary = build_summary(args, input_root, daily_root, hourly_root, summary_path, results, started, done=True)
    write_summary(summary_path, final_summary)

    print("=== AGGREGATION COMPLETE ===")
    print(f"Processed OK: {final_summary['processed_ok']}/{final_summary['files_seen']}")
    print(f"Errors: {final_summary['errors']}")
    print(f"Daily rows: {final_summary['daily_rows']}")
    print(f"Hourly rows: {final_summary['hourly_rows']}")
    if args.make_15m:
        print(f"15m rows: {final_summary['m15_rows']}")
    if args.make_5m:
        print(f"5m rows: {final_summary['m5_rows']}")
    print(f"Summary: {summary_path}")
    print("Done.")
    return 0 if final_summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
