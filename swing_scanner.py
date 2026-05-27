#!/usr/bin/env python3
"""
Universal 1-3 Day Swing Scanner
Version: swing_scanner_v1.1.0_universal

Purpose:
- Universal scanner. Not tied to the 300-ticker dataset.
- Scans whatever universe/data source is passed at runtime.
- Supports:
    1) parquet folder validation/live cache mode
    2) Alpaca live data mode, using a provided universe/symbol list
- Writes visualization/research output only.
- No production signal_engine changes.
- No broker execution.
"""

from __future__ import annotations

import argparse
import csv
import gc
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd


SCANNER_VERSION = "swing_scanner_v1.1.0_universal"

STATUS_RANK = {
    "SWING_ACTIVE": 3,
    "SWING_READY": 2,
    "SWING_WATCH": 1,
}

SETUP_PRIORITY = {
    "DAILY_BREAKOUT_CONTINUATION": 3,
    "SWING_PULLBACK_SUPPORT_HOLD": 2,
    "GAP_HOLD_SWING": 2,
    "DAY_TO_SWING_PROMOTION": 1,
}

OUT_COLUMNS = [
    "symbol",
    "setup_type",
    "swing_status",
    "score",
    "confidence",
    "entry_trigger",
    "stop_loss",
    "target_1",
    "target_2",
    "reward_risk",
    "expected_hold_days",
    "suggested_risk_pct",
    "close_price",
    "close_time_et",
    "latest_date_et",
    "price",
    "avg_volume_20d",
    "atr_pct",
    "atr_tier",
    "rsi_14",
    "sma20",
    "sma50",
    "sma200",
    "above_sma20",
    "above_sma50",
    "above_sma200",
    "daily_trend_score",
    "intraday_structure_score",
    "relative_strength_score",
    "volume_score",
    "support_stop_score",
    "target_room_score",
    "close_quality_score",
    "rel_volume",
    "close_location_pct",
    "gap_pct",
    "gap_risk",
    "earnings_risk",
    "vwap",
    "close_above_vwap",
    "late_fade_pct",
    "panic_selling",
    "reason",
    "invalid_if",
    "blockers",
    "warnings",
]


@dataclass
class SwingCandidate:
    symbol: str
    setup_type: str
    swing_status: str
    score: float
    confidence: float
    entry_trigger: float
    stop_loss: float
    target_1: float
    target_2: float
    reward_risk: float
    expected_hold_days: int
    suggested_risk_pct: float
    close_price: float
    close_time_et: str
    latest_date_et: str
    price: float
    avg_volume_20d: float
    atr_pct: float
    atr_tier: str
    rsi_14: float
    sma20: float
    sma50: float
    sma200: float
    above_sma20: bool
    above_sma50: bool
    above_sma200: bool
    daily_trend_score: float
    intraday_structure_score: float
    relative_strength_score: float
    volume_score: float
    support_stop_score: float
    target_room_score: float
    close_quality_score: float
    rel_volume: float
    close_location_pct: float
    gap_pct: float
    gap_risk: str
    earnings_risk: str
    vwap: float
    close_above_vwap: bool
    late_fade_pct: float
    panic_selling: bool
    reason: str
    invalid_if: str
    blockers: str
    warnings: str


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


def pct(a: float, b: float) -> float:
    b = safe_float(b)
    if b == 0:
        return 0.0
    return (safe_float(a) - b) / b * 100.0


def normalize_symbol_from_file(path: Path) -> str:
    name = path.stem
    for suffix in ["_1Min", "_1min", "_1MIN", "_minute", "_Minute"]:
        if name.endswith(suffix):
            return name[: -len(suffix)].upper()
    return name.upper()


def load_symbols_from_file(path: Optional[str]) -> List[str]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"symbols file not found: {path}")
    raw = p.read_text(errors="ignore").splitlines()
    symbols: List[str] = []
    for line in raw:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Accept CSV first column or plain text.
        first = line.split(",")[0].strip().upper()
        if first and first not in {"SYMBOL", "TICKER"}:
            symbols.append(first)
    return sorted(set(symbols))


def parse_symbols_arg(symbols: Optional[str]) -> List[str]:
    if not symbols:
        return []
    return sorted(set(s.strip().upper() for s in symbols.split(",") if s.strip()))


