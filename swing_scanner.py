#!/usr/bin/env python3
"""
Swing Scanner v1.0.0
Separate 1-3 day swing scanner for Elite Scanner.

Purpose:
- Does NOT modify intraday signal_engine.py behavior.
- Does NOT create day-trade Active signals.
- Scans historical/live 1-minute parquet files and produces Swing Watch/Ready/Active candidates.
- Supports independent swing setups and strict day-to-swing promotion logic.

Inputs expected in parquet:
    symbol, timestamp_utc, date_et, time_et, is_regular_session,
    open, high, low, close, volume, optional bar_vwap

Default data root:
    /opt/strategy-discovery/data/raw_1min

Outputs:
    swing_results/swing_candidates_latest.csv
    swing_results/swing_candidates_latest.json
    swing_results/swing_scanner_summary.json

No production files modified.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


SWING_SCANNER_VERSION = "swing_scanner_v1.0.0"

DEFAULT_DATA_ROOT = Path("/opt/strategy-discovery/data/raw_1min")
DEFAULT_OUTPUT_DIR = Path("/opt/elite-scanner/swing_results")
DEFAULT_INTRADAY_RESULTS = Path("/opt/elite-scanner/historical_sim_results/historical_forward_signals.csv")


# -----------------------------
# Configuration
# -----------------------------

MIN_PRICE = 10.0
MAX_PRICE = 200.0
MIN_AVG_DAILY_VOLUME = 1_000_000
IDEAL_ATR_MIN_PCT = 1.0
IDEAL_ATR_MAX_PCT = 6.0
ATR_REDUCED_SIZE_MAX_PCT = 10.0
ATR_EXCELLENT_ONLY_MAX_PCT = 15.0

WATCH_MIN_SCORE = 65.0
READY_MIN_SCORE = 75.0
ACTIVE_MIN_SCORE = 80.0

MIN_SWING_RR_READY = 1.5
MIN_SWING_RR_ACTIVE = 1.5

EARNINGS_BLOCK_DAYS = 3
MAX_HOLD_DAYS_DEFAULT = 3

# Conservative position sizing tiers based on ATR risk.
ATR_SIZE_TIERS = [
    (0.0, 1.0, "LOW_PRIORITY_TOO_SLOW", 0.50),
    (1.0, 6.0, "IDEAL", 1.00),
    (6.0, 10.0, "REDUCED_SIZE", 0.50),
    (10.0, 15.0, "EXCELLENT_ONLY", 0.25),
    (15.0, 999.0, "MANUAL_REVIEW_OR_REJECT", 0.00),
]


SETUP_DAILY_BREAKOUT = "DAILY_BREAKOUT_CONTINUATION"
SETUP_PULLBACK_SUPPORT = "SWING_PULLBACK_SUPPORT_HOLD"
SETUP_GAP_HOLD = "GAP_HOLD_SWING"
SETUP_DAY_TO_SWING = "DAY_TO_SWING_PROMOTION"

STATUS_REJECTED = "REJECTED"
STATUS_SWING_WATCH = "SWING_WATCH"
STATUS_SWING_READY = "SWING_READY"
STATUS_SWING_ACTIVE = "SWING_ACTIVE"


# -----------------------------
# Data classes
# -----------------------------

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
    blockers: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class SymbolFeatures:
    symbol: str
    valid: bool
    reason: str

    latest_date: str = ""
    latest_time: str = ""
    latest_close: float = math.nan
    latest_open: float = math.nan
    latest_high: float = math.nan
    latest_low: float = math.nan
    latest_volume: float = math.nan

    daily: Optional[pd.DataFrame] = None
    intraday: Optional[pd.DataFrame] = None
    last_day_bars: Optional[pd.DataFrame] = None

    sma20: float = math.nan
    sma50: float = math.nan
    sma200: float = math.nan
    ema20: float = math.nan
    atr14: float = math.nan
    atr_pct: float = math.nan
    rsi14: float = math.nan
    avg_volume20: float = math.nan
    rel_volume: float = math.nan

    close_location_pct: float = math.nan
    gap_pct: float = math.nan
    day_vwap: float = math.nan
    close_above_vwap: bool = False
    opening_range_high: float = math.nan
    opening_range_low: float = math.nan
    late_fade_pct: float = math.nan
    panic_selling: bool = False

    ret_5d: float = math.nan
    ret_20d: float = math.nan
    rs_score: float = 0.0

    earnings_risk: str = "UNKNOWN"
    earnings_date: str = ""


# -----------------------------
# Utility functions
# -----------------------------

def safe_float(v: Any, default: float = math.nan) -> float:
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def pct(a: float, b: float) -> float:
    if not b or math.isnan(a) or math.isnan(b):
        return math.nan
    return (a - b) / b * 100.0


def clamp(x: float, lo: float, hi: float) -> float:
    if math.isnan(x):
        return lo
    return max(lo, min(hi, x))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_symbol_from_file(path: Path) -> str:
    name = path.name
    for suffix in ["_1Min.parquet", "_1min.parquet", ".parquet"]:
        if name.endswith(suffix):
            return name[: -len(suffix)].upper()
    return path.stem.upper()


def score_linear(value: float, low: float, high: float, max_score: float) -> float:
    if math.isnan(value):
        return 0.0
    if value <= low:
        return 0.0
    if value >= high:
        return max_score
    return (value - low) / (high - low) * max_score


def score_band(value: float, ideal_low: float, ideal_high: float, hard_low: float, hard_high: float, max_score: float) -> float:
    if math.isnan(value):
        return 0.0
    if ideal_low <= value <= ideal_high:
        return max_score
    if hard_low < value < ideal_low:
        return (value - hard_low) / max(ideal_low - hard_low, 1e-9) * max_score
    if ideal_high < value < hard_high:
        return (hard_high - value) / max(hard_high - ideal_high, 1e-9) * max_score
    return 0.0


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    close = pd.to_numeric(series, errors="coerce")
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def calculate_atr(daily: pd.DataFrame, period: int = 14) -> pd.Series:
    high = pd.to_numeric(daily["high"], errors="coerce")
    low = pd.to_numeric(daily["low"], errors="coerce")
    close = pd.to_numeric(daily["close"], errors="coerce")
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=max(3, period // 2)).mean()


def weighted_vwap(df: pd.DataFrame) -> float:
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    if "bar_vwap" in df.columns:
        price = pd.to_numeric(df["bar_vwap"], errors="coerce")
        price = price.fillna(pd.to_numeric(df["close"], errors="coerce"))
    else:
        price = (
            pd.to_numeric(df["high"], errors="coerce")
            + pd.to_numeric(df["low"], errors="coerce")
            + pd.to_numeric(df["close"], errors="coerce")
        ) / 3.0
    denom = vol.sum()
    if denom <= 0:
        return safe_float(df["close"].iloc[-1])
    return safe_float((price * vol).sum() / denom)


def load_earnings_calendar(path: Optional[Path]) -> Dict[str, str]:
    if not path or not path.exists():
        return {}
    try:
        rows = list(csv.DictReader(path.open("r", encoding="utf-8", errors="ignore")))
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for r in rows:
        sym = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
        dt = str(r.get("earnings_date") or r.get("date") or r.get("report_date") or "").strip()
        if sym and dt:
            out[sym] = dt
    return out


def earnings_risk_for_symbol(symbol: str, latest_date: str, earnings: Dict[str, str]) -> Tuple[str, str]:
    dt_text = earnings.get(symbol.upper(), "")
    if not dt_text:
        return "UNKNOWN", ""
    try:
        e_date = pd.to_datetime(dt_text).date()
        ref = pd.to_datetime(latest_date).date()
        delta = (e_date - ref).days
        if 0 <= delta <= EARNINGS_BLOCK_DAYS:
            return "BLOCK_FUTURE_EARNINGS", str(e_date)
        if delta < 0:
            return "POST_EARNINGS", str(e_date)
        return "CLEAR", str(e_date)
    except Exception:
        return "UNKNOWN", dt_text


def atr_tier(atr_pct: float) -> Tuple[str, float]:
    for lo, hi, label, size_mult in ATR_SIZE_TIERS:
        if lo <= atr_pct < hi:
            return label, size_mult
    return "UNKNOWN", 0.0


def parse_parquet(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if df.empty:
        return df

    # Normalize expected columns.
    lower_map = {c.lower(): c for c in df.columns}
    rename = {}
    for want in ["open", "high", "low", "close", "volume", "symbol", "timestamp_utc", "date_et", "time_et", "is_regular_session", "bar_vwap"]:
        if want not in df.columns and want.lower() in lower_map:
            rename[lower_map[want.lower()]] = want
    if rename:
        df = df.rename(columns=rename)

    if "timestamp_utc" in df.columns:
        df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
        df["dt_et"] = df["timestamp_utc"].dt.tz_convert("America/New_York")
        if "date_et" not in df.columns:
            df["date_et"] = df["dt_et"].dt.date.astype(str)
        if "time_et" not in df.columns:
            df["time_et"] = df["dt_et"].dt.strftime("%H:%M:%S")
    else:
        # Fallback if date_et/time_et exist.
        if "date_et" in df.columns and "time_et" in df.columns:
            dt = pd.to_datetime(df["date_et"].astype(str) + " " + df["time_et"].astype(str), errors="coerce")
            df["dt_et"] = dt.dt.tz_localize("America/New_York", nonexistent="shift_forward", ambiguous="NaT")
        else:
            raise ValueError("Missing timestamp_utc or date_et/time_et")

    if "is_regular_session" not in df.columns:
        # Use time to infer regular session.
        t = df["dt_et"].dt.strftime("%H:%M")
        df["is_regular_session"] = (t >= "09:30") & (t <= "16:00")

    for c in ["open", "high", "low", "close", "volume"]:
        if c not in df.columns:
            raise ValueError(f"Missing required column: {c}")
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["dt_et", "open", "high", "low", "close"]).sort_values("dt_et").reset_index(drop=True)
    return df


def aggregate_daily(regular: pd.DataFrame) -> pd.DataFrame:
    if regular.empty:
        return pd.DataFrame()
    g = regular.groupby("date_et", sort=True)
    daily = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
        "bars": g["close"].count(),
    })
    daily.index = daily.index.astype(str)
    daily["vwap"] = g.apply(weighted_vwap)
    daily = daily.reset_index().rename(columns={"date_et": "date_et"})
    daily["sma20"] = daily["close"].rolling(20, min_periods=5).mean()
    daily["sma50"] = daily["close"].rolling(50, min_periods=15).mean()
    daily["sma200"] = daily["close"].rolling(200, min_periods=50).mean()
    daily["ema20"] = daily["close"].ewm(span=20, min_periods=5, adjust=False).mean()
    daily["atr14"] = calculate_atr(daily, 14)
    daily["rsi14"] = calculate_rsi(daily["close"], 14)
    daily["avg_volume20"] = daily["volume"].rolling(20, min_periods=5).mean()
    return daily


def build_features(path: Path, earnings: Dict[str, str], lookback_days: int) -> SymbolFeatures:
    symbol = normalize_symbol_from_file(path)
    try:
        df = parse_parquet(path)
        if df.empty:
            return SymbolFeatures(symbol=symbol, valid=False, reason="empty parquet")

        regular = df[df["is_regular_session"] == True].copy()
        if regular.empty:
            return SymbolFeatures(symbol=symbol, valid=False, reason="no regular-session bars")

        daily = aggregate_daily(regular)
        if len(daily) < 60:
            return SymbolFeatures(symbol=symbol, valid=False, reason=f"not enough daily bars: {len(daily)}")

        if lookback_days > 0 and len(daily) > lookback_days:
            # Keep enough prior history for SMA/ATR by not trimming the main daily table.
            pass

        last = daily.iloc[-1]
        prev = daily.iloc[-2] if len(daily) >= 2 else last
        latest_date = str(last["date_et"])
        last_day = regular[regular["date_et"].astype(str) == latest_date].copy()

        if last_day.empty:
            return SymbolFeatures(symbol=symbol, valid=False, reason="missing last day bars")

        latest_time = str(last_day["time_et"].iloc[-1]) if "time_et" in last_day.columns else str(last_day["dt_et"].iloc[-1])
        close = safe_float(last["close"])
        day_open = safe_float(last["open"])
        day_high = safe_float(last["high"])
        day_low = safe_float(last["low"])
        day_vol = safe_float(last["volume"])

        sma20 = safe_float(last.get("sma20"))
        sma50 = safe_float(last.get("sma50"))
        sma200 = safe_float(last.get("sma200"))
        ema20 = safe_float(last.get("ema20"))
        atr14 = safe_float(last.get("atr14"))
        atr_pct_val = atr14 / close * 100.0 if close > 0 and not math.isnan(atr14) else math.nan
        rsi14 = safe_float(last.get("rsi14"), 50.0)
        avg_vol20 = safe_float(last.get("avg_volume20"))
        rel_vol = day_vol / avg_vol20 if avg_vol20 and avg_vol20 > 0 else math.nan

        close_loc = ((close - day_low) / max(day_high - day_low, 1e-9)) * 100.0
        gap = pct(day_open, safe_float(prev["close"]))
        day_vwap = safe_float(last.get("vwap"), weighted_vwap(last_day))
        close_above_vwap = close >= day_vwap if not math.isnan(day_vwap) else False

        first30 = last_day.head(30)
        orh = safe_float(first30["high"].max())
        orl = safe_float(first30["low"].min())

        late = last_day.tail(30)
        late_high = safe_float(late["high"].max())
        late_close = safe_float(late["close"].iloc[-1])
        late_fade = pct(late_close, late_high) if late_high > 0 else math.nan

        # Panic selling = high volume + bearish price action + VWAP/support loss + bearish late close.
        red_day = close < day_open
        close_below_support = (not close_above_vwap) or (not math.isnan(ema20) and close < ema20)
        late_bearish = close_loc < 35.0 and (not math.isnan(late_fade) and late_fade <= -1.0)
        panic = bool((rel_vol >= 2.0 if not math.isnan(rel_vol) else False) and red_day and close_below_support and late_bearish)

        # Returns for relative strength cross-sectional scoring later.
        ret5 = pct(close, safe_float(daily["close"].iloc[-6])) if len(daily) > 6 else math.nan
        ret20 = pct(close, safe_float(daily["close"].iloc[-21])) if len(daily) > 21 else math.nan

        erisk, edate = earnings_risk_for_symbol(symbol, latest_date, earnings)

        return SymbolFeatures(
            symbol=symbol,
            valid=True,
            reason="ok",
            latest_date=latest_date,
            latest_time=latest_time,
            latest_close=close,
            latest_open=day_open,
            latest_high=day_high,
            latest_low=day_low,
            latest_volume=day_vol,
            daily=daily,
            intraday=regular,
            last_day_bars=last_day,
            sma20=sma20,
            sma50=sma50,
            sma200=sma200,
            ema20=ema20,
            atr14=atr14,
            atr_pct=atr_pct_val,
            rsi14=rsi14,
            avg_volume20=avg_vol20,
            rel_volume=rel_vol,
            close_location_pct=close_loc,
            gap_pct=gap,
            day_vwap=day_vwap,
            close_above_vwap=close_above_vwap,
            opening_range_high=orh,
            opening_range_low=orl,
            late_fade_pct=late_fade,
            panic_selling=panic,
            ret_5d=ret5,
            ret_20d=ret20,
            earnings_risk=erisk,
            earnings_date=edate,
        )

    except Exception as e:
        return SymbolFeatures(symbol=symbol, valid=False, reason=f"read/feature error: {e}")


def assign_relative_strength_scores(features: List[SymbolFeatures]) -> None:
    vals = []
    for f in features:
        if f.valid:
            # Blend 5d and 20d relative performance; penalize missing values.
            val = 0.6 * (f.ret_5d if not math.isnan(f.ret_5d) else 0.0) + 0.4 * (f.ret_20d if not math.isnan(f.ret_20d) else 0.0)
            vals.append((f.symbol, val))
    if not vals:
        return
    series = pd.Series({s: v for s, v in vals}).rank(pct=True) * 100.0
    for f in features:
        if f.valid and f.symbol in series:
            f.rs_score = safe_float(series[f.symbol], 50.0)


# -----------------------------
# Hard blockers and scoring
# -----------------------------

def hard_blockers_common(f: SymbolFeatures) -> List[str]:
    blockers: List[str] = []
    price = f.latest_close

    if price < MIN_PRICE or price > MAX_PRICE:
        blockers.append(f"price_outside_{MIN_PRICE:g}_{MAX_PRICE:g}")

    if math.isnan(f.avg_volume20) or f.avg_volume20 < MIN_AVG_DAILY_VOLUME:
        blockers.append("avg_volume_below_1m")

    # Hard reject only if below BOTH SMA50 and SMA200.
    below50 = not math.isnan(f.sma50) and price < f.sma50
    below200 = not math.isnan(f.sma200) and price < f.sma200
    if below50 and below200:
        blockers.append("below_both_sma50_and_sma200")

    tier, _ = atr_tier(f.atr_pct)
    if tier == "MANUAL_REVIEW_OR_REJECT":
        blockers.append("atr_over_15_manual_review")

    if f.earnings_risk == "BLOCK_FUTURE_EARNINGS":
        blockers.append("earnings_within_next_3_days")

    if f.panic_selling:
        blockers.append("panic_selling")

    return blockers


def score_daily_trend(f: SymbolFeatures, setup_type: str) -> float:
    # Normal continuation: daily trend matters more.
    max_score = 30.0 if setup_type in {SETUP_DAILY_BREAKOUT, SETUP_PULLBACK_SUPPORT} else 24.0

    score = 0.0
    price = f.latest_close
    if not math.isnan(f.sma20) and price > f.sma20:
        score += max_score * 0.25
    if not math.isnan(f.sma50) and price > f.sma50:
        score += max_score * 0.25
    if not math.isnan(f.sma200) and price > f.sma200:
        score += max_score * 0.20
    if not math.isnan(f.sma20) and not math.isnan(f.sma50) and f.sma20 >= f.sma50:
        score += max_score * 0.15
    if f.rsi14 >= 50:
        score += max_score * 0.15
    return clamp(score, 0.0, max_score)


def score_intraday_structure(f: SymbolFeatures) -> float:
    # Use latest day 30m/1h structure; max 15.
    if f.last_day_bars is None or f.last_day_bars.empty:
        return 0.0

    bars = f.last_day_bars.copy()
    bars = bars.set_index("dt_et").sort_index()
    try:
        m30 = bars.resample("30min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    except Exception:
        return 0.0

    if len(m30) < 3:
        return 4.0

    score = 0.0
    last = m30.iloc[-1]
    prev = m30.iloc[-2]
    first = m30.iloc[0]
    if last["close"] > prev["close"]:
        score += 4.0
    if last["close"] > first["open"]:
        score += 3.0
    if f.close_above_vwap:
        score += 4.0
    if f.close_location_pct >= 60:
        score += 2.0
    if f.late_fade_pct > -1.0:
        score += 2.0
    return clamp(score, 0.0, 15.0)


def score_volume(f: SymbolFeatures) -> float:
    # Relative volume quality; max 15.
    rv = f.rel_volume
    if math.isnan(rv):
        return 0.0
    if rv >= 2.0:
        return 15.0
    if rv >= 1.5:
        return 12.0
    if rv >= 1.2:
        return 9.0
    if rv >= 1.0:
        return 6.0
    return 2.0


def score_support_stop(f: SymbolFeatures, stop: float, entry: float) -> float:
    # Max 15. Prefer stops below real support without insane distance.
    risk_pct = (entry - stop) / entry * 100.0 if entry > 0 and stop > 0 else math.nan
    if math.isnan(risk_pct) or risk_pct <= 0:
        return 0.0

    score = 0.0
    if f.close_above_vwap:
        score += 4.0
    if not math.isnan(f.ema20) and f.latest_close >= f.ema20:
        score += 3.0
    if f.latest_low > stop:
        score += 2.0

    # For swings, risk around 1-4% is usually workable; ATR tier adjusts size.
    if 1.0 <= risk_pct <= 4.0:
        score += 6.0
    elif 0.5 <= risk_pct < 1.0 or 4.0 < risk_pct <= 6.0:
        score += 3.0

    return clamp(score, 0.0, 15.0)


def score_target_room(rr: float) -> float:
    if math.isnan(rr):
        return 0.0
    if rr >= 2.5:
        return 10.0
    if rr >= 2.0:
        return 8.0
    if rr >= 1.5:
        return 6.0
    if rr >= 1.2:
        return 3.0
    return 0.0


def score_close_quality(f: SymbolFeatures) -> float:
    # Max 10.
    score = 0.0
    if f.close_location_pct >= 70:
        score += 4.0
    elif f.close_location_pct >= 55:
        score += 3.0
    elif f.close_location_pct >= 40:
        score += 1.5

    if f.close_above_vwap:
        score += 3.0

    if f.late_fade_pct >= -0.5:
        score += 3.0
    elif f.late_fade_pct >= -1.5:
        score += 1.0

    return clamp(score, 0.0, 10.0)


def gap_risk_label(f: SymbolFeatures) -> str:
    gp = abs(f.gap_pct) if not math.isnan(f.gap_pct) else 0.0
    tier, _ = atr_tier(f.atr_pct)
    if gp >= 12 or tier == "MANUAL_REVIEW_OR_REJECT":
        return "HIGH"
    if gp >= 8 or tier == "EXCELLENT_ONLY":
        return "ELEVATED"
    if gp >= 5 or tier == "REDUCED_SIZE":
        return "MODERATE"
    return "NORMAL"


# -----------------------------
# Resistance / target logic
# -----------------------------

def recent_resistance_levels(daily: pd.DataFrame, entry: float, lookback: int = 120) -> List[float]:
    if daily is None or daily.empty:
        return []
    d = daily.tail(lookback).copy()
    highs = pd.to_numeric(d["high"], errors="coerce").dropna().tolist()
    levels: List[float] = []

    for h in highs:
        if h > entry * 1.005:
            levels.append(float(h))

    # Add obvious round levels above entry.
    if entry > 0:
        increments = [0.5, 1.0, 2.5, 5.0]
        inc = 0.5 if entry < 20 else 1.0 if entry < 50 else 2.5 if entry < 100 else 5.0
        base = math.ceil(entry / inc) * inc
        for k in range(1, 8):
            lvl = base + inc * k
            if lvl > entry * 1.005:
                levels.append(float(lvl))

    uniq = sorted(set(round(x, 4) for x in levels if x > entry))
    return uniq


def pick_targets(f: SymbolFeatures, entry: float, stop: float, setup_type: str) -> Tuple[float, float, float, str]:
    risk = entry - stop
    if risk <= 0:
        return math.nan, math.nan, math.nan, "INVALID_RISK"

    levels = recent_resistance_levels(f.daily, entry, lookback=160)

    # T1 = first real resistance above entry, unless too close.
    meaningful_min = entry + risk * MIN_SWING_RR_READY
    usable = [lvl for lvl in levels if lvl >= meaningful_min]

    if usable:
        t1 = usable[0]
        t2 = usable[1] if len(usable) > 1 else max(t1 + risk, entry + 2.0 * risk)
        rr = (t1 - entry) / risk
        return float(t1), float(t2), float(rr), "REAL_DAILY_RESISTANCE"

    # For open-air daily/gap breakouts, measured move is allowed as real structure,
    # not fake VWAP extension.
    open_air_ok = setup_type in {SETUP_DAILY_BREAKOUT, SETUP_GAP_HOLD} and (
        f.latest_close >= f.latest_high * 0.98
        and f.close_location_pct >= 65
        and f.rel_volume >= 1.3
        and f.close_above_vwap
    )

    if open_air_ok:
        measured = max(f.atr14 * 1.5 if not math.isnan(f.atr14) else 0.0, risk * 1.5)
        t1 = entry + measured
        t2 = entry + measured * 1.6
        rr = (t1 - entry) / risk
        return float(t1), float(t2), float(rr), "OPEN_AIR_MEASURED_MOVE"

    # No trusted target: allow Watch, block Ready/Active.
    return entry + risk * 1.0, entry + risk * 1.5, 1.0, "NO_REAL_TARGET_WATCH_ONLY"


# -----------------------------
# Setup detection and plan builders
# -----------------------------

def is_daily_breakout_continuation(f: SymbolFeatures) -> bool:
    d = f.daily
    if d is None or len(d) < 30:
        return False
    prev_high = safe_float(d.iloc[-21:-1]["high"].max())
    close = f.latest_close
    if math.isnan(prev_high):
        return False
    return bool(
        close > prev_high * 1.002
        and f.close_location_pct >= 60
        and f.rel_volume >= 1.3
        and close > f.sma50
        and close > f.sma200
        and f.rsi14 >= 50
    )


def is_swing_pullback_support_hold(f: SymbolFeatures) -> bool:
    d = f.daily
    if d is None or len(d) < 30:
        return False
    close = f.latest_close
    low = f.latest_low
    if math.isnan(f.ema20) or math.isnan(f.sma50):
        return False

    # Uptrend but pullback held EMA20/SMA20/prior support.
    trend_ok = close > f.sma50 and (math.isnan(f.sma200) or close > f.sma200) and f.rsi14 >= 45
    held_ema20 = low <= f.ema20 * 1.015 and close >= f.ema20
    selling_faded = f.close_location_pct >= 45 and f.late_fade_pct > -1.5
    return bool(trend_ok and held_ema20 and selling_faded and f.close_above_vwap)


def is_gap_hold_swing(f: SymbolFeatures) -> bool:
    gp = f.gap_pct
    if math.isnan(gp):
        return False
    gap_ok = 2.0 <= gp <= 8.0
    held_or = f.latest_close >= f.opening_range_low if not math.isnan(f.opening_range_low) else True
    return bool(
        gap_ok
        and held_or
        and f.close_above_vwap
        and f.close_location_pct >= 55
        and f.rel_volume >= 1.3
        and f.late_fade_pct > -2.0
    )


def build_plan_for_setup(f: SymbolFeatures, setup_type: str) -> Tuple[float, float, float, float, float, str, str]:
    """Return entry, stop, target1, target2, rr, target_source, invalid_if."""
    close = f.latest_close
    atr = f.atr14 if not math.isnan(f.atr14) else close * 0.03
    buffer = max(atr * 0.15, close * 0.003)

    if setup_type == SETUP_DAILY_BREAKOUT:
        entry = max(f.latest_high * 1.001, close * 1.002)
        support = max(safe_float(f.daily.iloc[-2]["high"]) if f.daily is not None and len(f.daily) >= 2 else f.latest_low, f.opening_range_high if not math.isnan(f.opening_range_high) else f.latest_low)
        stop = min(f.latest_low - buffer, support - buffer)
        invalid_if = "Invalid if price loses breakout level / prior-day high with volume."

    elif setup_type == SETUP_PULLBACK_SUPPORT:
        entry = max(f.latest_high * 1.001, close * 1.001)
        support_candidates = [f.latest_low]
        if not math.isnan(f.ema20):
            support_candidates.append(f.ema20)
        if not math.isnan(f.day_vwap):
            support_candidates.append(f.day_vwap)
        support = min(support_candidates)
        stop = support - buffer
        invalid_if = "Invalid if pullback low / EMA20 / VWAP support breaks."

    elif setup_type == SETUP_GAP_HOLD:
        entry = max(f.latest_high * 1.001, f.opening_range_high * 1.001 if not math.isnan(f.opening_range_high) else close * 1.002)
        support = f.opening_range_low if not math.isnan(f.opening_range_low) else f.latest_low
        stop = min(support - buffer, f.day_vwap - buffer if not math.isnan(f.day_vwap) else support - buffer)
        invalid_if = "Invalid if opening-range low or VWAP support breaks."

    elif setup_type == SETUP_DAY_TO_SWING:
        entry = close
        support = max(f.day_vwap if not math.isnan(f.day_vwap) else f.latest_low, f.latest_low)
        stop = min(f.latest_low - buffer, support - buffer)
        invalid_if = "Invalid if EOD VWAP/support or entry hold fails next session."

    else:
        entry = close
        stop = f.latest_low - buffer
        invalid_if = "Invalid if support breaks."

    # Prevent absurd stop > entry or too tight stop.
    if stop >= entry:
        stop = entry - max(atr * 0.5, entry * 0.015)

    t1, t2, rr, source = pick_targets(f, entry, stop, setup_type)
    return entry, stop, t1, t2, rr, source, invalid_if


def build_candidate(f: SymbolFeatures, setup_type: str, mode: str = "independent") -> SwingCandidate:
    blockers = hard_blockers_common(f)
    warnings: List[str] = []

    tier, size_mult = atr_tier(f.atr_pct)
    if tier in {"REDUCED_SIZE", "EXCELLENT_ONLY"}:
        warnings.append(f"ATR tier {tier}; reduce size.")
    if f.earnings_risk == "UNKNOWN":
        warnings.append("Earnings risk unknown; manual check required.")
    if f.gap_risk if False else False:
        pass

    entry, stop, t1, t2, rr, target_source, invalid_if = build_plan_for_setup(f, setup_type)

    daily_score = score_daily_trend(f, setup_type)
    intraday_score = score_intraday_structure(f)
    rs_score = score_linear(f.rs_score, 40.0, 85.0, 15.0)
    volume_score = score_volume(f)
    support_score = score_support_stop(f, stop, entry)
    target_score = score_target_room(rr)
    close_score = score_close_quality(f)

    # Setup-specific weighting adjustment: gap setups use volume/close more.
    if setup_type == SETUP_GAP_HOLD:
        daily_score = min(daily_score, 24.0)
        volume_score = min(15.0, volume_score + 2.0)
        close_score = min(10.0, close_score + 1.0)

    total_score = daily_score + intraday_score + rs_score + volume_score + support_score + target_score + close_score
    confidence = clamp(total_score, 0.0, 100.0)

    # No real target means Watch only. Ready/Active blocked unless real/open-air target.
    no_real_target = target_source == "NO_REAL_TARGET_WATCH_ONLY"
    if no_real_target:
        warnings.append("No real swing target found; Watch only until structure improves.")

    if f.latest_close < f.latest_open and f.close_location_pct < 35:
        warnings.append("Weak daily close.")

    if blockers:
        status = STATUS_REJECTED
    elif confidence >= ACTIVE_MIN_SCORE and rr >= MIN_SWING_RR_ACTIVE and not no_real_target:
        # Active only if current/latest bar was during regular market and trigger is already crossed.
        # If scanner is run after-hours, treat as Ready, not Active.
        last_time = str(f.latest_time)
        regular_time = "09:30" <= last_time[:5] <= "16:00"
        if regular_time and f.latest_close >= entry:
            status = STATUS_SWING_ACTIVE
        else:
            status = STATUS_SWING_READY
    elif confidence >= READY_MIN_SCORE and rr >= MIN_SWING_RR_READY and not no_real_target:
        status = STATUS_SWING_READY
    elif confidence >= WATCH_MIN_SCORE:
        status = STATUS_SWING_WATCH
    else:
        status = STATUS_REJECTED

    gap_risk = gap_risk_label(f)
    if gap_risk in {"ELEVATED", "HIGH"}:
        warnings.append(f"Gap/ATR risk {gap_risk}; reduce size or skip.")

    suggested_risk_pct = round(1.0 * size_mult, 3)
    if gap_risk == "ELEVATED":
        suggested_risk_pct = min(suggested_risk_pct, 0.5)
    elif gap_risk == "HIGH":
        suggested_risk_pct = min(suggested_risk_pct, 0.25)

    reason = (
        f"{setup_type}: score={confidence:.1f}; "
        f"trend={daily_score:.1f}, 30m/1h={intraday_score:.1f}, RS={rs_score:.1f}, "
        f"vol={volume_score:.1f}, support={support_score:.1f}, target={target_score:.1f}, close={close_score:.1f}; "
        f"target_source={target_source}"
    )

    return SwingCandidate(
        symbol=f.symbol,
        setup_type=setup_type,
        swing_status=status,
        score=round(total_score, 2),
        confidence=round(confidence, 2),
        entry_trigger=round(entry, 4),
        stop_loss=round(stop, 4),
        target_1=round(t1, 4),
        target_2=round(t2, 4),
        reward_risk=round(rr, 3) if not math.isnan(rr) else math.nan,
        expected_hold_days=MAX_HOLD_DAYS_DEFAULT,
        suggested_risk_pct=suggested_risk_pct,
        close_price=round(f.latest_close, 4),
        close_time_et=str(f.latest_time),
        latest_date_et=str(f.latest_date),
        price=round(f.latest_close, 4),
        avg_volume_20d=round(f.avg_volume20, 0) if not math.isnan(f.avg_volume20) else math.nan,
        atr_pct=round(f.atr_pct, 3) if not math.isnan(f.atr_pct) else math.nan,
        atr_tier=tier,
        rsi_14=round(f.rsi14, 2) if not math.isnan(f.rsi14) else math.nan,
        sma20=round(f.sma20, 4) if not math.isnan(f.sma20) else math.nan,
        sma50=round(f.sma50, 4) if not math.isnan(f.sma50) else math.nan,
        sma200=round(f.sma200, 4) if not math.isnan(f.sma200) else math.nan,
        above_sma20=bool(f.latest_close > f.sma20) if not math.isnan(f.sma20) else False,
        above_sma50=bool(f.latest_close > f.sma50) if not math.isnan(f.sma50) else False,
        above_sma200=bool(f.latest_close > f.sma200) if not math.isnan(f.sma200) else False,
        daily_trend_score=round(daily_score, 2),
        intraday_structure_score=round(intraday_score, 2),
        relative_strength_score=round(rs_score, 2),
        volume_score=round(volume_score, 2),
        support_stop_score=round(support_score, 2),
        target_room_score=round(target_score, 2),
        close_quality_score=round(close_score, 2),
        rel_volume=round(f.rel_volume, 3) if not math.isnan(f.rel_volume) else math.nan,
        close_location_pct=round(f.close_location_pct, 2) if not math.isnan(f.close_location_pct) else math.nan,
        gap_pct=round(f.gap_pct, 3) if not math.isnan(f.gap_pct) else math.nan,
        gap_risk=gap_risk,
        earnings_risk=f.earnings_risk,
        vwap=round(f.day_vwap, 4) if not math.isnan(f.day_vwap) else math.nan,
        close_above_vwap=f.close_above_vwap,
        late_fade_pct=round(f.late_fade_pct, 3) if not math.isnan(f.late_fade_pct) else math.nan,
        panic_selling=f.panic_selling,
        reason=reason,
        invalid_if=invalid_if,
        blockers=blockers,
        warnings=warnings,
    )


def candidates_for_symbol(f: SymbolFeatures) -> List[SwingCandidate]:
    if not f.valid:
        return []

    setups: List[str] = []
    if is_daily_breakout_continuation(f):
        setups.append(SETUP_DAILY_BREAKOUT)
    if is_swing_pullback_support_hold(f):
        setups.append(SETUP_PULLBACK_SUPPORT)
    if is_gap_hold_swing(f):
        setups.append(SETUP_GAP_HOLD)

    cands = [build_candidate(f, s) for s in setups]

    # If no setup but still interesting, do not emit generic noise.
    return cands


# -----------------------------
# Day-to-swing promotion
# -----------------------------

def load_intraday_active_unresolved(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()

    needed = {"symbol", "final_status_after_active_replay", "outcome"}
    if not needed.issubset(set(df.columns)):
        return pd.DataFrame()

    return df[
        (df["final_status_after_active_replay"] == "ACTIVE_SIGNAL")
        & (df["outcome"] == "OPEN_AT_FORWARD_END")
    ].copy()


def passes_day_to_swing_eod(f: SymbolFeatures, signal_row: pd.Series) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    entry = safe_float(signal_row.get("entry_trigger"))
    if math.isnan(entry):
        entry = safe_float(signal_row.get("price_at_signal"), f.latest_close)

    if f.latest_close < entry * 0.99:
        reasons.append("close_below_entry_by_more_than_1pct")
    if not f.close_above_vwap:
        reasons.append("close_below_vwap")
    if f.close_location_pct < 25 and (not f.close_above_vwap or f.latest_close < entry):
        reasons.append("bottom_25pct_close_plus_weakness")
    if not math.isnan(f.late_fade_pct) and f.late_fade_pct <= -2.0:
        reasons.append("heavy_late_day_fade")
    if f.panic_selling:
        reasons.append("panic_selling")
    if f.earnings_risk == "BLOCK_FUTURE_EARNINGS":
        reasons.append("earnings_within_next_3_days")

    return len(reasons) == 0, reasons


def build_day_to_swing_candidates(
    features_by_symbol: Dict[str, SymbolFeatures],
    intraday_results_path: Path,
) -> List[SwingCandidate]:
    unresolved = load_intraday_active_unresolved(intraday_results_path)
    if unresolved.empty:
        return []

    out: List[SwingCandidate] = []
    # Latest row per symbol/date to avoid duplicates.
    for _, r in unresolved.tail(5000).iterrows():
        sym = str(r.get("symbol", "")).upper()
        f = features_by_symbol.get(sym)
        if not f or not f.valid:
            continue

        ok, reasons = passes_day_to_swing_eod(f, r)
        if not ok:
            continue

        cand = build_candidate(f, SETUP_DAY_TO_SWING, mode="promotion")
        cand.reason = "Day-to-swing promotion: unresolved intraday ACTIVE passed EOD validation. " + cand.reason
        cand.invalid_if = "Invalid if next session loses EOD VWAP/support or opens below stop."
        cand.warnings.append("Promoted from intraday Active; not a blind overnight hold.")
        out.append(cand)

    return out


# -----------------------------
# Main runner
# -----------------------------

def select_files(data_root: Path, limit_files: int = 0) -> List[Path]:
    files = sorted(data_root.rglob("*.parquet"))
    if limit_files and limit_files > 0:
        files = files[:limit_files]
    return files


def write_outputs(candidates: List[SwingCandidate], summary: Dict[str, Any], output_dir: Path, top: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = [asdict(c) for c in candidates]
    # Serialize lists as joined text for CSV.
    csv_rows: List[Dict[str, Any]] = []
    for r in rows:
        rr = dict(r)
        rr["blockers"] = "; ".join(rr.get("blockers") or [])
        rr["warnings"] = "; ".join(rr.get("warnings") or [])
        csv_rows.append(rr)

    csv_path = output_dir / "swing_candidates_latest.csv"
    json_path = output_dir / "swing_candidates_latest.json"
    summary_path = output_dir / "swing_scanner_summary.json"

    if csv_rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows[:top] if top > 0 else csv_rows)
    else:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            f.write("symbol,setup_type,swing_status,score,reason\n")

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(rows[:top] if top > 0 else rows, f, indent=2)

    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)


def build_summary(candidates: List[SwingCandidate], total_files: int, valid_features: int, rejected_features: int, output_dir: Path) -> Dict[str, Any]:
    from collections import Counter

    status_counts = Counter(c.swing_status for c in candidates)
    setup_counts = Counter(c.setup_type for c in candidates)
    blocker_counts: Counter[str] = Counter()
    warning_counts: Counter[str] = Counter()

    for c in candidates:
        blocker_counts.update(c.blockers)
        warning_counts.update(c.warnings)

    return {
        "version": SWING_SCANNER_VERSION,
        "run_time": now_iso(),
        "total_files": total_files,
        "valid_feature_rows": valid_features,
        "rejected_feature_rows": rejected_features,
        "total_candidates": len(candidates),
        "status_counts": dict(status_counts),
        "setup_counts": dict(setup_counts),
        "top_blockers": dict(blocker_counts.most_common(20)),
        "top_warnings": dict(warning_counts.most_common(20)),
        "outputs": {
            "csv": str(output_dir / "swing_candidates_latest.csv"),
            "json": str(output_dir / "swing_candidates_latest.json"),
            "summary": str(output_dir / "swing_scanner_summary.json"),
        },
        "note": "Separate Swing Scanner only. No intraday signals modified. No production files modified.",
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="1-3 Day Swing Scanner for Elite Scanner")
    p.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--earnings-csv", type=Path, default=None, help="Optional earnings calendar CSV with symbol,date columns")
    p.add_argument("--intraday-results", type=Path, default=DEFAULT_INTRADAY_RESULTS, help="Optional historical/day-trade results for day-to-swing promotion")
    p.add_argument("--mode", choices=["independent", "promotion", "both"], default="both")
    p.add_argument("--limit-files", type=int, default=0)
    p.add_argument("--lookback-days", type=int, default=260)
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    print("=== SWING SCANNER ===")
    print(f"Version: {SWING_SCANNER_VERSION}")
    print(f"Data root: {args.data_root}")
    print(f"Mode: {args.mode}")
    print("No production files modified.")

    files = select_files(args.data_root, args.limit_files)
    print(f"Parquet files: {len(files)}")
    if not files:
        print("No parquet files found.")
        return 2

    earnings = load_earnings_calendar(args.earnings_csv)

    features: List[SymbolFeatures] = []
    for idx, f in enumerate(files, 1):
        if idx == 1 or idx % 50 == 0 or idx == len(files):
            print(f"[{idx}/{len(files)}] {f.name}")
        feat = build_features(f, earnings, args.lookback_days)
        features.append(feat)

    assign_relative_strength_scores(features)
    features_by_symbol = {f.symbol: f for f in features if f.valid}

    candidates: List[SwingCandidate] = []

    if args.mode in {"independent", "both"}:
        for f in features:
            candidates.extend(candidates_for_symbol(f))

    if args.mode in {"promotion", "both"}:
        candidates.extend(build_day_to_swing_candidates(features_by_symbol, args.intraday_results))

    # Remove rejected candidates from final display unless they are useful for debugging.
    display = [c for c in candidates if c.swing_status != STATUS_REJECTED]

    # Sort: Active/Ready first, then score.
    status_rank = {STATUS_SWING_ACTIVE: 0, STATUS_SWING_READY: 1, STATUS_SWING_WATCH: 2, STATUS_REJECTED: 9}
    display.sort(key=lambda c: (status_rank.get(c.swing_status, 9), -c.score, c.symbol))

    summary = build_summary(
        display,
        total_files=len(files),
        valid_features=sum(1 for f in features if f.valid),
        rejected_features=sum(1 for f in features if not f.valid),
        output_dir=args.output_dir,
    )

    if args.dry_run:
        print("DRY RUN: no files written.")
    else:
        write_outputs(display, summary, args.output_dir, args.top)
        print(f"Saved: {args.output_dir / 'swing_candidates_latest.csv'}")
        print(f"Saved: {args.output_dir / 'swing_candidates_latest.json'}")
        print(f"Saved: {args.output_dir / 'swing_scanner_summary.json'}")

    print("Status counts:", summary["status_counts"])
    print("Setup counts:", summary["setup_counts"])
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
