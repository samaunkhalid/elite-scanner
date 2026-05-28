#!/usr/bin/env python3
"""
Universal Swing Forward Backtester
Version: swing_forward_backtester_v1.0.2_universal

Purpose:
- Historical day-by-day validation for swing_scanner.py.
- Reuses the current swing_scanner.py logic as the candidate builder.
- Walks parquet history as-of each historical date, then forward-tests future
  regular-session candles for 1-3 trading days.
- Research / validation output only.
- No production Signal Desk, dashboard, broker, or alert files are modified.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

try:
    import swing_scanner as ss
except Exception as exc:  # pragma: no cover - import failure is fatal at runtime
    raise SystemExit(
        "ERROR: swing_forward_backtester.py must run from the project folder "
        "where swing_scanner.py is available. Original import error: "
        f"{exc!r}"
    )


BACKTESTER_VERSION = "swing_forward_backtester_v1.0.2_universal"


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def round4(x: Any) -> float:
    return round(safe_float(x), 4)


def parse_csv_set(value: str) -> set[str]:
    return {x.strip().upper() for x in str(value or "").split(",") if x.strip()}


def parse_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return str(pd.to_datetime(value).date())


def now_stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def regular_intraday_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = ss.standardize_ohlcv_columns(ss.infer_datetime_columns(df))
    out = out.dropna(subset=["date_et"]).copy()
    if "timestamp_utc" not in out.columns and "dt_et" in out.columns:
        out["timestamp_utc"] = pd.to_datetime(out["dt_et"], utc=True, errors="coerce")
    if "dt_et" in out.columns:
        out = out.sort_values("dt_et")
    out["date_et"] = out["date_et"].astype(str)
    if "is_regular_session" in out.columns:
        regular = out[out["is_regular_session"] == True].copy()  # noqa: E712
        if not regular.empty:
            return regular
    return out.copy()


def load_parquet_intraday(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if df.empty:
        return pd.DataFrame()
    return regular_intraday_frame(df)


def session_dates_from_regular(regular: pd.DataFrame) -> List[str]:
    if regular.empty or "date_et" not in regular.columns:
        return []
    return sorted(str(x) for x in regular["date_et"].dropna().astype(str).unique())


def slice_daily_through(daily: pd.DataFrame, date_et: str) -> pd.DataFrame:
    d = daily[daily["date_et"].astype(str) <= str(date_et)].copy()
    return d.sort_values("date_et").reset_index(drop=True)


def slice_intraday_date(regular: pd.DataFrame, date_et: str) -> pd.DataFrame:
    return regular[regular["date_et"].astype(str) == str(date_et)].copy()


def future_sessions(
    regular: pd.DataFrame,
    current_date: str,
    max_hold_days: int,
) -> Tuple[pd.DataFrame, List[str]]:
    dates = session_dates_from_regular(regular)
    fut = [d for d in dates if d > str(current_date)]
    fut = fut[:max(1, int(max_hold_days))]
    if not fut:
        return pd.DataFrame(), []
    bars = regular[regular["date_et"].astype(str).isin(fut)].copy()
    if "dt_et" in bars.columns:
        bars = bars.sort_values("dt_et")
    return bars, fut


def scanner_namespace(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        source="parquet",
        data_root=args.data_root,
        symbols=args.symbols,
        symbols_file=args.symbols_file,
        limit_files=args.limit_files,
        limit_symbols=None,
        mode=args.mode,
        output_dir=args.output_dir,
        earnings_csv=args.earnings_csv,
        alpaca_feed="sip",
        min_price=args.min_price,
        max_price=args.max_price,
        min_avg_volume=args.min_avg_volume,
        max_atr_pct=args.max_atr_pct,
        dry_run=True,
    )


def bar_time(row: pd.Series) -> str:
    try:
        return str(row["dt_et"].strftime("%Y-%m-%d %H:%M:%S %Z"))
    except Exception:
        return ""


def filter_bars_to_horizon(
    future_bars: pd.DataFrame,
    future_dates: Sequence[str],
    horizon_days: int,
) -> pd.DataFrame:
    dates = list(future_dates)[:max(1, int(horizon_days))]
    if not dates:
        return pd.DataFrame()
    out = future_bars[future_bars["date_et"].astype(str).isin(dates)].copy()
    if "dt_et" in out.columns:
        out = out.sort_values("dt_et")
    return out


def simulate_candidate_horizon(
    candidate: Dict[str, Any],
    future_bars: pd.DataFrame,
    future_dates: Sequence[str],
    horizon_days: int,
    same_bar_rule: str = "stop_first",
) -> Dict[str, Any]:
    entry = safe_float(candidate.get("entry_trigger"))
    stop = safe_float(candidate.get("stop_loss"))
    target1 = safe_float(candidate.get("target_1"))
    target2 = safe_float(candidate.get("target_2"))
    symbol = str(candidate.get("symbol") or "").upper()

    base = {
        "symbol": symbol,
        "horizon_days": int(horizon_days),
        "bad_plan": False,
        "entry_touched": False,
        "entry_date_et": "",
        "entry_time_et": "",
        "fill_price": 0.0,
        "outcome": "ENTRY_NOT_TOUCHED",
        "terminal_date_et": "",
        "terminal_time_et": "",
        "t1_hit": False,
        "t2_hit": False,
        "stop_hit": False,
        "stop_after_t1": False,
        "time_exit": False,
        "bars_after_entry": 0,
        "sessions_available": len(future_dates),
        "sessions_tested": min(len(future_dates), int(horizon_days)),
        "days_to_entry": None,
        "days_to_t1": None,
        "days_to_t2": None,
        "days_to_stop": None,
        "exit_price": 0.0,
        "exit_R": 0.0,
        "mfe_R": 0.0,
        "mae_R": 0.0,
    }

    risk = entry - stop
    if entry <= 0 or stop <= 0 or risk <= 0 or target1 <= entry:
        base["bad_plan"] = True
        base["outcome"] = "BAD_PLAN"
        return base

    bars = filter_bars_to_horizon(future_bars, future_dates, horizon_days)
    if bars.empty:
        base["outcome"] = "NO_FUTURE_REGULAR_SESSION"
        return base

    entered = False
    fill_price = 0.0
    t1_hit = False
    t2_hit = False
    stop_hit = False
    stop_after_t1 = False
    first_entry_date = ""
    t1_date = ""
    t2_date = ""
    stop_date = ""
    terminal_date = ""
    terminal_time = ""
    outcome = "TIME_EXIT"
    exit_price = 0.0
    max_high = None
    min_low = None
    bars_after_entry = 0

    # Map future session date to 1-based day count.
    session_index = {d: i + 1 for i, d in enumerate(future_dates[:horizon_days])}

    for _, row in bars.iterrows():
        high = safe_float(row.get("high"))
        low = safe_float(row.get("low"))
        open_ = safe_float(row.get("open"))
        close = safe_float(row.get("close"))
        date_et = str(row.get("date_et") or "")
        time_et = bar_time(row)

        if not entered:
            if high < entry:
                continue

            # Conservative long-entry fill:
            # If the entire trigger bar is already above the trigger, fill at open.
            # If the trigger trades through inside the bar, fill at the planned entry.
            fill_price = open_ if open_ > entry and low > entry else entry
            if fill_price <= 0:
                fill_price = entry

            entered = True
            first_entry_date = date_et
            base["entry_touched"] = True
            base["entry_date_et"] = date_et
            base["entry_time_et"] = time_et
            base["fill_price"] = round4(fill_price)
            base["days_to_entry"] = session_index.get(date_et)

        if not entered:
            continue

        bars_after_entry += 1
        max_high = high if max_high is None else max(max_high, high)
        min_low = low if min_low is None else min(min_low, low)

        stop_now = low <= stop
        t1_now = high >= target1
        t2_now = high >= target2

        # Before any target, same-bar stop/target ambiguity is handled
        # conservatively by default.
        if not t1_hit and not t2_hit:
            if stop_now and (t1_now or t2_now):
                if same_bar_rule == "target_first":
                    if t2_now:
                        t2_hit = True
                        t1_hit = True
                        t2_date = date_et
                        t1_date = date_et
                        outcome = "T2_HIT"
                        exit_price = target2
                        terminal_date = date_et
                        terminal_time = time_et
                        break
                    t1_hit = True
                    t1_date = date_et
                    # Continue after first target to see if T2 appears later.
                    continue
                stop_hit = True
                stop_date = date_et
                outcome = "STOP_HIT"
                exit_price = stop
                terminal_date = date_et
                terminal_time = time_et
                break

            if stop_now:
                stop_hit = True
                stop_date = date_et
                outcome = "STOP_HIT"
                exit_price = stop
                terminal_date = date_et
                terminal_time = time_et
                break

            if t2_now:
                t2_hit = True
                t1_hit = True
                t2_date = date_et
                t1_date = date_et
                outcome = "T2_HIT"
                exit_price = target2
                terminal_date = date_et
                terminal_time = time_et
                break

            if t1_now:
                t1_hit = True
                t1_date = date_et
                # T1 is a valid swing success marker. Continue to see whether
                # T2 is reached inside the requested hold window.
                continue

        else:
            if t2_now:
                t2_hit = True
                t2_date = date_et
                outcome = "T2_HIT"
                exit_price = target2
                terminal_date = date_et
                terminal_time = time_et
                break
            if stop_now:
                stop_after_t1 = True
                if not terminal_date:
                    terminal_date = date_et
                    terminal_time = time_et
                # Do not convert a prior T1 success into a full loss. Keep
                # scanning for T2; final outcome will be T1_HIT_STOP_AFTER_T1
                # if T2 never appears.

    if not entered:
        return base

    if t2_hit:
        outcome = "T2_HIT"
        exit_price = target2
    elif t1_hit:
        outcome = "T1_HIT_STOP_AFTER_T1" if stop_after_t1 else "T1_HIT"
        exit_price = target1
        if not terminal_date:
            terminal_date = t1_date
    elif stop_hit:
        outcome = "STOP_HIT"
        exit_price = stop
        if not terminal_date:
            terminal_date = stop_date
    else:
        last = bars.iloc[-1]
        exit_price = safe_float(last.get("close"))
        terminal_date = str(last.get("date_et") or "")
        terminal_time = bar_time(last)
        outcome = "TIME_EXIT"

    mfe_r = ((safe_float(max_high, fill_price) - fill_price) / risk) if risk > 0 else 0.0
    mae_r = ((safe_float(min_low, fill_price) - fill_price) / risk) if risk > 0 else 0.0
    exit_r = ((exit_price - fill_price) / risk) if risk > 0 else 0.0

    base.update({
        "outcome": outcome,
        "terminal_date_et": terminal_date or "",
        "terminal_time_et": terminal_time or "",
        "t1_hit": bool(t1_hit),
        "t2_hit": bool(t2_hit),
        "stop_hit": bool(stop_hit),
        "stop_after_t1": bool(stop_after_t1),
        "time_exit": outcome == "TIME_EXIT",
        "bars_after_entry": int(bars_after_entry),
        "days_to_t1": session_index.get(t1_date) if t1_date else None,
        "days_to_t2": session_index.get(t2_date) if t2_date else None,
        "days_to_stop": session_index.get(stop_date) if stop_date else None,
        "exit_price": round4(exit_price),
        "exit_R": round4(exit_r),
        "mfe_R": round4(mfe_r),
        "mae_R": round4(mae_r),
    })
    return base


def simulate_candidate(
    candidate: Dict[str, Any],
    future_bars: pd.DataFrame,
    future_dates: Sequence[str],
    max_hold_days: int,
    same_bar_rule: str,
) -> Dict[str, Any]:
    primary = simulate_candidate_horizon(
        candidate,
        future_bars,
        future_dates,
        max_hold_days,
        same_bar_rule=same_bar_rule,
    )
    for h in range(1, max_hold_days + 1):
        hres = simulate_candidate_horizon(
            candidate,
            future_bars,
            future_dates,
            h,
            same_bar_rule=same_bar_rule,
        )
        primary[f"h{h}_outcome"] = hres.get("outcome")
        primary[f"h{h}_exit_R"] = hres.get("exit_R")
        primary[f"h{h}_t1_hit"] = hres.get("t1_hit")
        primary[f"h{h}_t2_hit"] = hres.get("t2_hit")
        primary[f"h{h}_stop_hit"] = hres.get("stop_hit")
        primary[f"h{h}_mfe_R"] = hres.get("mfe_R")
        primary[f"h{h}_mae_R"] = hres.get("mae_R")
    return primary


def row_from_candidate(
    candidate: Any,
    source_file: str,
    test_date: str,
    future_dates: Sequence[str],
    sim: Dict[str, Any],
) -> Dict[str, Any]:
    cdict = asdict(candidate) if hasattr(candidate, "__dataclass_fields__") else dict(candidate)
    row: Dict[str, Any] = {}
    for col in getattr(ss, "OUT_COLUMNS", []):
        row[col] = cdict.get(col, "")
    for k, v in cdict.items():
        row.setdefault(k, v)
    row.update({
        "backtester_version": BACKTESTER_VERSION,
        "scanner_version": getattr(ss, "SCANNER_VERSION", ""),
        "source_file": source_file,
        "test_date_et": test_date,
        "future_session_dates": ",".join(future_dates),
    })
    row.update(sim)
    return row


def summarize_results(
    rows: List[Dict[str, Any]],
    counters: Dict[str, Any],
    args: argparse.Namespace,
    file_errors: Dict[str, str],
) -> Dict[str, Any]:
    df = pd.DataFrame(rows)
    summary: Dict[str, Any] = {
        "version": BACKTESTER_VERSION,
        "scanner_version": getattr(ss, "SCANNER_VERSION", ""),
        "run_time": now_stamp(),
        "source": args.source,
        "data_root": args.data_root or "",
        "mode": args.mode,
        "min_warmup_days": args.min_warmup_days,
        "max_hold_days": args.max_hold_days,
        "same_bar_rule": args.same_bar_rule,
        "files_seen": counters.get("files_seen", 0),
        "files_processed": counters.get("files_processed", 0),
        "days_scanned": counters.get("days_scanned", 0),
        "historical_candidate_days_seen": counters.get("historical_candidate_days_seen", 0),
        "candidates_built": counters.get("candidates_built", 0),
        "skipped_no_future_regular_session": counters.get("skipped_no_future_regular_session", 0),
        "skipped_entry_not_touched": counters.get("skipped_entry_not_touched", 0),
        "skipped_bad_plan": counters.get("skipped_bad_plan", 0),
        "forward_tested": counters.get("forward_tested", 0),
        "t1_hits": 0,
        "t2_hits": 0,
        "stop_hits": 0,
        "time_exits": 0,
        "win_rate_all_forward_tested_pct": 0.0,
        "win_rate_vs_stop_pct": 0.0,
        "median_exit_R": 0.0,
        "mean_exit_R": 0.0,
        "median_mfe_R": 0.0,
        "median_mae_R": 0.0,
        "status_counts": {},
        "setup_counts": {},
        "win_rate_by_setup": {},
        "hold_period_stats": {},
        "best_hold_period": "",
        "file_errors": dict(list(file_errors.items())[:50]),
        "acceptance_note": (
            "Research only. Swing candidates are not trade instructions until "
            "sample size and forward-test performance pass user acceptance rules."
        ),
        "acceptance_thresholds": {
            "swing_ready_active_sample_min": 500,
            "t1_t2_vs_stop_win_rate_min_pct": 50.0,
            "median_exit_R_min": 0.0,
            "stop_rate": "must be controlled",
            "negative_setup_family": "must be reviewed or blocked",
        },
    }

    if df.empty:
        return summary

    forward = df[df["entry_touched"] == True].copy()  # noqa: E712
    summary["t1_hits"] = int(forward["t1_hit"].fillna(False).astype(bool).sum()) if "t1_hit" in forward else 0
    summary["t2_hits"] = int(forward["t2_hit"].fillna(False).astype(bool).sum()) if "t2_hit" in forward else 0
    summary["stop_hits"] = int(forward["stop_hit"].fillna(False).astype(bool).sum()) if "stop_hit" in forward else 0
    summary["time_exits"] = int((forward["outcome"] == "TIME_EXIT").sum()) if "outcome" in forward else 0

    n = len(forward)
    if n:
        wins = (forward["t1_hit"].fillna(False).astype(bool) | forward["t2_hit"].fillna(False).astype(bool))
        stops_before_target = (forward["outcome"] == "STOP_HIT")
        resolved = wins | stops_before_target
        summary["win_rate_all_forward_tested_pct"] = round4(wins.sum() / n * 100.0)
        if int(resolved.sum()) > 0:
            summary["win_rate_vs_stop_pct"] = round4(wins[resolved].sum() / int(resolved.sum()) * 100.0)
        for col, key in [("exit_R", "median_exit_R"), ("mfe_R", "median_mfe_R"), ("mae_R", "median_mae_R")]:
            vals = pd.to_numeric(forward[col], errors="coerce").dropna() if col in forward else pd.Series(dtype=float)
            if not vals.empty:
                summary[key] = round4(vals.median())
        vals = pd.to_numeric(forward["exit_R"], errors="coerce").dropna() if "exit_R" in forward else pd.Series(dtype=float)
        if not vals.empty:
            summary["mean_exit_R"] = round4(vals.mean())

    if "swing_status" in df:
        summary["status_counts"] = {str(k): int(v) for k, v in df["swing_status"].fillna("").value_counts().to_dict().items() if str(k)}
    if "setup_type" in df:
        summary["setup_counts"] = {str(k): int(v) for k, v in df["setup_type"].fillna("").value_counts().to_dict().items() if str(k)}

    setup_stats: Dict[str, Any] = {}
    if not forward.empty and "setup_type" in forward:
        for setup, sub in forward.groupby("setup_type"):
            wins = (sub["t1_hit"].fillna(False).astype(bool) | sub["t2_hit"].fillna(False).astype(bool))
            stops = (sub["outcome"] == "STOP_HIT")
            resolved = wins | stops
            exit_vals = pd.to_numeric(sub["exit_R"], errors="coerce").dropna()
            setup_stats[str(setup)] = {
                "forward_tested": int(len(sub)),
                "t1_hits": int(sub["t1_hit"].fillna(False).astype(bool).sum()),
                "t2_hits": int(sub["t2_hit"].fillna(False).astype(bool).sum()),
                "stop_hits": int(stops.sum()),
                "time_exits": int((sub["outcome"] == "TIME_EXIT").sum()),
                "win_rate_all_pct": round4(wins.sum() / len(sub) * 100.0) if len(sub) else 0.0,
                "win_rate_vs_stop_pct": round4(wins[resolved].sum() / int(resolved.sum()) * 100.0) if int(resolved.sum()) else 0.0,
                "median_exit_R": round4(exit_vals.median()) if not exit_vals.empty else 0.0,
                "mean_exit_R": round4(exit_vals.mean()) if not exit_vals.empty else 0.0,
            }
    summary["win_rate_by_setup"] = setup_stats

    hold_stats: Dict[str, Any] = {}
    best_label = ""
    best_rank = (-999.0, -999.0, -999)
    for h in range(1, args.max_hold_days + 1):
        out_col = f"h{h}_outcome"
        r_col = f"h{h}_exit_R"
        t1_col = f"h{h}_t1_hit"
        t2_col = f"h{h}_t2_hit"
        stop_col = f"h{h}_stop_hit"
        if out_col not in forward or r_col not in forward:
            continue
        sub = forward[forward[out_col].notna()].copy()
        if sub.empty:
            continue
        wins = sub[t1_col].fillna(False).astype(bool) | sub[t2_col].fillna(False).astype(bool)
        stops = sub[stop_col].fillna(False).astype(bool) & (sub[out_col] == "STOP_HIT")
        vals = pd.to_numeric(sub[r_col], errors="coerce").dropna()
        mean_r = round4(vals.mean()) if not vals.empty else 0.0
        med_r = round4(vals.median()) if not vals.empty else 0.0
        win_rate = round4(wins.sum() / len(sub) * 100.0) if len(sub) else 0.0
        hold_stats[str(h)] = {
            "forward_tested": int(len(sub)),
            "t1_hits": int(sub[t1_col].fillna(False).astype(bool).sum()),
            "t2_hits": int(sub[t2_col].fillna(False).astype(bool).sum()),
            "stop_hits": int(stops.sum()),
            "win_rate_all_pct": win_rate,
            "median_exit_R": med_r,
            "mean_exit_R": mean_r,
        }
        rank = (mean_r, win_rate, int(len(sub)))
        if rank > best_rank:
            best_rank = rank
            best_label = f"{h} day" if h == 1 else f"{h} days"

    summary["hold_period_stats"] = hold_stats
    summary["best_hold_period"] = best_label

    return summary


def write_outputs(rows: List[Dict[str, Any]], summary: Dict[str, Any], args: argparse.Namespace) -> None:
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "swing_forward_backtest_latest.csv"
    json_path = out_dir / "swing_forward_backtest_latest.json"
    summary_path = out_dir / "swing_forward_backtest_summary.json"
    setup_path = out_dir / "swing_forward_backtest_by_setup.json"

    df = pd.DataFrame(rows)
    if df.empty:
        # Keep stable, inspectable output even when no candidates are found.
        df = pd.DataFrame(columns=[
            "symbol", "setup_type", "swing_status", "test_date_et",
            "entry_trigger", "stop_loss", "target_1", "target_2",
            "outcome", "exit_R", "mfe_R", "mae_R",
        ])
    df.to_csv(csv_path, index=False)
    json_path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    summary["outputs"] = {
        "csv": str(csv_path),
        "json": str(json_path),
        "summary": str(summary_path),
        "by_setup": str(setup_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    setup_path.write_text(json.dumps(summary.get("win_rate_by_setup", {}), indent=2, default=str), encoding="utf-8")

    if args.append_history:
        hist_path = out_dir / "swing_forward_backtest_history.jsonl"
        with hist_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary, default=str) + "\n")
        summary["outputs"]["history"] = str(hist_path)
        # Re-write summary so outputs includes history path.
        summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print(f"Saved: {csv_path}")
    print(f"Saved: {json_path}")
    print(f"Saved: {summary_path}")
    print(f"Saved: {setup_path}")


def run_backtest(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if args.source != "parquet":
        raise SystemExit("Only --source parquet is supported for historical forward backtesting.")
    if not args.data_root:
        raise SystemExit("--data-root is required.")
    data_root = Path(args.data_root)
    if not data_root.exists():
        raise FileNotFoundError(f"data root not found: {data_root}")

    scanner_args = scanner_namespace(args)
    earnings_map = ss.load_earnings_csv(args.earnings_csv)
    explicit_symbols = sorted(set(ss.parse_symbols_arg(args.symbols) + ss.load_symbols_from_file(args.symbols_file)))
    sources = ss.parquet_sources(data_root, explicit_symbols, args.limit_files)

    statuses = parse_csv_set(args.statuses)
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)

    rows: List[Dict[str, Any]] = []
    file_errors: Dict[str, str] = {}
    counters: Dict[str, Any] = {
        "files_seen": len(sources),
        "files_processed": 0,
        "days_scanned": 0,
        "historical_candidate_days_seen": 0,
        "candidates_built": 0,
        "skipped_no_future_regular_session": 0,
        "skipped_entry_not_touched": 0,
        "skipped_bad_plan": 0,
        "forward_tested": 0,
    }

    if args.progress:
        print(f"Parquet files: {len(sources)}", flush=True)

    for idx, (symbol, path) in enumerate(sources, start=1):
        if args.progress and (idx == 1 or idx % 25 == 0 or idx == len(sources)):
            print(f"[{idx}/{len(sources)}] {symbol} {Path(path).name}", flush=True)

        try:
            regular = load_parquet_intraday(path)
            if regular.empty:
                file_errors[symbol] = "empty parquet after regular-session normalization"
                continue

            daily = ss.daily_from_intraday(regular)
            if daily.empty:
                file_errors[symbol] = "no daily candles"
                continue
            daily = daily.sort_values("date_et").reset_index(drop=True)
            dates = list(daily["date_et"].astype(str))

            if len(dates) < args.min_warmup_days + 1:
                file_errors[symbol] = f"not enough daily candles: {len(dates)}"
                continue

            counters["files_processed"] += 1

            max_idx = len(dates) - 1  # last daily date has no future session inside this file
            for day_idx in range(args.min_warmup_days - 1, max_idx):
                test_date = str(dates[day_idx])
                if start_date and test_date < start_date:
                    continue
                if end_date and test_date > end_date:
                    continue

                future_bars, future_dates = future_sessions(regular, test_date, args.max_hold_days)
                if future_bars.empty or not future_dates:
                    counters["skipped_no_future_regular_session"] += 1
                    continue

                daily_until = daily.iloc[: day_idx + 1].copy().reset_index(drop=True)
                intraday_day = slice_intraday_date(regular, test_date)
                if intraday_day.empty:
                    continue

                counters["days_scanned"] += 1
                candidates, _notes = ss.scan_symbol(symbol, daily_until, intraday_day, scanner_args, earnings_map)
                if not candidates:
                    continue

                # Make sure duplicates from overlapping setup checks do not distort results.
                candidates = ss.dedupe_candidates(candidates)
                candidates = [
                    c for c in candidates
                    if str(c.swing_status).upper() in statuses and safe_float(c.score) >= args.min_score
                ]
                if args.max_candidates_per_symbol_day and len(candidates) > args.max_candidates_per_symbol_day:
                    candidates = candidates[: args.max_candidates_per_symbol_day]
                if not candidates:
                    continue

                counters["historical_candidate_days_seen"] += 1
                counters["candidates_built"] += len(candidates)

                for cand in candidates:
                    cdict = asdict(cand)
                    sim = simulate_candidate(
                        cdict,
                        future_bars,
                        future_dates,
                        args.max_hold_days,
                        args.same_bar_rule,
                    )
                    if sim.get("bad_plan"):
                        counters["skipped_bad_plan"] += 1
                    elif not sim.get("entry_touched"):
                        counters["skipped_entry_not_touched"] += 1
                    else:
                        counters["forward_tested"] += 1

                    rows.append(row_from_candidate(cand, str(path), test_date, future_dates, sim))

                if args.max_rows and len(rows) >= args.max_rows:
                    break

            del regular, daily
            gc.collect()

            if args.max_rows and len(rows) >= args.max_rows:
                break

        except Exception as exc:
            file_errors[symbol] = repr(exc)
            continue

    summary = summarize_results(rows, counters, args, file_errors)
    return rows, summary


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Universal historical forward backtester for swing_scanner.py. "
            "Research only; no production files are modified."
        )
    )
    p.add_argument("--source", choices=["parquet"], default="parquet",
                   help="Historical backtest source. v1.0.2 supports parquet.")
    p.add_argument("--data-root", required=True,
                   help="Parquet folder. No dataset path is hardcoded.")
    p.add_argument("--symbols", default=None,
                   help="Optional comma-separated symbol filter.")
    p.add_argument("--symbols-file", default=None,
                   help="Optional plain text/CSV universe file. First column must be symbol.")
    p.add_argument("--limit-files", type=int, default=None,
                   help="Testing only: limit parquet files.")
    p.add_argument("--start-date", default=None,
                   help="Optional first historical test date, YYYY-MM-DD.")
    p.add_argument("--end-date", default=None,
                   help="Optional last historical test date, YYYY-MM-DD.")
    p.add_argument("--min-warmup-days", type=int, default=220,
                   help="Minimum daily candles before a historical date can be tested.")
    p.add_argument("--max-hold-days", type=int, choices=[1, 2, 3], default=3,
                   help="Forward regular-session hold window.")
    p.add_argument("--same-bar-rule", choices=["stop_first", "target_first"], default="stop_first",
                   help="If stop and target both occur in the same bar before T1, default is conservative stop_first.")
    p.add_argument("--statuses", default="SWING_WATCH,SWING_READY,SWING_ACTIVE",
                   help="Comma-separated swing statuses to forward-test.")
    p.add_argument("--min-score", type=float, default=0.0,
                   help="Optional candidate score floor.")
    p.add_argument("--max-candidates-per-symbol-day", type=int, default=None,
                   help="Optional cap after scanner dedupe/ranking.")
    p.add_argument("--max-rows", type=int, default=None,
                   help="Development safety cap for generated result rows.")

    # Scanner-compatible filters. These must stay aligned with swing_scanner.py.
    p.add_argument("--mode", choices=["independent", "day-to-swing", "both"], default="independent",
                   help="Passed to swing_scanner.scan_symbol. independent is primary.")
    p.add_argument("--earnings-csv", default=None,
                   help="Optional earnings CSV with symbol/date columns.")
    p.add_argument("--min-price", type=float, default=10.0)
    p.add_argument("--max-price", type=float, default=200.0)
    p.add_argument("--min-avg-volume", type=float, default=1_000_000.0)
    p.add_argument("--max-atr-pct", type=float, default=15.0)

    p.add_argument("--output-dir", default="/opt/elite-scanner/swing_results",
                   help="Output folder. Default keeps swing research outputs together.")
    p.add_argument("--append-history", action="store_true",
                   help="Append summary to swing_forward_backtest_history.jsonl.")
    p.add_argument("--dry-run", action="store_true",
                   help="Run and print summary but do not write output files.")
    p.add_argument("--progress", action="store_true",
                   help="Print simple progress lines for VPS runs.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    print("=== UNIVERSAL SWING FORWARD BACKTESTER ===")
    print(f"Version: {BACKTESTER_VERSION}")
    print(f"Scanner version: {getattr(ss, 'SCANNER_VERSION', '')}")
    print(f"Source: {args.source}")
    print(f"Data root: {args.data_root}")
    print("Mode: research/backtest only. No production files modified.")

    rows, summary = run_backtest(args)

    if args.dry_run:
        print("DRY RUN: no files written.")
    else:
        write_outputs(rows, summary, args)

    print("Days scanned:", summary.get("days_scanned", 0))
    print("Historical candidate days seen:", summary.get("historical_candidate_days_seen", 0))
    print("Candidates built:", summary.get("candidates_built", 0))
    print("Forward tested:", summary.get("forward_tested", 0))
    print("Skipped entry not touched:", summary.get("skipped_entry_not_touched", 0))
    print("T1 hits:", summary.get("t1_hits", 0))
    print("T2 hits:", summary.get("t2_hits", 0))
    print("Stop hits:", summary.get("stop_hits", 0))
    print("Time exits:", summary.get("time_exits", 0))
    print("Median exit_R:", summary.get("median_exit_R", 0.0))
    print("Best hold period:", summary.get("best_hold_period", ""))
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
