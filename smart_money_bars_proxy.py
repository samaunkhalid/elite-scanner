#!/usr/bin/env python3
"""
smart_money_bars_proxy.py
-------------------------

Phase 1 Smart Money Proxy Tracker for the Elite Scanner project.

Purpose:
  - Read the scanner's candidate universe.
  - Fetch Alpaca SIP 5-minute stock bars.
  - Calculate a conservative Smart Money Proxy score from bar behavior only.
  - Write:
      * smart_money_scores.json
      * smart_money_scores_history.csv

Phase 1 intentionally does NOT use:
  - WebSocket trade streams
  - OPRA/options data
  - dark-pool detection
  - sweep detection
  - block-trade claims

This script is designed to be placed in the project root next to:
  - elite_scanner.py
  - signal_engine.py
  - alpaca_feed.py

It reuses alpaca_feed.AlpacaFeed for credentials, feed selection, and HTTP access.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

try:
    from alpaca_feed import AlpacaFeed
except Exception as exc:  # pragma: no cover
    AlpacaFeed = None  # type: ignore
    _ALPACA_IMPORT_ERROR = exc
else:
    _ALPACA_IMPORT_ERROR = None


# =============================================================================
# Configuration
# =============================================================================

VERSION = "phase1_bars_proxy_v1.0"

PROJECT_DIR = Path(os.getenv("ELITE_PROJECT_DIR", Path(__file__).resolve().parent)).resolve()

RAW_WATCHLIST_FILE = PROJECT_DIR / os.getenv("SMART_MONEY_RAW_FILE", "elite_watchlist_raw.csv")
POTENTIAL_FILE = PROJECT_DIR / os.getenv("SMART_MONEY_POTENTIAL_FILE", "potential_movers.csv")
ACTIVE_FILE = PROJECT_DIR / os.getenv("SMART_MONEY_ACTIVE_FILE", "active_momentum.csv")

OUTPUT_FILE = PROJECT_DIR / os.getenv("SMART_MONEY_OUTPUT_FILE", "smart_money_scores.json")
HISTORY_FILE = PROJECT_DIR / os.getenv("SMART_MONEY_HISTORY_FILE", "smart_money_scores_history.csv")

DATA_FEED = os.getenv("ALPACA_DATA_FEED", "sip").strip().lower() or "sip"
TIMEFRAME = os.getenv("SMART_MONEY_TIMEFRAME", "5Min").strip() or "5Min"

SYMBOL_LIMIT = int(os.getenv("SMART_MONEY_SYMBOL_LIMIT", "100"))
CHUNK_SIZE = int(os.getenv("SMART_MONEY_CHUNK_SIZE", "75"))

# Regular-session scoring only. This matches the rule: no after-hours entries.
REGULAR_SESSION_ONLY = os.getenv("SMART_MONEY_REGULAR_SESSION_ONLY", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "y",
}

# Alpaca /v2/stocks/bars limit is 1..10000. Keep symbol chunks small enough that
# regular-session 5Min bars do not hit the endpoint limit.
ALPACA_BAR_LIMIT = int(os.getenv("SMART_MONEY_BAR_LIMIT", "10000"))

# Data staleness. Signal engine should later use this too.
MAX_AGE_SECONDS = int(os.getenv("SMART_MONEY_MAX_AGE_SECONDS", "180"))

# Scoring thresholds.
VOLUME_SURGE_STRONG = float(os.getenv("SMART_MONEY_VOLUME_SURGE_STRONG", "3.0"))
VOLUME_SURGE_MODERATE = float(os.getenv("SMART_MONEY_VOLUME_SURGE_MODERATE", "2.0"))

VWAP_TOUCH_DISTANCE_PCT = float(os.getenv("SMART_MONEY_VWAP_TOUCH_DISTANCE_PCT", "0.50"))
VWAP_BASIC_HOLD_DISTANCE_PCT = float(os.getenv("SMART_MONEY_VWAP_BASIC_HOLD_DISTANCE_PCT", "1.50"))

LARGE_CANDLE_STRONG = float(os.getenv("SMART_MONEY_LARGE_CANDLE_STRONG", "2.5"))
LARGE_CANDLE_MODERATE = float(os.getenv("SMART_MONEY_LARGE_CANDLE_MODERATE", "1.8"))

CLUSTER_BREAKOUT_VOLUME_RATIO = float(os.getenv("SMART_MONEY_CLUSTER_BREAKOUT_VOL_RATIO", "1.5"))

PHASE1_MAX_ADJUSTMENT = 5
PHASE1_MIN_ADJUSTMENT = -3


# =============================================================================
# Time helpers
# =============================================================================

def et_tz():
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo is unavailable. Use Python 3.9+.")
    return ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(et_tz())


def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=et_tz())
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def iso_et(dt: Optional[datetime] = None) -> str:
    dt = dt or now_et()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=et_tz())
    return dt.astimezone(et_tz()).isoformat(timespec="seconds")


def parse_ts_to_et(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    try:
        dt = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(dt):
            return None
        return dt.to_pydatetime().astimezone(et_tz())
    except Exception:
        return None


def regular_session_bounds(current_et: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    current_et = current_et or now_et()
    start = current_et.replace(hour=9, minute=30, second=0, microsecond=0)
    end = current_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return start, end


def request_start_end(current_et: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """
    Return the Alpaca request window.

    Phase 1 is regular-session only by default. During weekends/off-hours this still
    requests the current date window; if there are no bars, the output safely records
    no scored symbols rather than inventing stale signals.
    """
    current_et = current_et or now_et()

    if REGULAR_SESSION_ONLY:
        start, regular_end = regular_session_bounds(current_et)
        end = min(current_et, regular_end)
        # If run before the open, ask from 09:30 to now; Alpaca will usually return no bars.
        return start, end

    # Optional diagnostic mode: include premarket bars.
    start = current_et.replace(hour=4, minute=0, second=0, microsecond=0)
    end = current_et
    return start, end


def is_high_probability_time(current_et: Optional[datetime] = None) -> bool:
    """
    Time window aligned with the trading rules:
      - avoid the first 15 minutes after open
      - favor morning continuation and power hour
    """
    current_et = current_et or now_et()
    t = current_et.time()
    morning = t >= datetime.strptime("09:45", "%H:%M").time() and t <= datetime.strptime("10:45", "%H:%M").time()
    power_hour = t >= datetime.strptime("14:30", "%H:%M").time() and t <= datetime.strptime("15:45", "%H:%M").time()
    return bool(morning or power_hour)


# =============================================================================
# General helpers
# =============================================================================

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        x = int(float(value))
        return x
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def clean_symbol(value: Any) -> str:
    sym = str(value or "").strip().upper()
    if not sym:
        return ""

    # Exclude obvious non-stock tickers from Yahoo screeners.
    if any(ch in sym for ch in ["^", "=", "/"]):
        return ""

    # Alpaca supports class symbols such as BRK.B. Keep dots.
    if not re.fullmatch(r"[A-Z0-9.]{1,12}", sym):
        return ""

    return sym


def chunks(items: Sequence[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=False)
            f.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except Exception:
                pass


# =============================================================================
# Candidate loading
# =============================================================================

@dataclass
class Candidate:
    symbol: str
    scanner_score: float = 0.0
    setup_bucket: str = ""
    risk_category: str = ""
    source_file: str = ""


def _candidate_from_row(row: pd.Series, source_file: str) -> Optional[Candidate]:
    sym = clean_symbol(row.get("symbol"))
    if not sym:
        return None

    return Candidate(
        symbol=sym,
        scanner_score=safe_float(row.get("score"), 0.0),
        setup_bucket=str(row.get("setup_bucket", "") or "").strip(),
        risk_category=str(row.get("risk_category", "") or "").strip(),
        source_file=source_file,
    )


def load_candidates(limit: int = SYMBOL_LIMIT) -> List[Candidate]:
    """
    Prefer elite_watchlist_raw.csv so smart money can influence ranking across the
    broader universe. Fall back to potential_movers + active_momentum if raw file
    is unavailable.
    """
    candidates: Dict[str, Candidate] = {}

    if RAW_WATCHLIST_FILE.exists():
        try:
            df = pd.read_csv(RAW_WATCHLIST_FILE)
            if not df.empty and "symbol" in df.columns:
                if "score" in df.columns:
                    df["_sort_score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0)
                    df = df.sort_values("_sort_score", ascending=False)
                for _, row in df.iterrows():
                    cand = _candidate_from_row(row, RAW_WATCHLIST_FILE.name)
                    if cand and cand.symbol not in candidates:
                        candidates[cand.symbol] = cand
                    if len(candidates) >= limit:
                        break
        except Exception as exc:
            print(f"  ⚠️ Failed to read {RAW_WATCHLIST_FILE.name}: {exc}")

    # Fallback / supplemental source if raw file is absent or too small.
    if len(candidates) == 0:
        for path in [POTENTIAL_FILE, ACTIVE_FILE]:
            if not path.exists():
                continue
            try:
                df = pd.read_csv(path)
                if df.empty or "symbol" not in df.columns:
                    continue
                if "score" in df.columns:
                    df["_sort_score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0)
                    df = df.sort_values("_sort_score", ascending=False)
                for _, row in df.iterrows():
                    cand = _candidate_from_row(row, path.name)
                    if cand and cand.symbol not in candidates:
                        candidates[cand.symbol] = cand
                    if len(candidates) >= limit:
                        break
            except Exception as exc:
                print(f"  ⚠️ Failed to read {path.name}: {exc}")

    out = list(candidates.values())
    out.sort(key=lambda c: c.scanner_score, reverse=True)
    return out[:limit]


# =============================================================================
# Alpaca bars
# =============================================================================

def get_alpaca_feed() -> Any:
    if AlpacaFeed is None:
        raise RuntimeError(f"Could not import alpaca_feed.AlpacaFeed: {_ALPACA_IMPORT_ERROR}")

    feed = AlpacaFeed(feed=DATA_FEED)
    if not getattr(feed, "_has_credentials", lambda: False)():
        raise RuntimeError("Missing Alpaca credentials. Set ALPACA_API_KEY/ALPACA_SECRET_KEY or APCA_API_KEY_ID/APCA_API_SECRET_KEY.")
    return feed


def fetch_bars_for_symbols(feed: Any, symbols: List[str]) -> Dict[str, pd.DataFrame]:
    start_et, end_et = request_start_end()

    # Before the regular session opens, Phase 1 should not request an invalid
    # window such as 09:30 ET -> 08:15 ET. Return no scores and let the signal
    # engine fall back to original scanner behavior.
    if end_et <= start_et:
        print(
            "  ⚠️ Smart Money Bars Proxy skipped: outside regular-session bar window "
            f"({iso_et(start_et)} -> {iso_et(end_et)})"
        )
        return {}

    start_iso = to_utc_iso(start_et)
    end_iso = to_utc_iso(end_et)

    out: Dict[str, pd.DataFrame] = {}

    for chunk in chunks(symbols, CHUNK_SIZE):
        params = {
            "symbols": ",".join(chunk),
            "timeframe": TIMEFRAME,
            "start": start_iso,
            "end": end_iso,
            "limit": ALPACA_BAR_LIMIT,
            "adjustment": "raw",
            "sort": "asc",
        }

        try:
            data = feed._get("/stocks/bars", params=params, feed=DATA_FEED)
        except Exception as exc:
            print(f"  ⚠️ Alpaca bars request failed for {','.join(chunk[:5])}...: {exc}")
            continue

        bars_by_symbol = data.get("bars", {}) or {}
        for sym in chunk:
            rows = bars_by_symbol.get(sym, []) or []
            if not rows:
                continue
            df = pd.DataFrame(rows)
            if df.empty:
                continue
            out[sym] = normalize_bars_df(df)

    return out


def normalize_bars_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Expected Alpaca keys: t, o, h, l, c, v, vw, n
    for col in ["o", "h", "l", "c", "v", "vw", "n"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "t" in df.columns:
        df["timestamp_utc"] = pd.to_datetime(df["t"], utc=True, errors="coerce")
        df["timestamp_et"] = df["timestamp_utc"].dt.tz_convert("America/New_York")
    else:
        df["timestamp_utc"] = pd.NaT
        df["timestamp_et"] = pd.NaT

    df = df.dropna(subset=["c", "h", "l", "v"]).copy()
    if df.empty:
        return df

    df = df.sort_values("timestamp_utc").reset_index(drop=True)

    # Keep regular session bars only if configured.
    if REGULAR_SESSION_ONLY and "timestamp_et" in df.columns:
        et_times = df["timestamp_et"].dt.time
        start_t = datetime.strptime("09:30", "%H:%M").time()
        end_t = datetime.strptime("16:00", "%H:%M").time()
        df = df[(et_times >= start_t) & (et_times <= end_t)].copy()

    return df.reset_index(drop=True)


# =============================================================================
# Scoring logic
# =============================================================================

def get_complete_bars(df: pd.DataFrame) -> pd.DataFrame:
    """
    Avoid scoring an obviously incomplete live 5-minute bar when possible.
    If filtering removes too much data, return the original df.
    """
    if df.empty or "timestamp_et" not in df.columns:
        return df

    current = now_et()
    timeframe_minutes = 5
    match = re.fullmatch(r"(\d+)Min", TIMEFRAME)
    if match:
        timeframe_minutes = int(match.group(1))

    cutoff = current - timedelta(seconds=20)
    end_times = df["timestamp_et"] + pd.to_timedelta(timeframe_minutes, unit="m")
    complete = df[end_times <= cutoff].copy()

    if len(complete) >= 2:
        return complete.reset_index(drop=True)

    return df.reset_index(drop=True)


def calculate_session_vwap(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0

    vol = pd.to_numeric(df.get("v", pd.Series(dtype=float)), errors="coerce").fillna(0.0)

    if "vw" in df.columns:
        vw = pd.to_numeric(df["vw"], errors="coerce").fillna(0.0)
        valid = (vol > 0) & (vw > 0)
        if valid.any() and vol[valid].sum() > 0:
            return float((vw[valid] * vol[valid]).sum() / vol[valid].sum())

    high = pd.to_numeric(df.get("h", pd.Series(dtype=float)), errors="coerce")
    low = pd.to_numeric(df.get("l", pd.Series(dtype=float)), errors="coerce")
    close = pd.to_numeric(df.get("c", pd.Series(dtype=float)), errors="coerce")
    typical = (high + low + close) / 3.0
    valid = (vol > 0) & typical.notna()
    if valid.any() and vol[valid].sum() > 0:
        return float((typical[valid] * vol[valid]).sum() / vol[valid].sum())

    return safe_float(df.iloc[-1].get("c"), 0.0)


def count_vwap_touches(df: pd.DataFrame, vwap: float, distance_pct: float = VWAP_TOUCH_DISTANCE_PCT) -> int:
    if df.empty or vwap <= 0:
        return 0

    band = vwap * (distance_pct / 100.0)
    lows = pd.to_numeric(df["l"], errors="coerce")
    highs = pd.to_numeric(df["h"], errors="coerce")
    closes = pd.to_numeric(df["c"], errors="coerce")

    # Touch if the bar crosses the VWAP band or closes inside it.
    touched = ((lows <= vwap + band) & (highs >= vwap - band)) | (abs(closes - vwap) <= band)
    return int(touched.tail(30).sum())


def price_bucket_increment(price: float) -> float:
    if price < 10:
        return 0.05
    if price < 50:
        return 0.10
    if price < 200:
        return 0.25
    if price < 500:
        return 0.50
    return 1.00


def detect_volume_cluster_breakout(df: pd.DataFrame) -> Tuple[bool, Optional[float], float]:
    """
    Detect a simple high-volume price-node breakout.

    Returns:
      (breakout_detected, breakout_level, breakout_volume_ratio)
    """
    if len(df) < 8:
        return False, None, 0.0

    latest = df.iloc[-1]
    prior = df.iloc[:-1].tail(50).copy()
    if len(prior) < 6:
        return False, None, 0.0

    latest_close = safe_float(latest.get("c"))
    prev_close = safe_float(df.iloc[-2].get("c"))
    latest_vol = safe_float(latest.get("v"))

    if latest_close <= 0 or latest_vol <= 0:
        return False, None, 0.0

    avg_vol = safe_float(pd.to_numeric(prior["v"], errors="coerce").tail(20).mean(), 0.0)
    volume_ratio = latest_vol / avg_vol if avg_vol > 0 else 0.0

    inc = price_bucket_increment(latest_close)
    prior["price_bucket"] = (pd.to_numeric(prior["c"], errors="coerce") / inc).round() * inc

    grouped = prior.groupby("price_bucket")["v"].sum().sort_values(ascending=False)
    if grouped.empty:
        return False, None, volume_ratio

    # Test the top three volume nodes. The nearest meaningful one below price is most useful.
    top_levels = [safe_float(x) for x in grouped.head(3).index.tolist()]
    top_levels = [x for x in top_levels if x > 0 and x < latest_close]
    if not top_levels:
        return False, None, volume_ratio

    # Prefer the closest high-volume node below the current close.
    level = sorted(top_levels, key=lambda x: abs(latest_close - x))[0]

    broke_above = latest_close > level * 1.002
    was_near_or_below = prev_close <= level * 1.008
    not_too_extended = (latest_close - level) / level * 100.0 <= 5.0 if level > 0 else False
    volume_confirmed = volume_ratio >= CLUSTER_BREAKOUT_VOLUME_RATIO

    return bool(broke_above and was_near_or_below and not_too_extended and volume_confirmed), level, volume_ratio


def score_volume(df: pd.DataFrame) -> Tuple[int, float, List[str]]:
    if len(df) < 2:
        return 0, 0.0, []

    latest_vol = safe_float(df.iloc[-1].get("v"), 0.0)
    prior = df.iloc[:-1].tail(20)
    avg_vol = safe_float(pd.to_numeric(prior["v"], errors="coerce").mean(), 0.0)
    ratio = latest_vol / avg_vol if avg_vol > 0 else 0.0

    if ratio >= VOLUME_SURGE_STRONG:
        return 30, ratio, [f"Volume surge {ratio:.1f}x"]
    if ratio >= VOLUME_SURGE_MODERATE:
        return 15, ratio, [f"Volume elevated {ratio:.1f}x"]
    return 0, ratio, []


def score_vwap(df: pd.DataFrame, vwap: float) -> Tuple[int, float, int, List[str]]:
    if df.empty or vwap <= 0:
        return 0, 0.0, 0, []

    latest = df.iloc[-1]
    price = safe_float(latest.get("c"), 0.0)
    if price <= 0:
        return 0, 0.0, 0, []

    dist_pct = (price - vwap) / vwap * 100.0
    touches = count_vwap_touches(df, vwap)

    signals: List[str] = []
    score = 0

    # Strong: above VWAP, not extended, and VWAP has been actively respected.
    if price >= vwap and 0 <= dist_pct <= VWAP_TOUCH_DISTANCE_PCT and touches >= 3:
        score = 25
        signals.append(f"VWAP anchor ({touches} touches)")
    elif price >= vwap and 0 <= dist_pct <= VWAP_BASIC_HOLD_DISTANCE_PCT:
        score = 12
        signals.append("VWAP hold")
    elif price < vwap and dist_pct <= -VWAP_BASIC_HOLD_DISTANCE_PCT:
        signals.append("Below VWAP")

    # Reclaim pattern: previous close below VWAP, latest close above VWAP.
    if len(df) >= 2:
        prev_close = safe_float(df.iloc[-2].get("c"), 0.0)
        if prev_close < vwap and price >= vwap and abs(dist_pct) <= VWAP_BASIC_HOLD_DISTANCE_PCT:
            score = max(score, 20)
            if "VWAP reclaim" not in signals:
                signals.append("VWAP reclaim")

    return score, dist_pct, touches, signals


def score_large_candle(df: pd.DataFrame) -> Tuple[int, float, float, List[str]]:
    if len(df) < 2:
        return 0, 0.0, 0.0, []

    latest = df.iloc[-1]
    high = safe_float(latest.get("h"))
    low = safe_float(latest.get("l"))
    close = safe_float(latest.get("c"))
    current_range = high - low

    if high <= low or current_range <= 0:
        return 0, 0.0, 0.0, []

    prior = df.iloc[:-1].tail(20)
    prior_ranges = pd.to_numeric(prior["h"], errors="coerce") - pd.to_numeric(prior["l"], errors="coerce")
    avg_range = safe_float(prior_ranges.mean(), 0.0)
    range_ratio = current_range / avg_range if avg_range > 0 else 0.0
    close_location = (close - low) / current_range if current_range > 0 else 0.0

    signals: List[str] = []
    score = 0

    if range_ratio >= LARGE_CANDLE_STRONG and close_location >= 0.70:
        score = 20
        signals.append(f"Large bullish candle {range_ratio:.1f}x")
    elif range_ratio >= LARGE_CANDLE_MODERATE and close_location >= 0.60:
        score = 10
        signals.append(f"Expanded bullish candle {range_ratio:.1f}x")
    elif range_ratio >= LARGE_CANDLE_MODERATE and close_location <= 0.30:
        signals.append(f"Large bearish candle {range_ratio:.1f}x")

    return score, range_ratio, close_location, signals


def raw_score_to_adjustment(raw_score: int) -> Tuple[int, str]:
    if raw_score >= 85:
        return 5, "Strong Proxy"
    if raw_score >= 70:
        return 3, "Moderate Proxy"
    if raw_score >= 55:
        return 1, "Weak Proxy"
    if raw_score <= 30:
        return -3, "Weak Structure"
    return 0, "Neutral"


def score_symbol(symbol: str, df: pd.DataFrame, candidate: Optional[Candidate] = None) -> Dict[str, Any]:
    current = now_et()

    if df.empty:
        return {
            "symbol": symbol,
            "raw_score": 0,
            "score_adjustment": 0,
            "label": "No Data",
            "bias": "NEUTRAL",
            "signals": [],
            "updated_at_et": iso_et(current),
            "error": "No bars returned",
        }

    complete = get_complete_bars(df)
    if complete.empty:
        complete = df

    # Need at least a few bars for a meaningful regular-session score.
    if len(complete) < 3:
        latest = complete.iloc[-1] if not complete.empty else df.iloc[-1]
        price = safe_float(latest.get("c"), 0.0)
        return {
            "symbol": symbol,
            "raw_score": 0,
            "score_adjustment": 0,
            "label": "Insufficient Bars",
            "bias": "NEUTRAL",
            "last_price": round(price, 4) if price > 0 else 0,
            "bar_count": int(len(complete)),
            "signals": ["Insufficient regular-session bars"],
            "updated_at_et": iso_et(current),
        }

    latest = complete.iloc[-1]
    price = safe_float(latest.get("c"), 0.0)

    vwap = calculate_session_vwap(complete)
    volume_score, volume_ratio, volume_signals = score_volume(complete)
    vwap_score, vwap_dist_pct, vwap_touches, vwap_signals = score_vwap(complete, vwap)
    candle_score, range_ratio, close_location, candle_signals = score_large_candle(complete)

    cluster_detected, cluster_level, cluster_volume_ratio = detect_volume_cluster_breakout(complete)
    cluster_score = 15 if cluster_detected else 0
    cluster_signals = [f"Cluster breakout near {cluster_level:.2f}"] if cluster_detected and cluster_level else []

    time_score = 10 if is_high_probability_time(current) else 0
    time_signals = ["High-probability time window"] if time_score > 0 else []

    raw_score = int(clamp(volume_score + vwap_score + candle_score + cluster_score + time_score, 0, 100))
    adjustment, label = raw_score_to_adjustment(raw_score)

    # Bias is intentionally conservative.
    bias = "NEUTRAL"
    if price > 0 and vwap > 0:
        if raw_score >= 55 and price >= vwap:
            bias = "BULLISH"
        elif raw_score <= 30 or price < vwap:
            bias = "WEAK_OR_BEARISH"

    signals = volume_signals + vwap_signals + candle_signals + cluster_signals + time_signals

    last_bar_et = ""
    try:
        ts = latest.get("timestamp_et")
        if pd.notna(ts):
            last_bar_et = ts.isoformat()
    except Exception:
        last_bar_et = ""

    return {
        "symbol": symbol,
        "phase": "PHASE_1_BARS_PROXY",
        "version": VERSION,
        "source": "Alpaca SIP bars" if DATA_FEED == "sip" else f"Alpaca {DATA_FEED.upper()} bars",
        "raw_score": raw_score,
        "score_adjustment": int(clamp(adjustment, PHASE1_MIN_ADJUSTMENT, PHASE1_MAX_ADJUSTMENT)),
        "label": label,
        "bias": bias,
        "signals": signals[:10],
        "scanner_score": round(candidate.scanner_score, 2) if candidate else 0.0,
        "setup_bucket": candidate.setup_bucket if candidate else "",
        "risk_category": candidate.risk_category if candidate else "",
        "last_price": round(price, 4) if price > 0 else 0.0,
        "vwap": round(vwap, 4) if vwap > 0 else 0.0,
        "vwap_distance_pct": round(vwap_dist_pct, 3),
        "vwap_touch_count": int(vwap_touches),
        "volume_ratio": round(volume_ratio, 3),
        "range_ratio": round(range_ratio, 3),
        "close_location": round(close_location, 3),
        "cluster_breakout": bool(cluster_detected),
        "cluster_level": round(cluster_level, 4) if cluster_level else None,
        "cluster_volume_ratio": round(cluster_volume_ratio, 3),
        "time_window_score": int(time_score),
        "bar_count": int(len(complete)),
        "last_bar_time_et": last_bar_et,
        "updated_at_et": iso_et(current),
    }


# =============================================================================
# Persistence
# =============================================================================

def append_history(payload: Dict[str, Any]) -> None:
    symbols = payload.get("symbols", {}) or {}
    if not symbols:
        return

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    exists = HISTORY_FILE.exists()

    fieldnames = [
        "run_at_et",
        "symbol",
        "raw_score",
        "score_adjustment",
        "label",
        "bias",
        "scanner_score",
        "setup_bucket",
        "risk_category",
        "last_price",
        "vwap",
        "vwap_distance_pct",
        "vwap_touch_count",
        "volume_ratio",
        "range_ratio",
        "cluster_breakout",
        "signals",
    ]

    run_at = payload.get("metadata", {}).get("generated_at_et", iso_et())

    with HISTORY_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()

        for sym, rec in symbols.items():
            writer.writerow({
                "run_at_et": run_at,
                "symbol": sym,
                "raw_score": rec.get("raw_score", 0),
                "score_adjustment": rec.get("score_adjustment", 0),
                "label": rec.get("label", ""),
                "bias": rec.get("bias", ""),
                "scanner_score": rec.get("scanner_score", 0),
                "setup_bucket": rec.get("setup_bucket", ""),
                "risk_category": rec.get("risk_category", ""),
                "last_price": rec.get("last_price", 0),
                "vwap": rec.get("vwap", 0),
                "vwap_distance_pct": rec.get("vwap_distance_pct", 0),
                "vwap_touch_count": rec.get("vwap_touch_count", 0),
                "volume_ratio": rec.get("volume_ratio", 0),
                "range_ratio": rec.get("range_ratio", 0),
                "cluster_breakout": rec.get("cluster_breakout", False),
                "signals": " | ".join(rec.get("signals", []) or []),
            })


# =============================================================================
# Main
# =============================================================================

def build_payload(candidates: List[Candidate], scores: Dict[str, Dict[str, Any]], errors: List[str]) -> Dict[str, Any]:
    current = now_et()
    start_et, end_et = request_start_end(current)

    return {
        "metadata": {
            "version": VERSION,
            "phase": "PHASE_1_BARS_PROXY",
            "generated_at_et": iso_et(current),
            "source": "Alpaca SIP bars" if DATA_FEED == "sip" else f"Alpaca {DATA_FEED.upper()} bars",
            "feed": DATA_FEED,
            "timeframe": TIMEFRAME,
            "regular_session_only": REGULAR_SESSION_ONLY,
            "request_start_et": iso_et(start_et),
            "request_end_et": iso_et(end_et),
            "max_age_seconds": MAX_AGE_SECONDS,
            "symbol_limit": SYMBOL_LIMIT,
            "candidates_loaded": len(candidates),
            "symbols_scored": len(scores),
            "errors": errors[:20],
            "notes": [
                "Phase 1 uses bars only.",
                "No WebSocket, OPRA, dark-pool, sweep, or institutional-intent claims.",
                "Score adjustment is capped between -3 and +5.",
            ],
        },
        "symbols": scores,
    }


def run() -> int:
    print("=" * 72)
    print("Smart Money Bars Proxy | Phase 1")
    print("=" * 72)

    errors: List[str] = []

    candidates = load_candidates(SYMBOL_LIMIT)
    if not candidates:
        msg = "No candidates found. Run elite_scanner.py first."
        print(f"  ⚠️ {msg}")
        errors.append(msg)
        payload = build_payload([], {}, errors)
        atomic_write_json(OUTPUT_FILE, payload)
        return 0

    symbols = [c.symbol for c in candidates]
    candidate_by_symbol = {c.symbol: c for c in candidates}

    print(f"  Candidates loaded: {len(symbols)}")
    print(f"  Data feed: Alpaca {DATA_FEED.upper()} | timeframe={TIMEFRAME} | regular_session_only={REGULAR_SESSION_ONLY}")

    try:
        feed = get_alpaca_feed()
    except Exception as exc:
        msg = str(exc)
        print(f"  ⚠️ {msg}")
        errors.append(msg)
        payload = build_payload(candidates, {}, errors)
        atomic_write_json(OUTPUT_FILE, payload)
        return 0

    bars = fetch_bars_for_symbols(feed, symbols)
    print(f"  Symbols with bars: {len(bars)}")

    scores: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        df = bars.get(sym)
        if df is None or df.empty:
            continue
        try:
            scores[sym] = score_symbol(sym, df, candidate_by_symbol.get(sym))
        except Exception as exc:
            err = f"{sym}: scoring failed: {exc}"
            print(f"  ⚠️ {err}")
            errors.append(err)

    payload = build_payload(candidates, scores, errors)
    atomic_write_json(OUTPUT_FILE, payload)
    append_history(payload)

    # Lightweight console summary.
    ranked = sorted(scores.values(), key=lambda r: (safe_float(r.get("score_adjustment")), safe_float(r.get("raw_score"))), reverse=True)
    print(f"  Wrote: {OUTPUT_FILE.name}")
    print(f"  History: {HISTORY_FILE.name}")
    print("\nTop Smart Money Proxy names:")
    if not ranked:
        print("  No scored symbols.")
    else:
        for rec in ranked[:10]:
            sig = "; ".join(rec.get("signals", [])[:3])
            print(
                f"  {rec.get('symbol'):>6} | raw={rec.get('raw_score'):>3} | adj={rec.get('score_adjustment'):>+2} | "
                f"{rec.get('label'):<15} | vol={safe_float(rec.get('volume_ratio')):.1f}x | "
                f"vwap_dist={safe_float(rec.get('vwap_distance_pct')):+.2f}% | {sig}"
            )

    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