def infer_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "timestamp_utc" in out.columns:
        out["timestamp_utc"] = pd.to_datetime(out["timestamp_utc"], utc=True, errors="coerce")
        out["dt_et"] = out["timestamp_utc"].dt.tz_convert("America/New_York")
    elif "timestamp" in out.columns:
        out["timestamp_utc"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
        out["dt_et"] = out["timestamp_utc"].dt.tz_convert("America/New_York")
    elif "t" in out.columns:
        out["timestamp_utc"] = pd.to_datetime(out["t"], utc=True, errors="coerce")
        out["dt_et"] = out["timestamp_utc"].dt.tz_convert("America/New_York")
    elif "date" in out.columns:
        out["dt_et"] = pd.to_datetime(out["date"], errors="coerce")
    else:
        # Last fallback: use index if datelike.
        out["dt_et"] = pd.to_datetime(out.index, errors="coerce")

    if "date_et" not in out.columns:
        out["date_et"] = pd.to_datetime(out["dt_et"], errors="coerce").dt.strftime("%Y-%m-%d")
    else:
        out["date_et"] = out["date_et"].astype(str)

    if "is_regular_session" not in out.columns:
        # If we have ET timestamps, classify 09:30 <= t < 16:00.
        try:
            t = out["dt_et"].dt.time
            out["is_regular_session"] = (
                (t >= pd.to_datetime("09:30").time()) &
                (t < pd.to_datetime("16:00").time())
            )
        except Exception:
            out["is_regular_session"] = True

    return out


def standardize_ohlcv_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    mapping = {
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "vw": "vwap",
    }
    for old, new in mapping.items():
        if new not in out.columns and old in out.columns:
            out[new] = out[old]
    required = ["open", "high", "low", "close", "volume"]
    for col in required:
        if col not in out.columns:
            out[col] = 0.0
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0)
    return out


def daily_from_intraday(df: pd.DataFrame) -> pd.DataFrame:
    df = standardize_ohlcv_columns(infer_datetime_columns(df))
    regular = df[df["is_regular_session"] == True].copy()
    if regular.empty:
        regular = df.copy()
    regular = regular.dropna(subset=["date_et"])
    if regular.empty:
        return pd.DataFrame()
    grouped = regular.groupby("date_et", sort=True)
    daily = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index()
    daily["date_et"] = daily["date_et"].astype(str)
    return daily


def latest_regular_day_slice(df: pd.DataFrame) -> pd.DataFrame:
    df = standardize_ohlcv_columns(infer_datetime_columns(df))
    regular = df[df["is_regular_session"] == True].copy()
    if regular.empty:
        regular = df.copy()
    if regular.empty or "date_et" not in regular.columns:
        return pd.DataFrame()
    latest = str(regular["date_et"].dropna().max())
    day = regular[regular["date_et"].astype(str) == latest].copy()
    return day


def add_daily_indicators(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily.copy().sort_values("date_et").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["sma20"] = d["close"].rolling(20, min_periods=20).mean()
    d["sma50"] = d["close"].rolling(50, min_periods=50).mean()
    d["sma200"] = d["close"].rolling(200, min_periods=200).mean()

    prev_close = d["close"].shift(1)
    tr = pd.concat([
        (d["high"] - d["low"]).abs(),
        (d["high"] - prev_close).abs(),
        (d["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    d["atr14"] = tr.rolling(14, min_periods=14).mean()
    d["atr_pct"] = d["atr14"] / d["close"] * 100.0

    delta = d["close"].diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=14).mean()
    rs = gain / loss.replace(0, pd.NA)
    d["rsi_14"] = 100 - (100 / (1 + rs))
    d["avg_volume_20d"] = d["volume"].rolling(20, min_periods=10).mean()
    d["rel_volume"] = d["volume"] / d["avg_volume_20d"].replace(0, pd.NA)

    d["ret_5d_pct"] = (d["close"] / d["close"].shift(5) - 1) * 100
    d["ret_20d_pct"] = (d["close"] / d["close"].shift(20) - 1) * 100
    d["gap_pct"] = (d["open"] / d["close"].shift(1) - 1) * 100

    day_range = (d["high"] - d["low"]).replace(0, pd.NA)
    d["close_location_pct"] = (d["close"] - d["low"]) / day_range * 100.0

    return d


def calc_intraday_metrics(day: pd.DataFrame) -> Dict[str, float]:
    if day.empty:
        return {
            "vwap": 0.0,
            "close_above_vwap": False,
            "late_fade_pct": 0.0,
            "intraday_structure_score": 0.0,
            "panic_selling": False,
            "close_time_et": "",
        }
    day = standardize_ohlcv_columns(infer_datetime_columns(day)).sort_values("dt_et")
    close = safe_float(day["close"].iloc[-1])
    high = safe_float(day["high"].max())
    low = safe_float(day["low"].min())
    vol = pd.to_numeric(day["volume"], errors="coerce").fillna(0)

    typical = (day["high"] + day["low"] + day["close"]) / 3.0
    v = vol.sum()
    vwap = safe_float((typical * vol).sum() / v) if v > 0 else close
    late_fade_pct = pct(close, high) if high > 0 else 0.0  # negative from HOD

    # Simple structure score based on last third vs first third.
    n = len(day)
    if n >= 30:
        first = safe_float(day["close"].iloc[max(0, n // 3 - 1)])
        last = close
        mid = safe_float(day["close"].iloc[max(0, (2 * n) // 3 - 1)])
        if last > mid > first:
            structure = 15.0
        elif last > vwap:
            structure = 11.0
        elif last > first:
            structure = 8.0
        else:
            structure = 3.0
    else:
        structure = 8.0 if close >= vwap else 3.0

    # Panic selling = high late fade + below vwap + weak close location + late volume pressure.
    rng = high - low
    close_loc = (close - low) / rng * 100.0 if rng > 0 else 50.0
    panic = bool(close < vwap and late_fade_pct <= -2.0 and close_loc < 35.0)

    close_time = ""
    try:
        close_time = str(day["dt_et"].iloc[-1].strftime("%H:%M:%S"))
    except Exception:
        pass

    return {
        "vwap": round4(vwap),
        "close_above_vwap": bool(close >= vwap),
        "late_fade_pct": round4(late_fade_pct),
        "intraday_structure_score": round4(structure),
        "panic_selling": panic,
        "close_time_et": close_time,
    }


def atr_tier(atr_pct: float) -> str:
    atr_pct = safe_float(atr_pct)
    if atr_pct < 1.0:
        return "LOW"
    if atr_pct <= 6.0:
        return "IDEAL"
    if atr_pct <= 10.0:
        return "ELEVATED_REDUCED_SIZE"
    if atr_pct <= 15.0:
        return "HIGH_MANUAL_CAUTION"
    return "EXTREME_REJECT"


def gap_risk(gap_pct: float) -> str:
    g = abs(safe_float(gap_pct))
    if g <= 3:
        return "NORMAL"
    if g <= 8:
        return "ELEVATED"
    if g <= 12:
        return "HIGH"
    return "EXTREME"


def earnings_risk_for(symbol: str, latest_date: str, earnings_map: Dict[str, str]) -> Tuple[str, Optional[str]]:
    if not earnings_map:
        return "UNKNOWN", "Earnings risk unknown; manual check required."
    dt_s = earnings_map.get(symbol.upper())
    if not dt_s:
        return "CLEAR", None
    try:
        latest = pd.to_datetime(latest_date).date()
        ed = pd.to_datetime(dt_s).date()
        delta = (ed - latest).days
        if 0 <= delta <= 3:
            return "BLOCKED_UPCOMING_EARNINGS", f"Earnings within {delta} calendar days."
        if -3 <= delta < 0:
            return "POST_EARNINGS", None
        return "CLEAR", None
    except Exception:
        return "UNKNOWN", "Earnings date parse error; manual check required."


def load_earnings_csv(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"earnings CSV not found: {path}")
    out: Dict[str, str] = {}
    with p.open(newline="", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = (row.get("symbol") or row.get("ticker") or "").strip().upper()
            dt = (row.get("earnings_date") or row.get("date") or row.get("report_date") or "").strip()
            if sym and dt:
                out[sym] = dt
    return out


def recent_resistance_targets(d: pd.DataFrame, entry: float, risk: float) -> Tuple[float, float, str]:
    hist = d.iloc[:-1].tail(80).copy()
    levels: List[float] = []
    if not hist.empty:
        for window in [10, 20, 50, 80]:
            sub = hist.tail(window)
            if not sub.empty:
                levels.append(safe_float(sub["high"].max()))
        # Round-number liquidity above entry.
        if entry > 0:
            inc = 0.5 if entry < 50 else 1.0 if entry < 150 else 2.5
            next_round = math.ceil(entry / inc) * inc
            if next_round > entry:
                levels.append(next_round)
    levels = sorted(set(round4(x) for x in levels if safe_float(x) > entry * 1.002))
    if levels:
        t1 = levels[0]
        t2 = levels[1] if len(levels) > 1 else max(t1, entry + 2.0 * risk)
        return round4(t1), round4(t2), "REAL_DAILY_RESISTANCE"
    # Open air fallback is measured move, not fake VWAP target.
    t1 = entry + max(1.5 * risk, safe_float(d["atr14"].iloc[-1], 0.0))
    t2 = entry + max(2.5 * risk, 1.5 * safe_float(d["atr14"].iloc[-1], 0.0))
    return round4(t1), round4(t2), "OPEN_AIR_MEASURED_MOVE"


def score_common(latest: pd.Series, intraday: Dict[str, Any]) -> Dict[str, float]:
    close = safe_float(latest["close"])
    sma20 = safe_float(latest.get("sma20"))
    sma50 = safe_float(latest.get("sma50"))
    sma200 = safe_float(latest.get("sma200"))
    rsi = safe_float(latest.get("rsi_14"))
    relv = safe_float(latest.get("rel_volume"), 0.0)
    ret5 = safe_float(latest.get("ret_5d_pct"), 0.0)
    ret20 = safe_float(latest.get("ret_20d_pct"), 0.0)
    close_loc = safe_float(latest.get("close_location_pct"), 50.0)

    trend = 0.0
    if close > sma200 > 0:
        trend += 10
    if close > sma50 > 0:
        trend += 8
    if close > sma20 > 0:
        trend += 6
    if sma20 > sma50 > 0:
        trend += 4
    if 45 <= rsi <= 70:
        trend += 2
    trend = min(30.0, trend)

    rs = 0.0
    if ret5 > 0:
        rs += 5
    if ret20 > 0:
        rs += 6
    if ret5 > 2:
        rs += 2
    if ret20 > 5:
        rs += 2
    rs = min(15.0, rs)

    vol = 0.0
    if relv >= 1.0:
        vol += 6
    if relv >= 1.2:
        vol += 3
    if relv >= 1.5:
        vol += 4
    if relv >= 2.0:
        vol += 2
    vol = min(15.0, vol)

    close_quality = 0.0
    if close_loc >= 50:
        close_quality += 4
    if close_loc >= 65:
        close_quality += 3
    if bool(intraday.get("close_above_vwap")):
        close_quality += 2
    if safe_float(intraday.get("late_fade_pct")) > -1.5:
        close_quality += 1
    close_quality = min(10.0, close_quality)

    return {
        "daily_trend_score": round4(trend),
        "intraday_structure_score": round4(safe_float(intraday.get("intraday_structure_score"), 0.0)),
        "relative_strength_score": round4(rs),
        "volume_score": round4(vol),
        "close_quality_score": round4(close_quality),
    }


def build_candidate(
    symbol: str,
    setup_type: str,
    status: str,
    latest: pd.Series,
    daily: pd.DataFrame,
    intraday: Dict[str, Any],
    scores: Dict[str, float],
    support_stop_score: float,
    target_room_score: float,
    entry: float,
    stop: float,
    target1: float,
    target2: float,
    target_source: str,
    warnings: List[str],
) -> Optional[SwingCandidate]:
    risk = entry - stop
    if entry <= 0 or stop <= 0 or risk <= 0:
        return None
    rr = (target1 - entry) / risk if target1 > entry else 0.0

    total = (
        scores["daily_trend_score"]
        + scores["intraday_structure_score"]
        + scores["relative_strength_score"]
        + scores["volume_score"]
        + support_stop_score
        + target_room_score
        + scores["close_quality_score"]
    )
    total = max(0.0, min(100.0, total))

    if total >= 80 and rr >= 1.2:
        status = "SWING_READY"
    elif total >= 65:
        status = "SWING_WATCH"
    else:
        return None

    atrp = safe_float(latest.get("atr_pct"))
    suggested_risk = 1.0
    if atrp > 10:
        suggested_risk = 0.35
    elif atrp > 6:
        suggested_risk = 0.5

    reason = (
        f"{setup_type}: score={total:.1f}; "
        f"trend={scores['daily_trend_score']:.1f}, "
        f"30m/1h={scores['intraday_structure_score']:.1f}, "
        f"RS={scores['relative_strength_score']:.1f}, "
        f"vol={scores['volume_score']:.1f}, "
        f"support={support_stop_score:.1f}, "
        f"target={target_room_score:.1f}, "
        f"close={scores['close_quality_score']:.1f}; "
        f"target_source={target_source}"
    )

    invalid = {
        "SWING_PULLBACK_SUPPORT_HOLD": "Invalid if pullback low / EMA20 / VWAP support breaks.",
        "DAILY_BREAKOUT_CONTINUATION": "Invalid if breakout level fails and price closes back inside prior range.",
        "GAP_HOLD_SWING": "Invalid if gap-day low / opening range support fails.",
        "DAY_TO_SWING_PROMOTION": "Invalid if EOD support, VWAP, or prior day low fails.",
    }.get(setup_type, "Invalid if support breaks.")

    return SwingCandidate(
        symbol=symbol,
        setup_type=setup_type,
        swing_status=status,
        score=round4(total),
        confidence=round4(total),
        entry_trigger=round4(entry),
        stop_loss=round4(stop),
        target_1=round4(target1),
        target_2=round4(target2),
        reward_risk=round4(rr),
        expected_hold_days=3,
        suggested_risk_pct=round4(suggested_risk),
        close_price=round4(latest["close"]),
        close_time_et=str(intraday.get("close_time_et", "")),
        latest_date_et=str(latest["date_et"]),
        price=round4(latest["close"]),
        avg_volume_20d=round4(latest.get("avg_volume_20d")),
        atr_pct=round4(atrp),
        atr_tier=atr_tier(atrp),
        rsi_14=round4(latest.get("rsi_14")),
        sma20=round4(latest.get("sma20")),
        sma50=round4(latest.get("sma50")),
        sma200=round4(latest.get("sma200")),
        above_sma20=bool(latest["close"] > safe_float(latest.get("sma20"))),
        above_sma50=bool(latest["close"] > safe_float(latest.get("sma50"))),
        above_sma200=bool(latest["close"] > safe_float(latest.get("sma200"))),
        daily_trend_score=round4(scores["daily_trend_score"]),
        intraday_structure_score=round4(scores["intraday_structure_score"]),
        relative_strength_score=round4(scores["relative_strength_score"]),
        volume_score=round4(scores["volume_score"]),
        support_stop_score=round4(support_stop_score),
        target_room_score=round4(target_room_score),
        close_quality_score=round4(scores["close_quality_score"]),
        rel_volume=round4(latest.get("rel_volume")),
        close_location_pct=round4(latest.get("close_location_pct")),
        gap_pct=round4(latest.get("gap_pct")),
        gap_risk=gap_risk(safe_float(latest.get("gap_pct"))),
        earnings_risk="",  # filled later
        vwap=round4(intraday.get("vwap")),
        close_above_vwap=bool(intraday.get("close_above_vwap")),
        late_fade_pct=round4(intraday.get("late_fade_pct")),
        panic_selling=bool(intraday.get("panic_selling")),
        reason=reason,
        invalid_if=invalid,
        blockers="",
        warnings="; ".join(warnings),
    )


def scan_symbol(
    symbol: str,
    daily_raw: pd.DataFrame,
    latest_intraday: pd.DataFrame,
    args: argparse.Namespace,
    earnings_map: Dict[str, str],
) -> Tuple[List[SwingCandidate], List[str]]:
    blockers: List[str] = []
    warnings: List[str] = []
    candidates: List[SwingCandidate] = []

    daily = add_daily_indicators(daily_raw)
    if daily.empty or len(daily) < 220:
        return [], ["Not enough daily history for SMA200/warmup."]

    latest = daily.iloc[-1]
    close = safe_float(latest["close"])
    avg_vol = safe_float(latest.get("avg_volume_20d"))
    atrp = safe_float(latest.get("atr_pct"))
    sma50 = safe_float(latest.get("sma50"))
    sma200 = safe_float(latest.get("sma200"))
    rsi = safe_float(latest.get("rsi_14"))
    relv = safe_float(latest.get("rel_volume"))

    # Universal hard blockers. These do not depend on dataset name.
    if close < args.min_price or close > args.max_price:
        blockers.append(f"Price outside range {args.min_price}-{args.max_price}.")
    if avg_vol < args.min_avg_volume:
        blockers.append(f"Avg volume below {args.min_avg_volume}.")
    if close < sma50 and close < sma200:
        blockers.append("Below BOTH SMA50 and SMA200.")
    if atrp > args.max_atr_pct:
        blockers.append(f"ATR% {atrp:.2f} above max {args.max_atr_pct}.")
    if pd.isna(latest.get("sma200")) or sma200 <= 0:
        blockers.append("SMA200 unavailable.")

    erisk, ewarn = earnings_risk_for(symbol, str(latest["date_et"]), earnings_map)
    if erisk == "BLOCKED_UPCOMING_EARNINGS":
        blockers.append(ewarn or "Upcoming earnings inside hold window.")
    elif ewarn:
        warnings.append(ewarn)

    intraday = calc_intraday_metrics(latest_intraday)
    if bool(intraday.get("panic_selling")):
        blockers.append("Panic selling / heavy late-day bearish close.")

    if blockers:
        return [], blockers + warnings

    scores = score_common(latest, intraday)
    atr = safe_float(latest.get("atr14"))
    if atr <= 0:
        return [], ["ATR unavailable."]

    high = safe_float(latest["high"])
    low = safe_float(latest["low"])
    open_ = safe_float(latest["open"])
    gap = safe_float(latest.get("gap_pct"))
    close_loc = safe_float(latest.get("close_location_pct"))

    # Common support stop.
    support_candidates = [low, safe_float(latest.get("sma20")), safe_float(intraday.get("vwap"))]
    support_candidates = [x for x in support_candidates if x and x > 0 and x < close]
    support = max(support_candidates) if support_candidates else low
    stop = max(0.01, support - 0.55 * atr)

    # Entry is next-day trigger above high or small confirmation above close.
    entry = max(high + 0.05 * atr, close * 1.003)
    target1, target2, target_source = recent_resistance_targets(daily, entry, entry - stop)
    rr = (target1 - entry) / (entry - stop) if entry > stop else 0

    support_stop_score = 15.0 if 0 < ((entry - stop) / close * 100) <= max(1.2 * atrp, 2.0) else 9.0
    target_room_score = 10.0 if rr >= 2 else 8.0 if rr >= 1.5 else 6.0 if rr >= 1.2 else 2.0

    prev20_high = safe_float(daily["high"].iloc[-21:-1].max()) if len(daily) >= 21 else 0.0

    # Setup 1: Daily breakout continuation
    breakout = (
        close > prev20_high * 1.001
        and close > safe_float(latest.get("sma50"))
        and close > safe_float(latest.get("sma200"))
        and relv >= 1.15
        and close_loc >= 60
        and rr >= 1.2
    )
    if breakout:
        cand = build_candidate(
            symbol, "DAILY_BREAKOUT_CONTINUATION", "SWING_READY", latest, daily, intraday,
            scores, support_stop_score, target_room_score, entry, stop, target1, target2,
            target_source, warnings
        )
        if cand:
            cand.earnings_risk = erisk
            candidates.append(cand)

    # Setup 2: Swing pullback support hold
    near_sma20 = abs(close - safe_float(latest.get("sma20"))) / close * 100 <= max(atrp * 0.8, 1.5) if close > 0 else False
    pullback = (
        close > sma50
        and close > sma200
        and 45 <= rsi <= 68
        and close_loc >= 50
        and (near_sma20 or bool(intraday.get("close_above_vwap")))
        and rr >= 1.2
    )
    if pullback:
        cand = build_candidate(
            symbol, "SWING_PULLBACK_SUPPORT_HOLD", "SWING_READY", latest, daily, intraday,
            scores, support_stop_score, target_room_score, entry, stop, target1, target2,
            target_source, warnings
        )
        if cand:
            cand.earnings_risk = erisk
            candidates.append(cand)

    # Setup 3: Gap hold swing
    gap_hold = (
        2.0 <= gap <= 8.0
        and close_loc >= 55
        and bool(intraday.get("close_above_vwap"))
        and safe_float(intraday.get("late_fade_pct")) > -2.5
        and rr >= 1.2
    )
    if gap_hold:
        gap_stop = max(0.01, min(open_, low) - 0.35 * atr)
        cand = build_candidate(
            symbol, "GAP_HOLD_SWING", "SWING_READY", latest, daily, intraday,
            scores, support_stop_score, target_room_score, entry, gap_stop, target1, target2,
            target_source, warnings
        )
        if cand:
            cand.earnings_risk = erisk
            candidates.append(cand)

    return candidates, warnings


def dedupe_candidates(candidates: Sequence[SwingCandidate]) -> List[SwingCandidate]:
    best: Dict[Tuple[str, str, str], SwingCandidate] = {}
    for c in candidates:
        key = (c.symbol, c.setup_type, c.latest_date_et)
        old = best.get(key)
        if old is None:
            best[key] = c
            continue
        old_rank = (
            STATUS_RANK.get(old.swing_status, 0),
            safe_float(old.score),
            safe_float(old.reward_risk),
            -len(old.blockers or ""),
            -len(old.warnings or ""),
        )
        new_rank = (
            STATUS_RANK.get(c.swing_status, 0),
            safe_float(c.score),
            safe_float(c.reward_risk),
            -len(c.blockers or ""),
            -len(c.warnings or ""),
        )
        if new_rank > old_rank:
            best[key] = c
    out = list(best.values())
    out.sort(key=lambda x: (
        STATUS_RANK.get(x.swing_status, 0),
        safe_float(x.score),
        safe_float(x.reward_risk),
        SETUP_PRIORITY.get(x.setup_type, 0),
    ), reverse=True)
    return out


def parquet_sources(data_root: Path, symbols: Sequence[str], limit: Optional[int]) -> List[Tuple[str, Path]]:
    files = sorted(data_root.rglob("*.parquet"))
    sym_filter = {s.upper() for s in symbols} if symbols else set()
    out: List[Tuple[str, Path]] = []
    for f in files:
        sym = normalize_symbol_from_file(f)
        if sym_filter and sym not in sym_filter:
            continue
        out.append((sym, f))
    if limit:
        out = out[:limit]
    return out


def read_parquet_symbol(path: Path) -> Tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_parquet(path)
    daily = daily_from_intraday(df)
    latest_day = latest_regular_day_slice(df)
    # Free memory held by raw intraday ASAP.
    del df
    gc.collect()
    return daily, latest_day


def alpaca_headers() -> Dict[str, str]:
    key = os.getenv("ALPACA_API_KEY") or os.getenv("ALPACA_KEY_ID")
    secret = os.getenv("ALPACA_SECRET_KEY") or os.getenv("ALPACA_API_SECRET")
    if not key or not secret:
        raise RuntimeError("Alpaca credentials missing. Set ALPACA_API_KEY/ALPACA_SECRET_KEY.")
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }


def alpaca_get_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=alpaca_headers())
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_alpaca_bars(symbol: str, timeframe: str, start: datetime, end: datetime, feed: str) -> pd.DataFrame:
    base = "https://data.alpaca.markets/v2/stocks/bars"
    params = {
        "symbols": symbol,
        "timeframe": timeframe,
        "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "adjustment": "raw",
        "feed": feed,
        "limit": "10000",
    }
    url = base + "?" + urllib.parse.urlencode(params)
    data = alpaca_get_json(url)
    bars = (data.get("bars") or {}).get(symbol, [])
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    # Alpaca returns t/o/h/l/c/v/vw/n.
    df = standardize_ohlcv_columns(infer_datetime_columns(df))
    return df


def read_alpaca_symbol(symbol: str, feed: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    now = datetime.now(timezone.utc)
    daily_start = now - timedelta(days=420)
    minute_start = now - timedelta(days=7)

    daily_bars = fetch_alpaca_bars(symbol, "1Day", daily_start, now, feed)
    if daily_bars.empty:
        return pd.DataFrame(), pd.DataFrame()
    daily = daily_from_intraday(daily_bars) if "date_et" not in daily_bars.columns else daily_bars.copy()
    if "date_et" not in daily.columns:
        daily = daily_from_intraday(daily_bars)
    else:
        # If Alpaca 1Day returned one row per day, standardize to daily columns.
        daily = standardize_ohlcv_columns(infer_datetime_columns(daily))
        daily = daily[["date_et", "open", "high", "low", "close", "volume"]].copy()

    minute_bars = fetch_alpaca_bars(symbol, "1Min", minute_start, now, feed)
    latest_day = latest_regular_day_slice(minute_bars) if not minute_bars.empty else pd.DataFrame()
    return daily, latest_day


def run_scan(args: argparse.Namespace) -> Tuple[List[SwingCandidate], Dict[str, Any]]:
    earnings_map = load_earnings_csv(args.earnings_csv)
    explicit_symbols = sorted(set(parse_symbols_arg(args.symbols) + load_symbols_from_file(args.symbols_file)))

    candidates: List[SwingCandidate] = []
    rejected: Dict[str, int] = {}
    warnings_count: Dict[str, int] = {}
    file_errors: Dict[str, str] = {}

    if args.source == "parquet":
        if not args.data_root:
            raise SystemExit("--data-root is required when --source parquet")
        root = Path(args.data_root)
        if not root.exists():
            raise FileNotFoundError(f"data root not found: {root}")
        sources = parquet_sources(root, explicit_symbols, args.limit_files)
        total = len(sources)
        print(f"Parquet files: {total}", flush=True)
        iterator = sources
    else:
        if not explicit_symbols:
            raise SystemExit("--symbols or --symbols-file is required when --source alpaca")
        syms = explicit_symbols[: args.limit_symbols] if args.limit_symbols else explicit_symbols
        total = len(syms)
        print(f"Alpaca symbols: {total}", flush=True)
        iterator = [(sym, None) for sym in syms]  # type: ignore[list-item]

    for idx, (symbol, obj) in enumerate(iterator, start=1):
        if idx == 1 or idx % 50 == 0 or idx == total:
            label = str(obj.name if isinstance(obj, Path) else symbol)
            print(f"[{idx}/{total}] {label}", flush=True)
        try:
            if args.source == "parquet":
                daily, latest_day = read_parquet_symbol(obj)  # type: ignore[arg-type]
            else:
                daily, latest_day = read_alpaca_symbol(symbol, args.alpaca_feed)

            cs, notes = scan_symbol(symbol, daily, latest_day, args, earnings_map)
            for n in notes:
                if "Earnings risk unknown" in n or "manual" in n:
                    warnings_count[n] = warnings_count.get(n, 0) + 1
                else:
                    rejected[n] = rejected.get(n, 0) + 1
            candidates.extend(cs)

            del daily, latest_day, cs
            if idx % 25 == 0:
                gc.collect()
        except Exception as e:
            file_errors[symbol] = repr(e)
            continue

    before = len(candidates)
    candidates = dedupe_candidates(candidates)
    after = len(candidates)

    summary: Dict[str, Any] = {
        "version": SCANNER_VERSION,
        "run_time": datetime.now().isoformat(timespec="seconds"),
        "source": args.source,
        "data_root": args.data_root or "",
        "symbols_file": args.symbols_file or "",
        "mode": args.mode,
        "total_inputs": total,
        "total_candidates_before_dedupe": before,
        "total_candidates": after,
        "dedupe_removed": before - after,
        "status_counts": {},
        "setup_counts": {},
        "top_blockers": dict(sorted(rejected.items(), key=lambda kv: kv[1], reverse=True)[:20]),
        "top_warnings": dict(sorted(warnings_count.items(), key=lambda kv: kv[1], reverse=True)[:20]),
        "file_errors": dict(list(file_errors.items())[:20]),
        "note": "Universal Swing Scanner. Dataset is selected only by CLI args. Research/visualization output only.",
    }

    for c in candidates:
        summary["status_counts"][c.swing_status] = summary["status_counts"].get(c.swing_status, 0) + 1
        summary["setup_counts"][c.setup_type] = summary["setup_counts"].get(c.setup_type, 0) + 1

    return candidates, summary


def write_outputs(candidates: Sequence[SwingCandidate], summary: Dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "swing_candidates_latest.csv"
    json_path = out_dir / "swing_candidates_latest.json"
    summary_path = out_dir / "swing_scanner_summary.json"

    rows = [asdict(c) for c in candidates]
    # Ensure stable columns.
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=OUT_COLUMNS)
    else:
        for col in OUT_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[OUT_COLUMNS]

    df.to_csv(csv_path, index=False)
    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    summary["outputs"] = {
        "csv": str(csv_path),
        "json": str(json_path),
        "summary": str(summary_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Saved: {csv_path}")
    print(f"Saved: {json_path}")
    print(f"Saved: {summary_path}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Universal 1-3 Day Swing Scanner")
    p.add_argument("--source", choices=["parquet", "alpaca"], default="parquet",
                   help="Data source. parquet=folder of parquet files. alpaca=Alpaca live API.")
    p.add_argument("--data-root", default=None,
                   help="Parquet folder. Required for --source parquet. No dataset is hardcoded.")
    p.add_argument("--symbols", default=None,
                   help="Comma-separated symbol list. Optional for parquet, required for alpaca unless --symbols-file is given.")
    p.add_argument("--symbols-file", default=None,
                   help="Plain text/CSV universe file. First column must be symbol.")
    p.add_argument("--limit-files", type=int, default=None,
                   help="Testing only: limit parquet files.")
    p.add_argument("--limit-symbols", type=int, default=None,
                   help="Testing only: limit Alpaca symbols.")
    p.add_argument("--mode", choices=["independent", "day-to-swing", "both"], default="independent",
                   help="Currently independent scanner is primary. day-to-swing reserved for EOD integration.")
    p.add_argument("--output-dir", default="/opt/elite-scanner/swing_results")
    p.add_argument("--earnings-csv", default=None)
    p.add_argument("--alpaca-feed", default=os.getenv("ALPACA_FEED", "sip"), choices=["sip", "iex", "otc"])
    p.add_argument("--min-price", type=float, default=10.0)
    p.add_argument("--max-price", type=float, default=200.0)
    p.add_argument("--min-avg-volume", type=float, default=1_000_000.0)
    p.add_argument("--max-atr-pct", type=float, default=15.0)
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    print("=== UNIVERSAL SWING SCANNER ===")
    print(f"Version: {SCANNER_VERSION}")
    print(f"Source: {args.source}")
    print(f"Data root: {args.data_root or ''}")
    print(f"Mode: {args.mode}")
    print("No production files modified.")

    candidates, summary = run_scan(args)

    if args.dry_run:
        print("DRY RUN: no files written.")
    else:
        write_outputs(candidates, summary, Path(args.output_dir))

    print("Status counts:", summary.get("status_counts", {}))
    print("Setup counts:", summary.get("setup_counts", {}))
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
