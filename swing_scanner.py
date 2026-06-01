#!/usr/bin/env python3
"""
Universal 1-3 Day Swing Scanner
top-level version = v1.3.3_setup_tracking
institutional_model.version = v1.3.3_setup_tracking

Purpose:
- Universal scanner. Not tied to the 300-ticker dataset.
- Scans whatever universe/data source is passed at runtime.
- Supports:
    1) daily+1H parquet swing mode plus legacy raw parquet validation mode
    2) Alpaca live data mode, using auto-discovered dynamic universe plus optional user seeds
    3) institutional swing filtering: SMA/EMA, MACD, smart money, news, sector, volume, setup-specific R/R
    4) tiered shortlist behavior: hard reject severe risk, downgrade soft weakness to Watch, rank top 8-10
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


SCANNER_VERSION = "swing_scanner_v1.3.3_setup_tracking"

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
    "INSTITUTIONAL_SWING_CANDIDATE_POOL": 0,
}

INSTITUTIONAL_SCORE_WEIGHTS = {
    "trend_sma_ema": 15.0,
    "setup_quality": 15.0,
    "macd_momentum": 10.0,
    "smart_money_confirmation": 20.0,
    "news_catalyst": 15.0,
    "volume_accumulation": 10.0,
    "sector_market": 10.0,
    "risk_stop": 5.0,
}

SETUP_RR_GATES = {
    "DAILY_BREAKOUT_CONTINUATION": 1.85,
    "SWING_PULLBACK_SUPPORT_HOLD": 1.55,
    "GAP_HOLD_SWING": 1.35,
    "DAY_TO_SWING_PROMOTION": 1.50,
    "INSTITUTIONAL_SWING_CANDIDATE_POOL": 99.0,
}

SETUP_WATCH_RR_GATES = {
    "DAILY_BREAKOUT_CONTINUATION": 1.25,
    "SWING_PULLBACK_SUPPORT_HOLD": 1.20,
    "GAP_HOLD_SWING": 1.15,
    "DAY_TO_SWING_PROMOTION": 1.20,
    "INSTITUTIONAL_SWING_CANDIDATE_POOL": 1.20,
}

SWING_READY_SCORE = 86.0
SWING_WATCH_SCORE = 72.0
SWING_POOL_WATCH_SCORE = 72.0
SWING_PROXY_WATCH_SCORE = 72.0
SWING_POOL_DISPLAY_SCORE = 72.0
SWING_POOL_DISPLAY_RR_MIN = 1.20

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
    "setup_generated_at_et",
    "setup_price",
    "setup_data_time_et",
    "move_since_setup_pct",
    "avg_volume_20d",
    "avg_dollar_volume_20d",
    "atr_pct",
    "atr_tier",
    "rsi_14",
    "sma20",
    "sma50",
    "sma200",
    "ema9",
    "ema20",
    "ema21",
    "macd_line",
    "macd_signal",
    "macd_hist",
    "macd_state",
    "macd_hist_slope",
    "above_sma20",
    "above_sma50",
    "above_sma200",
    "above_ema20",
    "above_ema21",
    "trend_sma_ema_score",
    "setup_quality_score",
    "macd_momentum_score",
    "smart_money_confirmation_score",
    "news_catalyst_score",
    "volume_accumulation_score",
    "sector_market_score",
    "risk_stop_score",
    "institutional_grade",
    "setup_rr_gate",
    "smart_money_score",
    "smart_money_bias",
    "smart_money_label",
    "smart_money_signals",
    "news_risk",
    "news_score",
    "news_summary",
    "positive_catalyst",
    "negative_catalyst",
    "sector_context",
    "sector_score",
    "volume_pattern",
    "close_to_high_pct",
    "close_quality_3d",
    "atr_trend",
    "overnight_gap_risk",
    "stop_atr_multiple",
    "entry_strategy",
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
    avg_dollar_volume_20d: float
    atr_pct: float
    atr_tier: str
    rsi_14: float
    sma20: float
    sma50: float
    sma200: float
    ema9: float
    ema20: float
    ema21: float
    macd_line: float
    macd_signal: float
    macd_hist: float
    macd_state: str
    macd_hist_slope: float
    above_sma20: bool
    above_sma50: bool
    above_sma200: bool
    above_ema20: bool
    above_ema21: bool
    trend_sma_ema_score: float
    setup_quality_score: float
    macd_momentum_score: float
    smart_money_confirmation_score: float
    news_catalyst_score: float
    volume_accumulation_score: float
    sector_market_score: float
    risk_stop_score: float
    institutional_grade: str
    setup_rr_gate: float
    smart_money_score: float
    smart_money_bias: str
    smart_money_label: str
    smart_money_signals: str
    news_risk: str
    news_score: float
    news_summary: str
    positive_catalyst: bool
    negative_catalyst: bool
    sector_context: str
    sector_score: float
    volume_pattern: str
    close_to_high_pct: float
    close_quality_3d: float
    atr_trend: str
    overnight_gap_risk: str
    stop_atr_multiple: float
    entry_strategy: str
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
    for suffix in ["_Daily", "_DAILY", "_daily", "_1H", "_1h", "_Hour", "_hour", "_1Min", "_1min", "_1MIN", "_minute", "_Minute", "_15m", "_15M", "_15Min", "_15min", "_5m", "_5M", "_5Min", "_5min"]:
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


# ==============================================================
# AUTO SWING UNIVERSE DISCOVERY
# ==============================================================
#
# Manual symbol files are optional seeds only.  The live Swing Desk must be able
# to discover its own candidates, similar to the day-trade scanner.  We build a
# dynamic long-only swing universe from broad Yahoo predefined screeners, prior
# local scanner outputs, and a small liquid core fallback.  Alpaca remains the
# OHLCV source used to validate each candidate.
#
# No production signals, alerts, or broker routes are modified here.

SWING_DYNAMIC_SCREENERS = [
    "day_gainers",
    "most_actives",
    "growth_technology_stocks",
    "small_cap_gainers",
    "undervalued_growth_stocks",
    "undervalued_large_caps",
]

SWING_OPTIONAL_SEED_FILES = [
    "static_swing_universe.csv",
    "swing_universe.csv",
    "static_liquid_universe.csv",
    "liquid_universe.csv",
    "regular_market_universe.csv",
    "potential_movers.csv",
    "active_momentum.csv",
    "elite_watchlist_raw.csv",
    "premarket_movers.csv",
    os.path.join("swing_results", "swing_candidates_latest.csv"),
]

SWING_CORE_SYMBOLS = [
    # Liquid market leaders / sector representatives used only as a fallback
    # seed.  Actual inclusion still requires the scanner's trend, liquidity,
    # ATR, support, target-room, and earnings-risk filters.
    "AAPL", "MSFT", "NVDA", "AMD", "AVGO", "TSM", "ASML", "ARM", "MU", "QCOM",
    "AMZN", "META", "GOOGL", "NFLX", "UBER", "SHOP", "CRM", "NOW", "SNOW", "DDOG",
    "TSLA", "RIVN", "GM", "F", "NIO", "LI", "XPEV", "PLTR", "AI", "SOUN",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "AXP", "PYPL",
    "LLY", "NVO", "UNH", "ABBV", "MRK", "PFE", "TMO", "ISRG", "VRTX", "REGN",
    "XOM", "CVX", "OXY", "SLB", "HAL", "COP", "LNG", "ENPH", "FSLR", "SEDG",
    "CAT", "DE", "GE", "BA", "LMT", "RTX", "NOC", "HON", "ETN", "URI",
    "WMT", "COST", "HD", "LOW", "TGT", "MCD", "SBUX", "NKE", "LULU", "CMG",
    "SPY", "QQQ", "IWM", "SMH", "XLK", "XLF", "XLE", "XLI", "XLV", "XLY",
]


def normalize_symbol_for_swing(symbol: Any) -> str:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return ""
    if any(c in sym for c in ["^", "=", "/"]):
        return ""
    if "." in sym:
        return ""
    return sym


def read_optional_symbol_seed_file(path: str) -> List[str]:
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return []
        if p.suffix.lower() == ".json":
            data = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, list):
                raw = [x.get("symbol") if isinstance(x, dict) else x for x in data]
                return [normalize_symbol_for_swing(x) for x in raw if normalize_symbol_for_swing(x)]
            return []
        df = pd.read_csv(p)
        if df.empty:
            return []
        for col in ["symbol", "Symbol", "ticker", "Ticker"]:
            if col in df.columns:
                return [normalize_symbol_for_swing(x) for x in df[col].dropna().tolist() if normalize_symbol_for_swing(x)]
        return [normalize_symbol_for_swing(x) for x in df.iloc[:, 0].dropna().tolist() if normalize_symbol_for_swing(x)]
    except Exception:
        return []


def fetch_yahoo_screener_symbols(args: argparse.Namespace) -> Tuple[List[str], Dict[str, int], Dict[str, str]]:
    found: List[str] = []
    counts: Dict[str, int] = {}
    errors: Dict[str, str] = {}

    for screener in SWING_DYNAMIC_SCREENERS:
        try:
            url = (
                "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?"
                + urllib.parse.urlencode({"count": "100", "scrIds": screener})
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))

            quotes = data.get("finance", {}).get("result", [{}])[0].get("quotes", [])
            local_count = 0
            for q in quotes:
                if not isinstance(q, dict):
                    continue
                sym = normalize_symbol_for_swing(q.get("symbol"))
                if not sym:
                    continue

                price = safe_float(
                    q.get("regularMarketPrice")
                    or q.get("postMarketPrice")
                    or q.get("preMarketPrice"),
                    0.0,
                )
                avg_vol = safe_float(
                    q.get("averageDailyVolume3Month")
                    or q.get("averageDailyVolume10Day")
                    or q.get("regularMarketVolume"),
                    0.0,
                )

                if price and (price < args.min_price or price > args.max_price):
                    continue
                if avg_vol and avg_vol < args.min_avg_volume:
                    continue

                found.append(sym)
                local_count += 1

            counts[screener] = local_count
        except Exception as exc:
            errors[screener] = repr(exc)

    return found, counts, errors


def build_auto_swing_universe(
    args: argparse.Namespace,
    explicit_symbols: Sequence[str],
) -> Tuple[List[str], Dict[str, Any]]:
    ordered: List[str] = []
    seen = set()
    source_counts: Dict[str, int] = {}
    source_errors: Dict[str, str] = {}

    def add_many(symbols: Iterable[Any], source_label: str) -> None:
        added = 0
        for raw in symbols or []:
            sym = normalize_symbol_for_swing(raw)
            if not sym or sym in seen:
                continue
            seen.add(sym)
            ordered.append(sym)
            added += 1
        if added:
            source_counts[source_label] = source_counts.get(source_label, 0) + added

    # Explicit symbols are honored first if supplied, but they are no longer
    # required. They act as a seed/override, not as the only live universe.
    add_many(explicit_symbols, "explicit_symbols")

    yahoo_symbols, yahoo_counts, yahoo_errors = fetch_yahoo_screener_symbols(args)
    add_many(yahoo_symbols, "yahoo_dynamic_screeners")
    for k, v in yahoo_counts.items():
        source_counts[f"yahoo:{k}"] = v
    source_errors.update({f"yahoo:{k}": v for k, v in yahoo_errors.items()})

    for fname in SWING_OPTIONAL_SEED_FILES:
        add_many(read_optional_symbol_seed_file(fname), fname)

    add_many(SWING_CORE_SYMBOLS, "liquid_core_fallback")

    cap = int(getattr(args, "max_universe_symbols", 650) or 650)
    symbols = ordered[: max(25, cap)]

    meta = {
        "mode": "auto_universe",
        "symbols": len(symbols),
        "max_universe_symbols": cap,
        "source_counts": source_counts,
        "source_errors": dict(list(source_errors.items())[:20]),
        "note": "Auto-discovered swing universe. Optional symbol files are seeds only, not required.",
    }
    return symbols, meta


def write_universe_cache(symbols: Sequence[str], meta: Dict[str, Any], path: Optional[str]) -> None:
    if not path:
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"symbol": list(symbols)}).to_csv(p, index=False)
        meta_path = p.with_suffix(".json")
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception:
        pass


def _as_indexed_series(values: Any, index: pd.Index) -> pd.Series:
    if isinstance(values, pd.Series):
        return values
    return pd.Series(values, index=index)


def _series_has_timezone_marker(values: pd.Series) -> bool:
    try:
        s = values.dropna().astype(str)
        if s.empty:
            return False
        return bool(s.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True).any())
    except Exception:
        return False


def _parse_et_datetime(values: Any, index: pd.Index) -> pd.Series:
    """
    Normalize timestamps to one consistent ET-aware dtype.

    Alpaca live-cache parquet stores timestamp_et with explicit offsets.
    Across DST boundaries that column legitimately contains mixed offsets
    (-0400 and -0500). Pandas 2.x raises "Mixed timezones detected" unless
    those strings are parsed with utc=True first.
    """
    raw = _as_indexed_series(values, index)

    if _series_has_timezone_marker(raw):
        parsed_utc = pd.to_datetime(raw, utc=True, errors="coerce")
        return parsed_utc.dt.tz_convert("America/New_York")

    parsed = pd.to_datetime(raw, errors="coerce")
    try:
        if getattr(parsed.dt, "tz", None) is None:
            return parsed.dt.tz_localize("America/New_York", nonexistent="shift_forward", ambiguous="NaT")
        return parsed.dt.tz_convert("America/New_York")
    except Exception:
        return parsed


def _date_et_from_dt(values: Any, index: pd.Index) -> pd.Series:
    raw = _as_indexed_series(values, index)
    try:
        if hasattr(raw, "dt"):
            return raw.dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    try:
        parsed = pd.to_datetime(raw, utc=True, errors="coerce")
        return parsed.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    except Exception:
        parsed = pd.to_datetime(raw, errors="coerce")
        return parsed.dt.strftime("%Y-%m-%d")


def infer_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "timestamp_et" in out.columns:
        out["dt_et"] = _parse_et_datetime(out["timestamp_et"], out.index)
    elif "timestamp_utc" in out.columns:
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
        out["dt_et"] = _parse_et_datetime(out.index, out.index)

    if "date_et" not in out.columns:
        if "date" in out.columns:
            out["date_et"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        else:
            out["date_et"] = _date_et_from_dt(out["dt_et"], out.index)
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
    d["ema9"] = d["close"].ewm(span=9, adjust=False, min_periods=9).mean()
    d["ema20"] = d["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    d["ema21"] = d["close"].ewm(span=21, adjust=False, min_periods=21).mean()

    ema12 = d["close"].ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = d["close"].ewm(span=26, adjust=False, min_periods=26).mean()
    d["macd_line"] = ema12 - ema26
    d["macd_signal"] = d["macd_line"].ewm(span=9, adjust=False, min_periods=9).mean()
    d["macd_hist"] = d["macd_line"] - d["macd_signal"]
    d["macd_hist_slope"] = d["macd_hist"] - d["macd_hist"].shift(2)

    prev_close = d["close"].shift(1)
    tr = pd.concat([
        (d["high"] - d["low"]).abs(),
        (d["high"] - prev_close).abs(),
        (d["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    d["atr14"] = tr.rolling(14, min_periods=14).mean()
    d["atr_pct"] = d["atr14"] / d["close"] * 100.0
    d["atr_pct_5d_avg"] = d["atr_pct"].rolling(5, min_periods=3).mean()
    d["atr_expanding"] = d["atr_pct"] > d["atr_pct_5d_avg"]

    delta = d["close"].diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=14).mean()
    rs = gain / loss.replace(0, pd.NA)
    d["rsi_14"] = 100 - (100 / (1 + rs))
    d["avg_volume_20d"] = d["volume"].rolling(20, min_periods=10).mean()
    d["avg_dollar_volume_20d"] = d["avg_volume_20d"] * d["close"]
    d["avg_volume_10d"] = d["volume"].rolling(10, min_periods=5).mean()
    d["avg_volume_3d"] = d["volume"].rolling(3, min_periods=2).mean()
    d["rel_volume"] = d["volume"] / d["avg_volume_20d"].replace(0, pd.NA)
    d["volume_3d_vs_10d"] = d["avg_volume_3d"] / d["avg_volume_10d"].replace(0, pd.NA)

    d["ret_1d_pct"] = (d["close"] / d["close"].shift(1) - 1) * 100
    d["ret_5d_pct"] = (d["close"] / d["close"].shift(5) - 1) * 100
    d["ret_20d_pct"] = (d["close"] / d["close"].shift(20) - 1) * 100
    d["gap_pct"] = (d["open"] / d["close"].shift(1) - 1) * 100

    day_range = (d["high"] - d["low"]).replace(0, pd.NA)
    d["close_location_pct"] = (d["close"] - d["low"]) / day_range * 100.0
    d["close_to_high_pct"] = (d["close"] / d["high"].replace(0, pd.NA) - 1) * 100.0
    d["close_quality_3d"] = d["close_location_pct"].rolling(3, min_periods=2).mean()

    up_day = d["close"] > d["close"].shift(1)
    down_day = d["close"] < d["close"].shift(1)
    up_vol = d["volume"].where(up_day, 0).rolling(5, min_periods=3).sum()
    down_vol = d["volume"].where(down_day, 0).rolling(5, min_periods=3).sum()
    d["up_volume_ratio_5d"] = up_vol / (up_vol + down_vol).replace(0, pd.NA)
    d["overnight_gap_abs_20d"] = d["gap_pct"].abs().rolling(20, min_periods=10).median()

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
            "close_location_pct": 50.0,
            "close_to_high_pct": 0.0,
        }
    day = standardize_ohlcv_columns(infer_datetime_columns(day)).sort_values("dt_et")
    close = safe_float(day["close"].iloc[-1])
    open_ = safe_float(day["open"].iloc[0])
    high = safe_float(day["high"].max())
    low = safe_float(day["low"].min())
    vol = pd.to_numeric(day["volume"], errors="coerce").fillna(0)

    typical = (day["high"] + day["low"] + day["close"]) / 3.0
    v = vol.sum()
    vwap = safe_float((typical * vol).sum() / v) if v > 0 else close
    late_fade_pct = pct(close, high) if high > 0 else 0.0
    day_change_pct = pct(close, open_) if open_ > 0 else 0.0

    n = len(day)
    if n >= 30:
        first = safe_float(day["close"].iloc[max(0, n // 3 - 1)])
        mid = safe_float(day["close"].iloc[max(0, (2 * n) // 3 - 1)])
        if close > mid > first and close >= vwap:
            structure = 15.0
        elif close > vwap and late_fade_pct > -1.5:
            structure = 12.0
        elif close > first:
            structure = 8.0
        else:
            structure = 3.0
    else:
        structure = 8.0 if close >= vwap else 3.0

    rng = high - low
    close_loc = (close - low) / rng * 100.0 if rng > 0 else 50.0
    close_to_high_pct = pct(close, high) if high > 0 else 0.0

    last_third = day.iloc[max(0, (2 * n) // 3):] if n else day
    first_two = day.iloc[:max(1, (2 * n) // 3)] if n else day
    late_vol = pd.to_numeric(last_third.get("volume", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()
    early_vol = pd.to_numeric(first_two.get("volume", pd.Series(dtype=float)), errors="coerce").fillna(0).sum()
    late_pressure = late_vol > early_vol * 0.45 if early_vol > 0 else False

    panic = bool(
        close < vwap
        and day_change_pct <= -1.5
        and late_fade_pct <= -2.0
        and close_loc < 35.0
        and late_pressure
    )

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
        "close_location_pct": round4(close_loc),
        "close_to_high_pct": round4(close_to_high_pct),
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


def load_json_file(path: Optional[str], default: Any = None) -> Any:
    if not path:
        return default
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _payload_symbols_map(payload: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("symbols", payload)
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for sym, rec in raw.items():
        ns = normalize_symbol_for_swing(sym)
        if ns and isinstance(rec, dict):
            out[ns] = rec
    return out


def load_smart_money_map(path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    payload = load_json_file(path, {})
    return _payload_symbols_map(payload)


def load_news_risk_map(path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    payload = load_json_file(path, {})
    return _payload_symbols_map(payload)


def load_market_context(path: Optional[str]) -> Dict[str, Any]:
    payload = load_json_file(path, {})
    return payload if isinstance(payload, dict) else {}


def smart_money_context(symbol: str, smart_money_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    rec = smart_money_map.get(normalize_symbol_for_swing(symbol), {}) if smart_money_map else {}
    raw_score = safe_float(rec.get("raw_score", rec.get("score", 0.0)), 0.0) if rec else 0.0
    adj = safe_float(rec.get("score_adjustment", rec.get("adjustment", 0.0)), 0.0) if rec else 0.0
    label = str(rec.get("label", "")) if rec else ""
    bias = str(rec.get("bias", "")).upper() if rec else "UNKNOWN"
    if not bias:
        if raw_score >= 70 or adj >= 2:
            bias = "BULLISH"
        elif raw_score <= 35 or adj <= -2:
            bias = "BEARISH"
        elif rec:
            bias = "NEUTRAL"
        else:
            bias = "UNKNOWN"
    signals = rec.get("signals", "") if rec else ""
    signals_text = " | ".join(str(x) for x in signals if str(x)) if isinstance(signals, list) else str(signals or "")
    bearish = bias in {"BEARISH", "DISTRIBUTION", "NEGATIVE"}
    bullish = bias in {"BULLISH", "ACCUMULATION", "POSITIVE"}
    score = 9.0
    if bullish:
        score = 15.0 + min(5.0, max(0.0, raw_score - 70.0) / 6.0) + min(2.0, max(0.0, adj))
    elif bearish:
        score = max(0.0, 5.0 + min(0.0, adj))
    elif bias == "NEUTRAL":
        score = 10.0
    return {
        "score": round4(max(0.0, min(20.0, score))),
        "bias": bias,
        "label": label,
        "signals": signals_text,
        "raw_score": round4(raw_score),
        "adjustment": round4(adj),
        "bearish": bearish,
        "bullish": bullish,
        "missing": not bool(rec),
    }


def news_context(symbol: str, news_map: Dict[str, Dict[str, Any]], earnings_risk: str) -> Dict[str, Any]:
    rec = news_map.get(normalize_symbol_for_swing(symbol), {}) if news_map else {}
    risk = str(rec.get("risk", rec.get("news_risk", ""))).upper() if rec else "UNKNOWN"
    summary = str(rec.get("summary", rec.get("headline", ""))) if rec else ""
    positive = bool(rec.get("positive_catalyst", False)) if rec else False
    negative = bool(rec.get("negative_catalyst", False)) if rec else False
    data_status = str(rec.get("data_status", "")).lower() if rec else ""
    severe_terms = ("OFFERING", "DILUTION", "BANKRUPTCY", "SEC", "FRAUD", "LAWSUIT", "FDA_REJECTION", "GUIDANCE_CUT", "REVERSE_SPLIT", "DELIST")
    text_blob = " ".join(str(rec.get(k, "")) for k in rec.keys()).upper() if rec else ""
    severe = risk in {"SEVERE", "SEVERE_NEGATIVE", "BLOCK", "BLOCKED"} or any(t in text_blob for t in severe_terms)
    unavailable = (not bool(rec)) or data_status in {"unavailable", "missing", "error"} or risk in {"", "UNKNOWN", "UNAVAILABLE"}
    if earnings_risk == "BLOCKED_UPCOMING_EARNINGS":
        severe = True
        risk = "EARNINGS_BLOCK"
        unavailable = False
    if positive and not severe:
        score = 15.0
        risk = risk if risk and risk not in {"UNKNOWN", "UNAVAILABLE"} else "POSITIVE"
    elif severe:
        score = 0.0
    elif negative or risk in {"NEGATIVE", "HIGH"}:
        score = 4.0
    elif risk in {"NEUTRAL", "LOW"}:
        score = 11.0
    else:
        # Unknown/unavailable real news blocks Ready but can still allow Watch
        # if all technical, liquidity, and smart-money structure is clean.
        score = 9.0
        risk = "UNKNOWN"
        unavailable = True
    return {
        "score": round4(score),
        "risk": risk,
        "summary": summary or ("News unavailable" if unavailable else ""),
        "positive": positive,
        "negative": negative or severe,
        "severe": severe,
        "missing": unavailable,
    }


def sector_market_context(symbol: str, market_context: Dict[str, Any], latest: pd.Series) -> Dict[str, Any]:
    risk = str(market_context.get("risk", market_context.get("market_risk", ""))).upper() if market_context else ""
    regime = str(market_context.get("regime", "")).upper() if market_context else ""
    bias = str(market_context.get("bias", "")).upper() if market_context else ""
    label = str(market_context.get("label", "")).upper() if market_context else ""
    vix = safe_float(market_context.get("vix", market_context.get("vix_level", 0.0)), 0.0) if market_context else 0.0
    macro_risk = bool(market_context.get("major_event_48h", market_context.get("major_event_within_48h", False))) if market_context else False

    # Accept the existing day-scanner market_regime.json shape.
    if not risk or risk == "UNKNOWN":
        if bias in {"BULLISH", "RISK_ON"} or regime in {"BULLISH", "STRONG"}:
            risk = "SUPPORTIVE"
        elif bias in {"BEARISH", "RISK_OFF"} or regime in {"BEARISH", "WEAK"}:
            risk = "BEARISH"
        elif regime in {"NORMAL", "MIXED"} or bias in {"NEUTRAL", "MIXED"}:
            risk = "NEUTRAL"
        elif label:
            if "BULL" in label or "RISK ON" in label:
                risk = "SUPPORTIVE"
            elif "BEAR" in label or "RISK OFF" in label:
                risk = "BEARISH"
            else:
                risk = "NEUTRAL"
        else:
            risk = "UNKNOWN"

    if vix >= 25 or risk in {"HIGH", "BEARISH", "RISK_OFF"} or macro_risk:
        return {"score": 3.0, "label": "HIGH_RISK", "risk": risk, "vix": round4(vix), "macro_risk": macro_risk}
    if risk in {"SUPPORTIVE", "BULLISH", "RISK_ON"}:
        return {"score": 10.0, "label": "SUPPORTIVE", "risk": risk, "vix": round4(vix), "macro_risk": macro_risk}
    if risk in {"NEUTRAL", "LOW", "NORMAL", "MIXED"}:
        return {"score": 7.0, "label": "NEUTRAL", "risk": risk, "vix": round4(vix), "macro_risk": macro_risk}
    return {"score": 7.0, "label": "UNKNOWN", "risk": risk, "vix": round4(vix), "macro_risk": macro_risk}


def apply_ohlcv_proxy_context(
    smart_ctx: Dict[str, Any],
    news_ctx: Dict[str, Any],
    sector_ctx: Dict[str, Any],
    latest: pd.Series,
    intraday: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], List[str]]:
    """
    v1.1.6: Historical parquet/live scans often lack point-in-time news and
    market context files. Missing files must block top-tier Ready, but they
    should not make all Watch candidates disappear. This proxy uses only
    OHLCV-derived evidence so the historical scanner can still build a
    monitored shortlist:
      - accumulation proxy: VWAP hold, close strength, up-volume ratio,
        multi-day volume trend, MACD improvement, low fade
      - catalyst proxy: gap + volume + strong close, without severe news
      - market/sector proxy: stock relative strength from 5d/20d returns
    """
    warnings: List[str] = []

    close_loc = safe_float(latest.get("close_location_pct"), 50.0)
    close_to_high = safe_float(latest.get("close_to_high_pct"), -99.0)
    relv = safe_float(latest.get("rel_volume"), 0.0)
    vol3v10 = safe_float(latest.get("volume_3d_vs_10d"), 0.0)
    up_ratio = safe_float(latest.get("up_volume_ratio_5d"), 0.5)
    close_q3 = safe_float(latest.get("close_quality_3d"), 50.0)
    ret5 = safe_float(latest.get("ret_5d_pct"), 0.0)
    ret20 = safe_float(latest.get("ret_20d_pct"), 0.0)
    gap = safe_float(latest.get("gap_pct"), 0.0)
    mstate = macd_state(latest)
    close_above_vwap = bool(intraday.get("close_above_vwap"))
    late_fade = safe_float(intraday.get("late_fade_pct"), 0.0)

    accumulation_points = 0.0
    if close_above_vwap:
        accumulation_points += 2.0
    if close_loc >= 55:
        accumulation_points += 2.0
    if close_to_high >= -2.0:
        accumulation_points += 1.0
    if relv >= 1.0:
        accumulation_points += 1.5
    if vol3v10 >= 1.0:
        accumulation_points += 1.5
    if up_ratio >= 0.55:
        accumulation_points += 1.5
    if close_q3 >= 52:
        accumulation_points += 1.0
    if mstate in {"BULLISH_EXPANDING", "BULLISH", "CURLING_UP"}:
        accumulation_points += 1.5
    if late_fade <= -2.5:
        accumulation_points -= 2.0

    if smart_ctx.get("missing"):
        proxy_score = 8.5 + min(6.5, max(0.0, accumulation_points) * 0.75)
        bias = "PROXY_BULLISH" if accumulation_points >= 7.0 else "PROXY_NEUTRAL" if accumulation_points >= 4.0 else "UNKNOWN"
        smart_ctx = dict(smart_ctx)
        smart_ctx["score"] = round4(max(safe_float(smart_ctx.get("score")), min(15.0, proxy_score)))
        smart_ctx["bias"] = bias
        smart_ctx["label"] = "OHLCV accumulation proxy"
        smart_ctx["signals"] = (
            f"proxy_points={accumulation_points:.1f}; vwap={close_above_vwap}; "
            f"close_loc={close_loc:.1f}; relv={relv:.2f}; up_vol_ratio={up_ratio:.2f}; macd={mstate}"
        )
        smart_ctx["proxy"] = True
        smart_ctx["bearish"] = False
        smart_ctx["bullish"] = bias == "PROXY_BULLISH"
        warnings.append("PROXY_SMART_MONEY: using OHLCV accumulation proxy because smart-money file is missing.")

    if news_ctx.get("missing"):
        proxy_catalyst = (
            (2.0 <= gap <= 8.0 and relv >= 1.15 and close_loc >= 55 and late_fade > -2.5)
            or (relv >= 1.5 and close_loc >= 60 and close_above_vwap)
        )
        news_ctx = dict(news_ctx)
        if proxy_catalyst:
            news_ctx["score"] = max(safe_float(news_ctx.get("score")), 11.5)
            news_ctx["risk"] = "PROXY_CATALYST"
            news_ctx["summary"] = "OHLCV catalyst proxy: gap/volume/close strength; manual news check still required."
            news_ctx["positive"] = True
        else:
            news_ctx["score"] = max(safe_float(news_ctx.get("score")), 10.0)
            news_ctx["summary"] = "News/catalyst unknown; no severe news file risk found. Manual check required."
        news_ctx["proxy"] = True
        warnings.append("PROXY_NEWS: using OHLCV catalyst proxy because news-risk file is missing.")

    if sector_ctx.get("label") == "UNKNOWN":
        sector_ctx = dict(sector_ctx)
        if ret5 > 2 and ret20 > 4:
            proxy_score = 8.5
            label = "PROXY_STOCK_RS_STRONG"
        elif ret5 > 0 and ret20 > 0:
            proxy_score = 7.5
            label = "PROXY_STOCK_RS_NEUTRAL_POSITIVE"
        elif ret5 > 0:
            proxy_score = 6.5
            label = "PROXY_STOCK_RS_SHORT_TERM"
        else:
            proxy_score = 5.5
            label = "PROXY_MARKET_UNKNOWN"
        sector_ctx["score"] = max(safe_float(sector_ctx.get("score")), proxy_score)
        sector_ctx["label"] = label
        sector_ctx["proxy"] = True
        warnings.append("PROXY_MARKET_CONTEXT: using stock relative-strength proxy because market-context file is missing.")

    return smart_ctx, news_ctx, sector_ctx, warnings


def macd_state(latest: pd.Series) -> str:
    line = safe_float(latest.get("macd_line"))
    signal = safe_float(latest.get("macd_signal"))
    hist = safe_float(latest.get("macd_hist"))
    slope = safe_float(latest.get("macd_hist_slope"))
    if line > signal and hist > 0 and slope >= 0:
        return "BULLISH_EXPANDING"
    if line > signal and hist >= 0:
        return "BULLISH"
    if hist < 0 and slope < 0:
        return "BEARISH_DETERIORATING"
    if line < signal and hist < 0:
        return "BEARISH"
    if slope > 0:
        return "CURLING_UP"
    return "NEUTRAL"


def macd_momentum_score(latest: pd.Series, setup_type: str) -> Tuple[float, str, bool]:
    state = macd_state(latest)
    line = safe_float(latest.get("macd_line"))
    hist = safe_float(latest.get("macd_hist"))
    slope = safe_float(latest.get("macd_hist_slope"))
    score = 5.0
    bearish_block = False
    if state == "BULLISH_EXPANDING":
        score = 10.0
    elif state == "BULLISH":
        score = 8.0
    elif state == "CURLING_UP":
        score = 7.0 if setup_type in {"SWING_PULLBACK_SUPPORT_HOLD", "GAP_HOLD_SWING"} else 6.0
    elif state == "BEARISH":
        score = 3.0
        bearish_block = setup_type == "DAILY_BREAKOUT_CONTINUATION"
    elif state == "BEARISH_DETERIORATING":
        score = 1.0
        bearish_block = True
    if setup_type == "SWING_PULLBACK_SUPPORT_HOLD" and line > 0 and slope >= 0:
        score = min(10.0, score + 1.0)
    if setup_type == "GAP_HOLD_SWING" and hist > 0:
        score = min(10.0, score + 1.0)
    return round4(max(0.0, min(10.0, score))), state, bearish_block


def trend_sma_ema_score(latest: pd.Series) -> float:
    close = safe_float(latest.get("close"))
    sma50 = safe_float(latest.get("sma50"))
    sma200 = safe_float(latest.get("sma200"))
    ema9 = safe_float(latest.get("ema9"))
    ema20 = safe_float(latest.get("ema20"))
    ema21 = safe_float(latest.get("ema21"))
    score = 0.0
    if close > sma200 > 0:
        score += 4.0
    if close > sma50 > 0:
        score += 4.0
    if sma50 > sma200 > 0:
        score += 2.0
    if close > ema20 > 0 or close > ema21 > 0:
        score += 3.0
    if ema9 > ema20 > 0:
        score += 1.0
    if ema20 >= ema21 > 0:
        score += 1.0
    return round4(min(15.0, score))


def volume_accumulation_score(latest: pd.Series, setup_type: str) -> Tuple[float, str]:
    relv = safe_float(latest.get("rel_volume"))
    vol3v10 = safe_float(latest.get("volume_3d_vs_10d"))
    up_ratio = safe_float(latest.get("up_volume_ratio_5d"), 0.5)
    close_q3 = safe_float(latest.get("close_quality_3d"), 50.0)
    score = 0.0
    if relv >= 1.0:
        score += 2.0
    if relv >= 1.3:
        score += 2.0
    if relv >= 1.6:
        score += 1.0
    if vol3v10 >= 1.05:
        score += 2.0
    if up_ratio >= 0.58:
        score += 2.0
    if close_q3 >= 55:
        score += 1.0
    pattern = "ACCUMULATION" if score >= 7 else "NEUTRAL" if score >= 4 else "WEAK_VOLUME"
    return round4(min(10.0, score)), pattern


def risk_stop_score(entry: float, stop: float, atr: float, latest: pd.Series) -> Tuple[float, str, float, str]:
    if entry <= stop or atr <= 0:
        return 0.0, "BAD_STOP", 0.0, "UNKNOWN"
    stop_atr = (entry - stop) / atr
    gap_med = safe_float(latest.get("overnight_gap_abs_20d"), 0.0)
    stop_pct = (entry - stop) / entry * 100.0 if entry > 0 else 0.0
    gap_ratio = gap_med / stop_pct if stop_pct > 0 else 99.0
    if 0.7 <= stop_atr <= 1.35:
        score = 5.0
    elif 0.5 <= stop_atr < 0.7 or 1.35 < stop_atr <= 1.7:
        score = 3.5
    else:
        score = 1.5
    if gap_ratio > 1.5:
        score = min(score, 2.0)
        gap_label = "ELEVATED_GAP_RISK"
    elif gap_ratio > 1.0:
        gap_label = "MODERATE_GAP_RISK"
    else:
        gap_label = "NORMAL_GAP_RISK"
    atr_trend = "EXPANDING" if bool(latest.get("atr_expanding")) else "CONTRACTING_OR_STABLE"
    if atr_trend == "EXPANDING":
        score = max(0.0, score - 0.75)
    return round4(max(0.0, min(5.0, score))), gap_label, round4(stop_atr), atr_trend


def setup_quality_score(setup_type: str, latest: pd.Series, intraday: Dict[str, Any], rr: float, rr_gate: float, macd_bearish_block: bool) -> float:
    close_loc = safe_float(latest.get("close_location_pct"), 50.0)
    close_to_high = safe_float(latest.get("close_to_high_pct"), 0.0)
    relv = safe_float(latest.get("rel_volume"))
    late_fade = safe_float(intraday.get("late_fade_pct"))
    score = 0.0
    if rr >= rr_gate:
        score += 4.0
    if close_loc >= 60:
        score += 3.0
    if close_to_high >= -1.5:
        score += 2.0
    if relv >= 1.3:
        score += 2.0
    if late_fade > -2.0:
        score += 2.0
    if bool(intraday.get("close_above_vwap")):
        score += 2.0
    if macd_bearish_block:
        score = min(score, 6.0)
    return round4(min(15.0, score))


def institutional_grade(total: float, ready_allowed: bool, watch_allowed: bool, pool_candidate: bool = False) -> str:
    if ready_allowed and total >= SWING_READY_SCORE:
        return "A_READY"
    if watch_allowed and total >= SWING_WATCH_SCORE:
        return "B_WATCH"
    if pool_candidate and watch_allowed and total >= SWING_POOL_DISPLAY_SCORE:
        return "C_POOL_WATCH"
    if total >= 60:
        return "C_REJECT_REVIEW"
    return "D_REJECT"


def entry_strategy_for(setup_type: str) -> str:
    return {
        "DAILY_BREAKOUT_CONTINUATION": "Next regular session only: enter on clean second break above trigger after volume confirmation.",
        "SWING_PULLBACK_SUPPORT_HOLD": "Next regular session only: enter on EMA20/21 or VWAP hold/reclaim, not after-hours.",
        "GAP_HOLD_SWING": "Next regular session only: enter only if gap support/VWAP remains intact and no dilution/news trap appears.",
        "DAY_TO_SWING_PROMOTION": "EOD validation only; next session entry requires support/VWAP to remain intact.",
    }.get(setup_type, "Next regular session only after trigger confirmation.")


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
    rsi = safe_float(latest.get("rsi_14"))
    relv = safe_float(latest.get("rel_volume"), 0.0)
    ret5 = safe_float(latest.get("ret_5d_pct"), 0.0)
    ret20 = safe_float(latest.get("ret_20d_pct"), 0.0)
    close_loc = safe_float(latest.get("close_location_pct"), 50.0)

    trend = min(30.0, trend_sma_ema_score(latest) * 2.0)

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

    vol_acc, _ = volume_accumulation_score(latest, "GENERIC")
    vol = min(15.0, vol_acc * 1.5)

    close_quality = 0.0
    if close_loc >= 50:
        close_quality += 3
    if close_loc >= 65:
        close_quality += 3
    if bool(intraday.get("close_above_vwap")):
        close_quality += 2
    if safe_float(intraday.get("late_fade_pct")) > -1.5:
        close_quality += 2
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
    institutional: Dict[str, Any],
) -> Optional[SwingCandidate]:
    risk = entry - stop
    if entry <= 0 or stop <= 0 or risk <= 0:
        return None
    rr = (target1 - entry) / risk if target1 > entry else 0.0
    rr_gate = safe_float(institutional.get("setup_rr_gate"), SETUP_RR_GATES.get(setup_type, 1.6))
    total = (
        safe_float(institutional.get("trend_sma_ema_score"))
        + safe_float(institutional.get("setup_quality_score"))
        + safe_float(institutional.get("macd_momentum_score"))
        + safe_float(institutional.get("smart_money_confirmation_score"))
        + safe_float(institutional.get("news_catalyst_score"))
        + safe_float(institutional.get("volume_accumulation_score"))
        + safe_float(institutional.get("sector_market_score"))
        + safe_float(institutional.get("risk_stop_score"))
    )
    total = max(0.0, min(100.0, total))

    hard_reject = bool(institutional.get("hard_reject"))
    ready_allowed = bool(institutional.get("ready_allowed")) and rr >= rr_gate and not hard_reject
    watch_allowed = bool(institutional.get("watch_allowed")) and not hard_reject

    # v1.1.6 candidate-pool/proxy behavior:
    # Ready remains strict at 86. Watch can be created from two safe sources:
    #   1) named setup families at the normal Watch gate
    #   2) OHLCV-proxy/candidate-pool setups at a monitored Watch floor
    #      when no true hard reject exists. This fixes the zero-candidate
    #      problem caused by missing point-in-time news/market files while
    #      still blocking D_REJECT garbage.
    pool_candidate = setup_type == "INSTITUTIONAL_SWING_CANDIDATE_POOL" or bool(institutional.get("candidate_pool"))
    proxy_watch = bool(institutional.get("proxy_watch"))
    watch_floor = SWING_POOL_WATCH_SCORE if pool_candidate else SWING_PROXY_WATCH_SCORE if proxy_watch else SWING_WATCH_SCORE

    if pool_candidate or proxy_watch:
        mstate_for_pool = str(institutional.get("macd_state", "UNKNOWN")).upper()
        smart_bias_for_pool = str(institutional.get("smart_money_bias", "UNKNOWN")).upper()

        # v1.2.4 display-quality floor:
        # The Swing Desk is a shortlist, not a landfill. Broad pool/proxy rows
        # may support Watch, but only if they are dashboard-clean. Missing real
        # news/market data can keep a name as Watch, but weak R/R, bearish MACD,
        # or non-bullish accumulation cannot be used to fill slots.
        if total < SWING_WATCH_SCORE:
            return None

        if pool_candidate:
            if rr < 1.20:
                return None
            if "BULLISH" not in mstate_for_pool:
                return None
            if smart_bias_for_pool not in {"BULLISH", "PROXY_BULLISH", "ACCUMULATION", "POSITIVE"}:
                return None
        else:
            watch_rr_gate = safe_float(institutional.get("watch_rr_gate"), SETUP_WATCH_RR_GATES.get(setup_type, 1.2))
            if rr < watch_rr_gate:
                return None
            if mstate_for_pool == "BEARISH_DETERIORATING":
                return None

        if smart_bias_for_pool in {"BEARISH", "DISTRIBUTION", "NEGATIVE"}:
            return None

    if ready_allowed and total >= SWING_READY_SCORE:
        status = "SWING_READY"
    elif watch_allowed and total >= watch_floor:
        status = "SWING_WATCH"
    else:
        return None

    atrp = safe_float(latest.get("atr_pct"))
    suggested_risk = 1.0
    if atrp > 10:
        suggested_risk = 0.5
    elif atrp > 6:
        suggested_risk = 0.7

    grade = institutional_grade(total, ready_allowed, watch_allowed, pool_candidate=pool_candidate)
    reason = (
        f"{setup_type}: institutional_score={total:.1f}; "
        f"trend={safe_float(institutional.get('trend_sma_ema_score')):.1f}/15, "
        f"setup={safe_float(institutional.get('setup_quality_score')):.1f}/15, "
        f"macd={safe_float(institutional.get('macd_momentum_score')):.1f}/10 ({institutional.get('macd_state','')}), "
        f"smart_money={safe_float(institutional.get('smart_money_confirmation_score')):.1f}/20 ({institutional.get('smart_money_bias','')}), "
        f"news={safe_float(institutional.get('news_catalyst_score')):.1f}/15 ({institutional.get('news_risk','')}), "
        f"volume={safe_float(institutional.get('volume_accumulation_score')):.1f}/10 ({institutional.get('volume_pattern','')}), "
        f"sector={safe_float(institutional.get('sector_market_score')):.1f}/10, "
        f"risk={safe_float(institutional.get('risk_stop_score')):.1f}/5; "
        f"rr={rr:.2f} gate={rr_gate:.2f}; target_source={target_source}"
    )
    invalid = {
        "SWING_PULLBACK_SUPPORT_HOLD": "Invalid if EMA20/21, VWAP, or pullback support fails into close.",
        "DAILY_BREAKOUT_CONTINUATION": "Invalid if breakout level fails, MACD rolls over, or price closes back inside prior range.",
        "GAP_HOLD_SWING": "Invalid if gap-day low / VWAP support fails or negative catalyst appears.",
        "DAY_TO_SWING_PROMOTION": "Invalid if EOD support, VWAP, sector context, or overnight news risk fails.",
        "INSTITUTIONAL_SWING_CANDIDATE_POOL": "Invalid if EMA/VWAP support fails, MACD deteriorates, or negative news/smart-money risk appears.",
    }.get(setup_type, "Invalid if support or institutional confirmation breaks.")

    warnings2 = list(warnings or [])
    for w in institutional.get("warnings", []) or []:
        if w and w not in warnings2:
            warnings2.append(str(w))

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
        avg_dollar_volume_20d=round4(latest.get("avg_dollar_volume_20d")),
        atr_pct=round4(atrp),
        atr_tier=atr_tier(atrp),
        rsi_14=round4(latest.get("rsi_14")),
        sma20=round4(latest.get("sma20")),
        sma50=round4(latest.get("sma50")),
        sma200=round4(latest.get("sma200")),
        ema9=round4(latest.get("ema9")),
        ema20=round4(latest.get("ema20")),
        ema21=round4(latest.get("ema21")),
        macd_line=round4(latest.get("macd_line")),
        macd_signal=round4(latest.get("macd_signal")),
        macd_hist=round4(latest.get("macd_hist")),
        macd_state=str(institutional.get("macd_state", "")),
        macd_hist_slope=round4(latest.get("macd_hist_slope")),
        above_sma20=bool(latest["close"] > safe_float(latest.get("sma20"))),
        above_sma50=bool(latest["close"] > safe_float(latest.get("sma50"))),
        above_sma200=bool(latest["close"] > safe_float(latest.get("sma200"))),
        above_ema20=bool(latest["close"] > safe_float(latest.get("ema20"))),
        above_ema21=bool(latest["close"] > safe_float(latest.get("ema21"))),
        trend_sma_ema_score=round4(institutional.get("trend_sma_ema_score")),
        setup_quality_score=round4(institutional.get("setup_quality_score")),
        macd_momentum_score=round4(institutional.get("macd_momentum_score")),
        smart_money_confirmation_score=round4(institutional.get("smart_money_confirmation_score")),
        news_catalyst_score=round4(institutional.get("news_catalyst_score")),
        volume_accumulation_score=round4(institutional.get("volume_accumulation_score")),
        sector_market_score=round4(institutional.get("sector_market_score")),
        risk_stop_score=round4(institutional.get("risk_stop_score")),
        institutional_grade=grade,
        setup_rr_gate=round4(rr_gate),
        smart_money_score=round4(institutional.get("smart_money_raw_score")),
        smart_money_bias=str(institutional.get("smart_money_bias", "")),
        smart_money_label=str(institutional.get("smart_money_label", "")),
        smart_money_signals=str(institutional.get("smart_money_signals", "")),
        news_risk=str(institutional.get("news_risk", "")),
        news_score=round4(institutional.get("news_catalyst_score")),
        news_summary=str(institutional.get("news_summary", "")),
        positive_catalyst=bool(institutional.get("positive_catalyst")),
        negative_catalyst=bool(institutional.get("negative_catalyst")),
        sector_context=str(institutional.get("sector_context", "")),
        sector_score=round4(institutional.get("sector_market_score")),
        volume_pattern=str(institutional.get("volume_pattern", "")),
        close_to_high_pct=round4(latest.get("close_to_high_pct")),
        close_quality_3d=round4(latest.get("close_quality_3d")),
        atr_trend=str(institutional.get("atr_trend", "")),
        overnight_gap_risk=str(institutional.get("overnight_gap_risk", "")),
        stop_atr_multiple=round4(institutional.get("stop_atr_multiple")),
        entry_strategy=entry_strategy_for(setup_type),
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
        earnings_risk=str(institutional.get("earnings_risk", "")),
        vwap=round4(intraday.get("vwap")),
        close_above_vwap=bool(intraday.get("close_above_vwap")),
        late_fade_pct=round4(intraday.get("late_fade_pct")),
        panic_selling=bool(intraday.get("panic_selling")),
        reason=reason,
        invalid_if=invalid,
        blockers="; ".join(institutional.get("blockers", []) or []),
        warnings="; ".join(warnings2),
    )


def scan_symbol(
    symbol: str,
    daily_raw: pd.DataFrame,
    latest_intraday: pd.DataFrame,
    args: argparse.Namespace,
    earnings_map: Dict[str, str],
    smart_money_map: Optional[Dict[str, Dict[str, Any]]] = None,
    news_map: Optional[Dict[str, Dict[str, Any]]] = None,
    market_context: Optional[Dict[str, Any]] = None,
) -> Tuple[List[SwingCandidate], List[str]]:
    """
    Institutional tiered swing scan.

    v1.1.6 correction:
    - True severe risks remain hard rejects.
    - Soft/incomplete institutional data downgrades to Watch instead of causing
      an empty dashboard.
    - Ready and Watch use separate R/R/setup gates.
    """
    hard_blockers: List[str] = []
    soft_warnings: List[str] = []
    diagnostics: List[str] = []
    candidates: List[SwingCandidate] = []

    daily = add_daily_indicators(daily_raw)
    min_history = int(getattr(args, "min_warmup_days", 220) or 220)
    if daily.empty or len(daily) < min_history:
        return [], [f"HARD_REJECT: Not enough daily history for SMA200/warmup ({len(daily)} < {min_history})."]

    latest = daily.iloc[-1]
    close = safe_float(latest["close"])
    avg_vol = safe_float(latest.get("avg_volume_20d"))
    atrp = safe_float(latest.get("atr_pct"))
    sma50 = safe_float(latest.get("sma50"))
    sma200 = safe_float(latest.get("sma200"))
    ema20 = safe_float(latest.get("ema20"))
    ema21 = safe_float(latest.get("ema21"))
    rsi = safe_float(latest.get("rsi_14"))
    relv = safe_float(latest.get("rel_volume"))

    min_price = safe_float(getattr(args, "min_price", 5.0), 5.0)
    max_price = safe_float(getattr(args, "max_price", 200.0), 200.0)
    min_avg_volume = safe_float(getattr(args, "min_avg_volume", 500_000.0), 500_000.0)
    min_avg_dollar_volume = safe_float(getattr(args, "min_avg_dollar_volume", 20_000_000.0), 20_000_000.0)
    avg_dollar_volume = safe_float(latest.get("avg_dollar_volume_20d"), close * avg_vol)
    max_atr = safe_float(getattr(args, "max_atr_pct", 15.0), 15.0)

    # True hard rejects: universe/eligibility and severe overnight risk only.
    # v1.2.1 user discipline:
    # - Tradeable price range is mandatory: $5-$200.
    # - Liquidity uses share volume plus dollar-volume, so liquid higher-priced
    #   names inside the user's price range are not rejected only because share
    #   count is below 1M.
    if close < min_price or close > max_price:
        hard_blockers.append(f"HARD_REJECT: Price outside range {min_price}-{max_price}.")
    if avg_vol < min_avg_volume and avg_dollar_volume < min_avg_dollar_volume:
        hard_blockers.append(
            f"HARD_REJECT: Liquidity below floor: avg_volume {avg_vol:.0f} < {min_avg_volume:.0f} "
            f"and avg_dollar_volume ${avg_dollar_volume:,.0f} < ${min_avg_dollar_volume:,.0f}."
        )
    if close < sma50 and close < sma200:
        hard_blockers.append("HARD_REJECT: Below BOTH SMA50 and SMA200.")
    if atrp > max_atr:
        hard_blockers.append(f"HARD_REJECT: ATR% {atrp:.2f} above max {max_atr}.")
    if pd.isna(latest.get("sma200")) or sma200 <= 0:
        hard_blockers.append("HARD_REJECT: SMA200 unavailable.")

    erisk, ewarn = earnings_risk_for(symbol, str(latest["date_et"]), earnings_map)
    if erisk == "BLOCKED_UPCOMING_EARNINGS":
        hard_blockers.append("HARD_REJECT: " + (ewarn or "Upcoming earnings inside hold window."))
    elif ewarn:
        soft_warnings.append(ewarn)

    intraday = calc_intraday_metrics(latest_intraday)

    # v1.1.6 retained bug fix:
    # close_loc and late_fade are needed by the panic/distribution hard gate.
    # They must be initialized before that gate runs. v1.1.4 initialized them
    # later, which caused UnboundLocalError for valid non-hard-rejected symbols.
    high = safe_float(latest["high"])
    low = safe_float(latest["low"])
    open_ = safe_float(latest["open"])
    gap = safe_float(latest.get("gap_pct"))
    close_loc = safe_float(latest.get("close_location_pct"), 50.0)
    late_fade = safe_float(intraday.get("late_fade_pct"), 0.0)
    atr = safe_float(latest.get("atr14"))

    smart_ctx = smart_money_context(symbol, smart_money_map or {})
    news_ctx = news_context(symbol, news_map or {}, erisk)
    sector_ctx = sector_market_context(symbol, market_context or {}, latest)
    smart_ctx, news_ctx, sector_ctx, proxy_warnings = apply_ohlcv_proxy_context(
        smart_ctx,
        news_ctx,
        sector_ctx,
        latest,
        intraday,
    )
    soft_warnings.extend(proxy_warnings)

    if news_ctx.get("severe"):
        hard_blockers.append(f"HARD_REJECT: Severe news/catalyst risk: {news_ctx.get('risk')}.")

    # Panic close is a hard reject only when it is confirmed by additional
    # technical/institutional weakness. Otherwise it is a downgrade warning.
    # v1.1.6: panic/distribution must be a confirmed cluster before it
    # becomes a hard reject. A mild late fade is a downgrade, not a kill switch.
    confirmed_panic = bool(intraday.get("panic_selling")) and (
        close_loc < 35
        and late_fade <= -2.5
        and close < ema20
        and close < ema21
        and not bool(intraday.get("close_above_vwap"))
        and (bool(smart_ctx.get("bearish")) or str(macd_state(latest)).startswith("BEARISH") or close < sma50)
    )
    if confirmed_panic:
        hard_blockers.append("HARD_REJECT: Confirmed panic/distribution close.")
    elif bool(intraday.get("panic_selling")) or safe_float(intraday.get("late_fade_pct")) <= -2.25:
        soft_warnings.append("SOFT_DOWNGRADE: Heavy late-day bearish close; Watch only unless reclaimed.")

    if smart_ctx.get("bearish") and close < ema20 and close < ema21 and not bool(intraday.get("close_above_vwap")):
        hard_blockers.append("HARD_REJECT: Bearish smart money while below EMA20/21 and VWAP.")

    if hard_blockers:
        return [], hard_blockers + soft_warnings

    scores = score_common(latest, intraday)
    if atr <= 0:
        return [], ["HARD_REJECT: ATR unavailable."]

    support_candidates = [low, ema20, ema21, safe_float(latest.get("sma20")), safe_float(intraday.get("vwap"))]
    support_candidates = [x for x in support_candidates if x and x > 0 and x < close]
    support = max(support_candidates) if support_candidates else low
    stop = max(0.01, support - 0.55 * atr)

    entry = max(high + 0.05 * atr, close * 1.003)
    target1, target2, target_source = recent_resistance_targets(daily, entry, entry - stop)
    rr = (target1 - entry) / (entry - stop) if entry > stop else 0

    risk_score, gap_risk_label, stop_atr, atr_trend_label = risk_stop_score(entry, stop, atr, latest)
    support_stop_score = 15.0 if 0.7 <= stop_atr <= 1.35 else 9.0 if 0.5 <= stop_atr <= 1.7 else 5.0
    target_room_score = 10.0 if rr >= 2 else 8.0 if rr >= 1.5 else 6.0 if rr >= 1.2 else 2.0
    vol_score, volume_pattern = volume_accumulation_score(latest, "GENERIC")
    trend_score = trend_sma_ema_score(latest)

    prev20_high = safe_float(daily["high"].iloc[-21:-1].max()) if len(daily) >= 21 else 0.0
    close_above_vwap = bool(intraday.get("close_above_vwap"))
    near_ema20 = abs(close - ema20) / close * 100 <= max(atrp * 0.75, 1.25) if close > 0 and ema20 > 0 else False
    near_ema21 = abs(close - ema21) / close * 100 <= max(atrp * 0.75, 1.25) if close > 0 and ema21 > 0 else False
    holds_tactical_support = close_above_vwap or close > ema20 or close > ema21 or near_ema20 or near_ema21
    above_major_trend = close > sma50 and close > sma200
    not_broken_major_trend = close > sma50 or close > sma200

    def institutional_for_setup(setup_type: str, setup_seen: bool, ready_shape: bool) -> Dict[str, Any]:
        rr_gate = SETUP_RR_GATES.get(setup_type, 1.6)
        watch_rr_gate = SETUP_WATCH_RR_GATES.get(setup_type, 1.2)
        macd_score, mstate, macd_block = macd_momentum_score(latest, setup_type)
        setup_score = setup_quality_score(setup_type, latest, intraday, rr, rr_gate, macd_block)

        candidate_pool = setup_type == "INSTITUTIONAL_SWING_CANDIDATE_POOL"

        # Give forming Watch setups credit for valid structure even if the
        # stricter Ready shape is not complete yet.
        if setup_seen and not ready_shape:
            setup_score = max(setup_score, 9.0)
            if rr >= watch_rr_gate:
                setup_score = max(setup_score, 10.5)
            if holds_tactical_support and close_loc >= 50:
                setup_score = max(setup_score, 11.0)
            if candidate_pool:
                # v1.2.3 true candidate pool:
                # This is a monitored shortlist lane, not a trade signal. It
                # should surface survivors with acceptable trend/support/target
                # structure even when they are not yet clean breakout/pullback/gap
                # setups. Institutional layers still decide Ready vs Watch.
                setup_score = max(setup_score, 13.0 if rr >= watch_rr_gate and close_loc >= 42 else 11.5)

        hard_reject = bool(news_ctx.get("severe")) or (
            smart_ctx.get("bearish")
            and setup_score < 10
            and (not holds_tactical_support or str(mstate).startswith("BEARISH"))
        )

        total_preview = (
            trend_score
            + setup_score
            + macd_score
            + safe_float(smart_ctx.get("score"))
            + safe_float(news_ctx.get("score"))
            + vol_score
            + safe_float(sector_ctx.get("score"))
            + risk_score
        )

        ready_allowed = True
        watch_allowed = True

        # Ready is strict. Watch is tiered.
        if rr < rr_gate:
            ready_allowed = False
        if rr < watch_rr_gate:
            watch_allowed = False
        if not ready_shape:
            ready_allowed = False
        if not setup_seen:
            watch_allowed = False
            ready_allowed = False
        if smart_ctx.get("bearish"):
            ready_allowed = False
        if news_ctx.get("negative") or news_ctx.get("risk") in {"NEGATIVE", "HIGH"}:
            ready_allowed = False
        if sector_ctx.get("label") == "HIGH_RISK":
            ready_allowed = False
        if macd_block:
            ready_allowed = False
        if late_fade <= -2.25:
            ready_allowed = False
        if close_loc < 55:
            ready_allowed = False
        if not holds_tactical_support:
            ready_allowed = False
        if not above_major_trend:
            ready_allowed = False

        # Missing institutional context prevents top-tier Ready, but not Watch.
        if smart_ctx.get("missing") or news_ctx.get("missing") or sector_ctx.get("label") == "UNKNOWN":
            ready_allowed = False

        inst_warnings: List[str] = []
        if not ready_shape and setup_seen:
            inst_warnings.append("SOFT_DOWNGRADE: Setup forming; Ready trigger/confirmation not fully complete.")
        if rr < rr_gate and rr >= watch_rr_gate:
            inst_warnings.append(f"SOFT_DOWNGRADE: R/R {rr:.2f} below Ready gate {rr_gate:.2f}, acceptable only as Watch.")
        if smart_ctx.get("missing"):
            inst_warnings.append("SOFT_DOWNGRADE: Smart-money data missing/neutral; manual confirmation required.")
        elif smart_ctx.get("bearish"):
            inst_warnings.append("SOFT_DOWNGRADE: Smart-money bias bearish; Ready blocked.")
        if news_ctx.get("missing"):
            inst_warnings.append("SOFT_DOWNGRADE: News/catalyst unknown; manual check required.")
        if sector_ctx.get("label") == "UNKNOWN":
            inst_warnings.append("SOFT_DOWNGRADE: Market/sector context unknown; manual check required.")
        if gap_risk_label != "NORMAL_GAP_RISK":
            inst_warnings.append(f"SOFT_DOWNGRADE: {gap_risk_label}; overnight gap risk may exceed planned stop.")
        if atr_trend_label == "EXPANDING":
            inst_warnings.append("SOFT_DOWNGRADE: ATR expanding; reduce size or require stronger confirmation.")
        if str(mstate).startswith("BEARISH"):
            inst_warnings.append(f"SOFT_DOWNGRADE: MACD state {mstate}; momentum not ideal.")
        # OHLCV proxies can support Watch when real smart/news/market files
        # are missing, but v1.2.4 no longer allows sub-72 or weak-R/R proxy
        # rows to appear on the final dashboard.
        proxy_watch = (
            setup_seen
            and not hard_reject
            and rr >= max(SWING_POOL_DISPLAY_RR_MIN if candidate_pool else watch_rr_gate, watch_rr_gate)
            and total_preview >= SWING_WATCH_SCORE
            and (
                smart_ctx.get("proxy")
                or news_ctx.get("proxy")
                or sector_ctx.get("proxy")
                or candidate_pool
            )
            and not str(mstate).startswith("BEARISH")
        )

        score_floor = SWING_POOL_WATCH_SCORE if candidate_pool else SWING_PROXY_WATCH_SCORE if proxy_watch else SWING_WATCH_SCORE
        if total_preview < score_floor:
            inst_warnings.append(f"SCORE_REJECT: Institutional score {total_preview:.1f} below Watch gate {score_floor:.1f}.")
        if candidate_pool:
            inst_warnings.append("POOL_WATCH: Broad candidate-pool name; requires manual confirmation before any swing entry.")
        elif proxy_watch:
            inst_warnings.append("PROXY_WATCH: OHLCV proxy-supported Watch; manual smart/news/sector confirmation required.")

        return {
            "trend_sma_ema_score": trend_score,
            "setup_quality_score": setup_score,
            "macd_momentum_score": macd_score,
            "smart_money_confirmation_score": safe_float(smart_ctx.get("score")),
            "news_catalyst_score": safe_float(news_ctx.get("score")),
            "volume_accumulation_score": vol_score,
            "sector_market_score": safe_float(sector_ctx.get("score")),
            "risk_stop_score": risk_score,
            "setup_rr_gate": rr_gate,
            "watch_rr_gate": watch_rr_gate,
            "macd_state": mstate,
            "smart_money_raw_score": smart_ctx.get("raw_score", 0.0),
            "smart_money_bias": smart_ctx.get("bias", "UNKNOWN"),
            "smart_money_label": smart_ctx.get("label", ""),
            "smart_money_signals": smart_ctx.get("signals", ""),
            "news_risk": news_ctx.get("risk", "UNKNOWN"),
            "news_summary": news_ctx.get("summary", ""),
            "positive_catalyst": bool(news_ctx.get("positive")),
            "negative_catalyst": bool(news_ctx.get("negative")),
            "sector_context": sector_ctx.get("label", "UNKNOWN"),
            "volume_pattern": volume_pattern,
            "overnight_gap_risk": gap_risk_label,
            "stop_atr_multiple": stop_atr,
            "atr_trend": atr_trend_label,
            "earnings_risk": erisk,
            "hard_reject": hard_reject,
            "ready_allowed": ready_allowed,
            "watch_allowed": watch_allowed,
            "candidate_pool": candidate_pool,
            "proxy_watch": proxy_watch,
            "warnings": inst_warnings,
            "blockers": hard_blockers,
            "total_preview": round4(total_preview),
        }

    def add_setup(setup_type: str, setup_seen: bool, ready_shape: bool, custom_stop: Optional[float] = None) -> None:
        if not setup_seen:
            diagnostics.append(f"NO_SETUP: {setup_type} watch conditions not met.")
            return
        inst = institutional_for_setup(setup_type, setup_seen, ready_shape)
        diagnostics.append(f"SETUP_SEEN: {setup_type}.")
        if inst.get("hard_reject"):
            diagnostics.append(f"HARD_REJECT: {setup_type} failed institutional hard gate.")
            return
        cand = build_candidate(
            symbol,
            setup_type,
            "SWING_READY" if ready_shape else "SWING_WATCH",
            latest,
            daily,
            intraday,
            scores,
            support_stop_score,
            target_room_score,
            entry,
            custom_stop if custom_stop is not None else stop,
            target1,
            target2,
            target_source,
            soft_warnings,
            inst,
        )
        if cand:
            candidates.append(cand)
        else:
            diagnostics.extend(inst.get("warnings", []) or [])

    breakout_ready = (
        prev20_high > 0
        and close > prev20_high * 1.001
        and above_major_trend
        and relv >= 1.30
        and close_loc >= 60
        and rr >= SETUP_RR_GATES["DAILY_BREAKOUT_CONTINUATION"]
    )
    breakout_seen = (
        prev20_high > 0
        and close >= prev20_high * 0.985
        and not_broken_major_trend
        and relv >= 0.85
        and close_loc >= 45
        and rr >= SETUP_WATCH_RR_GATES["DAILY_BREAKOUT_CONTINUATION"] * 0.75
        and late_fade > -4.0
    )
    add_setup("DAILY_BREAKOUT_CONTINUATION", breakout_seen, breakout_ready)

    pullback_ready = (
        above_major_trend
        and 45 <= rsi <= 65
        and close_loc >= 55
        and holds_tactical_support
        and rr >= SETUP_RR_GATES["SWING_PULLBACK_SUPPORT_HOLD"]
    )
    pullback_seen = (
        not_broken_major_trend
        and 38 <= rsi <= 72
        and close_loc >= 40
        and (holds_tactical_support or abs(close - sma50) / close * 100 <= max(atrp, 1.75))
        and rr >= SETUP_WATCH_RR_GATES["SWING_PULLBACK_SUPPORT_HOLD"] * 0.75
        and late_fade > -4.0
    )
    add_setup("SWING_PULLBACK_SUPPORT_HOLD", pullback_seen, pullback_ready)

    gap_ready = (
        2.0 <= gap <= 8.0
        and close_loc >= 60
        and close_above_vwap
        and late_fade > -2.0
        and rr >= SETUP_RR_GATES["GAP_HOLD_SWING"]
    )
    gap_seen = (
        1.0 <= gap <= 12.0
        and close_loc >= 45
        and (close_above_vwap or close > ema20 or close > ema21 or holds_tactical_support)
        and late_fade > -4.0
        and rr >= SETUP_WATCH_RR_GATES["GAP_HOLD_SWING"] * 0.75
    )
    gap_stop = max(0.01, min(open_, low, support) - 0.45 * atr)
    add_setup("GAP_HOLD_SWING", gap_seen, gap_ready, custom_stop=gap_stop)

    # v1.2.3 true candidate-pool layer:
    # After hard rejects, build a real monitored Watch pool from eligible
    # survivors. This fixes the "perfect setup or zero candidates" problem.
    # It is not a trade signal; it is the broad shortlist that institutional
    # scoring ranks/downgrades before the dashboard shows top names.
    pool_quality = 0
    pool_reasons: List[str] = []
    if not_broken_major_trend:
        pool_quality += 1
    else:
        pool_reasons.append("not above SMA50 or SMA200")
    if holds_tactical_support:
        pool_quality += 1
    else:
        pool_reasons.append("not holding EMA/VWAP tactical support")
    if close_loc >= 35:
        pool_quality += 1
    else:
        pool_reasons.append(f"close_location {close_loc:.1f} < 35")
    if late_fade > -5.0:
        pool_quality += 1
    else:
        pool_reasons.append(f"late_fade {late_fade:.1f} <= -5")
    if rr >= SETUP_WATCH_RR_GATES["INSTITUTIONAL_SWING_CANDIDATE_POOL"]:
        pool_quality += 1
    else:
        pool_reasons.append(f"rr {rr:.2f} < pool gate {SETUP_WATCH_RR_GATES['INSTITUTIONAL_SWING_CANDIDATE_POOL']:.2f}")
    if trend_score >= 6.0:
        pool_quality += 1
    if vol_score >= 3.0 or relv >= 0.75:
        pool_quality += 1
    if not str(macd_state(latest)).startswith("BEARISH_DETERIORATING"):
        pool_quality += 1
    if safe_float(latest.get("ret_5d_pct")) > -2.0:
        pool_quality += 1

    pool_seen = (
        rr >= SETUP_WATCH_RR_GATES["INSTITUTIONAL_SWING_CANDIDATE_POOL"]
        and close_loc >= 35
        and late_fade > -5.0
        and pool_quality >= 4
        and (
            not_broken_major_trend
            or holds_tactical_support
            or trend_score >= 6.0
        )
    )
    if pool_seen and not candidates:
        diagnostics.append(f"POOL_SEEN: INSTITUTIONAL_SWING_CANDIDATE_POOL quality={pool_quality}.")
        add_setup("INSTITUTIONAL_SWING_CANDIDATE_POOL", True, False)
    elif not candidates:
        diagnostics.append("POOL_REJECT: " + ("; ".join(pool_reasons[:4]) if pool_reasons else f"quality {pool_quality} below pool threshold"))

    if candidates:
        notes = soft_warnings[:]
        if pool_seen:
            notes.append("POOL_SEEN: INSTITUTIONAL_SWING_CANDIDATE_POOL.")
        return candidates, notes

    # Return diagnostic reasons so summary can show whether the engine saw
    # near-setups, failed score gates, or had no structure at all.
    notes = diagnostics + soft_warnings
    if not notes:
        notes = ["NO_SETUP: No tiered swing setup passed Watch conditions."]
    return [], notes



def candidate_rank(c: SwingCandidate) -> Tuple[float, float, float, float, float, float, float]:
    """
    Ranking tuple for choosing one dashboard row per ticker.

    Priority:
    1. Ready/Active before Watch.
    2. Institutional score.
    3. Setup priority. A confirmed breakout/pullback/gap setup is preferred
       over the broad pool if quality is otherwise similar.
    4. Reward/risk.
    5. MACD/structure and fewer warnings/blockers.
    """
    return (
        float(STATUS_RANK.get(c.swing_status, 0)),
        safe_float(c.score),
        float(SETUP_PRIORITY.get(c.setup_type, 0)),
        safe_float(c.reward_risk),
        safe_float(c.macd_momentum_score),
        -float(len(c.blockers or "")),
        -float(len(c.warnings or "")),
    )


def _append_note(existing: str, note: str) -> str:
    existing = (existing or "").strip()
    note = (note or "").strip()
    if not note:
        return existing
    if not existing:
        return note
    if note in existing:
        return existing
    return existing + "; " + note



def display_quality_allowed(c: SwingCandidate) -> bool:
    """
    Final dashboard quality floor.

    v1.2.3 correctly produced unique tickers, but still allowed C/D grade
    filler rows and broad-pool names with weak R/R. v1.2.4 enforces that the
    visible Swing Desk contains only B_WATCH or A_READY quality rows. Fewer
    than 10 names is acceptable when the market does not provide 10 clean
    candidates.
    """
    grade = str(c.institutional_grade or "").upper()
    if grade not in {"A_READY", "B_WATCH"}:
        return False

    score = safe_float(c.score)
    rr = safe_float(c.reward_risk)
    setup = str(c.setup_type or "")
    macd = str(c.macd_state or "").upper()
    smart_bias = str(c.smart_money_bias or "").upper()

    if c.swing_status == "SWING_READY":
        return score >= SWING_READY_SCORE

    if c.swing_status != "SWING_WATCH":
        return False

    if score < SWING_WATCH_SCORE:
        return False

    if setup == "INSTITUTIONAL_SWING_CANDIDATE_POOL":
        if rr < 1.20:
            return False
        if "BULLISH" not in macd:
            return False
        if smart_bias not in {"BULLISH", "PROXY_BULLISH", "ACCUMULATION", "POSITIVE"}:
            return False
        return True

    watch_rr_gate = SETUP_WATCH_RR_GATES.get(setup, 1.20)
    if rr < watch_rr_gate:
        return False
    if macd == "BEARISH_DETERIORATING":
        return False
    if smart_bias in {"BEARISH", "DISTRIBUTION", "NEGATIVE"}:
        return False
    return True


def dedupe_candidates(candidates: Sequence[SwingCandidate]) -> List[SwingCandidate]:
    """
    Build the final dashboard shortlist as one row per ticker per latest date.

    Earlier versions deduped by (symbol, setup_type, date), so one ticker could
    consume multiple Swing Desk slots when it qualified for multiple setup
    families. v1.2.3 keeps only the best setup per symbol/date and records the
    alternate setup names in the selected row's warnings/reason.
    """
    grouped: Dict[Tuple[str, str], List[SwingCandidate]] = {}
    for c in candidates:
        key = (str(c.symbol).upper(), str(c.latest_date_et))
        grouped.setdefault(key, []).append(c)

    winners: List[SwingCandidate] = []
    for (_symbol, _date), rows in grouped.items():
        rows_sorted = sorted(rows, key=candidate_rank, reverse=True)
        winner = rows_sorted[0]

        alternates = [r for r in rows_sorted[1:] if r.setup_type != winner.setup_type]
        if alternates:
            alt_labels = []
            seen_alt = set()
            for alt in alternates:
                label = f"{alt.setup_type} score={safe_float(alt.score):.1f} rr={safe_float(alt.reward_risk):.2f}"
                if label not in seen_alt:
                    seen_alt.add(label)
                    alt_labels.append(label)
            alt_note = "Alternate setups also detected: " + " | ".join(alt_labels[:4])
            winner.warnings = _append_note(winner.warnings, alt_note)
            winner.reason = _append_note(winner.reason, alt_note)

        winners.append(winner)

    winners.sort(key=lambda x: (
        STATUS_RANK.get(x.swing_status, 0),
        safe_float(x.score),
        SETUP_PRIORITY.get(x.setup_type, 0),
        safe_float(x.reward_risk),
        safe_float(x.macd_momentum_score),
    ), reverse=True)
    return winners


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


def read_daily_hourly_symbol(
    daily_path: Path,
    hourly_path: Optional[Path],
    m15_path: Optional[Path] = None,
    m5_path: Optional[Path] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Read pre-aggregated live swing inputs.

    Daily parquet drives the swing setup engine.
    1H confirms structure/continuation.
    15m confirms entry structure.
    5m provides exact latest regular-session trigger/price and avoids chasing.
    The scanner uses the most granular available regular-session file for
    latest_day metrics, falling back to 15m, then 1H, then daily.
    """
    daily = pd.read_parquet(daily_path)
    daily = standardize_ohlcv_columns(infer_datetime_columns(daily))
    if "date_et" not in daily.columns:
        daily["date_et"] = _date_et_from_dt(daily.get("dt_et", daily.index), daily.index)
    daily["date_et"] = daily["date_et"].astype(str)
    daily = daily.dropna(subset=["date_et", "open", "high", "low", "close"]).sort_values("date_et").reset_index(drop=True)

    latest_day = pd.DataFrame()

    def read_intraday(path: Optional[Path]) -> pd.DataFrame:
        if path is None or not path.exists():
            return pd.DataFrame()
        frame = pd.read_parquet(path)
        frame = standardize_ohlcv_columns(infer_datetime_columns(frame))
        if "date_et" not in frame.columns:
            frame["date_et"] = _date_et_from_dt(frame.get("dt_et", frame.index), frame.index)
        frame["date_et"] = frame["date_et"].astype(str)
        if "is_regular_session" in frame.columns:
            regular = frame[frame["is_regular_session"] == True].copy()
            if not regular.empty:
                frame = regular
        return frame.sort_values("dt_et" if "dt_et" in frame.columns else "date_et").reset_index(drop=True)

    latest_daily_date = str(daily["date_et"].dropna().max()) if not daily.empty else ""

    for candidate_path in [m5_path, m15_path, hourly_path]:
        frame = read_intraday(candidate_path)
        if frame.empty:
            continue
        if latest_daily_date:
            latest_day = frame[frame["date_et"].astype(str) == latest_daily_date].copy()
        if latest_day.empty:
            latest_intraday_date = str(frame["date_et"].dropna().max())
            latest_day = frame[frame["date_et"].astype(str) == latest_intraday_date].copy()
        if not latest_day.empty:
            break

    if latest_day.empty:
        # Safe fallback: create a one-row pseudo-intraday day from the latest
        # daily candle. This preserves scanner stability if intraday files are
        # missing, but Ready should still require external confirmation layers.
        latest = daily.iloc[-1].to_dict() if not daily.empty else {}
        latest_day = pd.DataFrame([latest]) if latest else pd.DataFrame()

    gc.collect()
    return daily, latest_day


def daily_hourly_sources(
    daily_root: Path,
    hourly_root: Optional[Path],
    symbols: Sequence[str],
    limit: Optional[int],
    m15_root: Optional[Path] = None,
    m5_root: Optional[Path] = None,
) -> List[Tuple[str, Path, Optional[Path], Optional[Path], Optional[Path]]]:
    daily_files = sorted(daily_root.rglob("*.parquet"))
    sym_filter = {s.upper() for s in symbols} if symbols else set()

    def build_map(root: Optional[Path]) -> Dict[str, Path]:
        out: Dict[str, Path] = {}
        if root is not None and root.exists():
            for p in root.rglob("*.parquet"):
                out[normalize_symbol_from_file(p)] = p
        return out

    hourly_map = build_map(hourly_root)
    m15_map = build_map(m15_root)
    m5_map = build_map(m5_root)

    out: List[Tuple[str, Path, Optional[Path], Optional[Path], Optional[Path]]] = []
    for df in daily_files:
        sym = normalize_symbol_from_file(df)
        if sym_filter and sym not in sym_filter:
            continue
        out.append((sym, df, hourly_map.get(sym), m15_map.get(sym), m5_map.get(sym)))
    if limit:
        out = out[:limit]
    return out


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
    smart_money_map = load_smart_money_map(getattr(args, "smart_money_file", None))
    news_map = load_news_risk_map(getattr(args, "news_risk_file", None))
    market_context = load_market_context(getattr(args, "market_context_file", None))
    explicit_symbols = sorted(set(parse_symbols_arg(args.symbols) + load_symbols_from_file(args.symbols_file)))

    candidates: List[SwingCandidate] = []
    rejected: Dict[str, int] = {}
    warnings_count: Dict[str, int] = {}
    file_errors: Dict[str, str] = {}

    universe_meta: Dict[str, Any] = {"mode": "parquet_files"}

    if args.source == "daily-hourly" or args.daily_root:
        if not args.daily_root:
            raise SystemExit("--daily-root is required when --source daily-hourly")
        daily_root = Path(args.daily_root)
        hourly_root = Path(args.hourly_root) if args.hourly_root else None
        m15_root = Path(args.m15_root) if getattr(args, "m15_root", None) else None
        m5_root = Path(args.m5_root) if getattr(args, "m5_root", None) else None
        if not daily_root.exists():
            raise FileNotFoundError(f"daily root not found: {daily_root}")
        if hourly_root is not None and not hourly_root.exists():
            raise FileNotFoundError(f"hourly root not found: {hourly_root}")
        if m15_root is not None and not m15_root.exists():
            raise FileNotFoundError(f"15m root not found: {m15_root}")
        if m5_root is not None and not m5_root.exists():
            raise FileNotFoundError(f"5m root not found: {m5_root}")
        sources = daily_hourly_sources(daily_root, hourly_root, explicit_symbols, args.limit_files, m15_root=m15_root, m5_root=m5_root)
        total = len(sources)
        universe_meta = {
            "mode": "daily_hourly_mtf_files",
            "daily_root": str(daily_root),
            "hourly_root": str(hourly_root or ""),
            "m15_root": str(m15_root or ""),
            "m5_root": str(m5_root or ""),
            "note": "Live Swing scanner uses pre-aggregated Daily + 1H + 15m + 5m bars. Raw 1m is preprocessing source only.",
        }
        print(f"Daily+1H+15m+5m files: {total}", flush=True)
        iterator = sources
    elif args.source == "parquet":
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
        if getattr(args, "auto_universe", True):
            syms, universe_meta = build_auto_swing_universe(args, explicit_symbols)
        else:
            if not explicit_symbols:
                raise SystemExit("--symbols or --symbols-file is required when --source alpaca and --no-auto-universe is used")
            syms = sorted(set(normalize_symbol_for_swing(s) for s in explicit_symbols if normalize_symbol_for_swing(s)))
            universe_meta = {
                "mode": "manual_symbols",
                "symbols": len(syms),
                "note": "Manual Alpaca symbol universe. Auto discovery disabled.",
            }

        if args.limit_symbols:
            syms = syms[: args.limit_symbols]

        if not syms:
            raise SystemExit("No symbols available for Alpaca swing scan")

        write_universe_cache(syms, universe_meta, args.universe_cache)
        total = len(syms)
        print(f"Alpaca symbols: {total} ({universe_meta.get('mode', 'auto')})", flush=True)
        iterator = [(sym, None) for sym in syms]  # type: ignore[list-item]

    for idx, item in enumerate(iterator, start=1):
        if args.source == "daily-hourly" or args.daily_root:
            symbol, daily_path, hourly_path, m15_path, m5_path = item  # type: ignore[misc]
            obj = daily_path
        else:
            symbol, obj = item  # type: ignore[misc]
            hourly_path = None
            m15_path = None
            m5_path = None
        if idx == 1 or idx % 50 == 0 or idx == total:
            label = str(obj.name if isinstance(obj, Path) else symbol)
            print(f"[{idx}/{total}] {label}", flush=True)
        try:
            if args.source == "daily-hourly" or args.daily_root:
                daily, latest_day = read_daily_hourly_symbol(obj, hourly_path, m15_path, m5_path)  # type: ignore[arg-type]
            elif args.source == "parquet":
                daily, latest_day = read_parquet_symbol(obj)  # type: ignore[arg-type]
            else:
                daily, latest_day = read_alpaca_symbol(symbol, args.alpaca_feed)

            cs, notes = scan_symbol(symbol, daily, latest_day, args, earnings_map, smart_money_map, news_map, market_context)
            for n in notes:
                if n.startswith("HARD_REJECT:"):
                    rejected[n] = rejected.get(n, 0) + 1
                elif n.startswith("NO_SETUP:") or n.startswith("SCORE_REJECT:") or n.startswith("SETUP_SEEN:") or n.startswith("POOL_SEEN:"):
                    rejected[n] = rejected.get(n, 0) + 1
                else:
                    warnings_count[n] = warnings_count.get(n, 0) + 1
            candidates.extend(cs)

            del daily, latest_day, cs
            if idx % 25 == 0:
                gc.collect()
        except Exception as e:
            file_errors[symbol] = repr(e)
            continue

    before = len(candidates)
    display_rejected = len([c for c in candidates if not display_quality_allowed(c)])
    candidates = [c for c in candidates if display_quality_allowed(c)]
    before_display = len(candidates)
    candidates = dedupe_candidates(candidates)
    after = len(candidates)
    max_out = int(getattr(args, "max_output_candidates", 10) or 10)
    if max_out > 0:
        candidates = candidates[:max_out]

    summary: Dict[str, Any] = {
        "version": SCANNER_VERSION,
        "run_time": datetime.now().isoformat(timespec="seconds"),
        "source": args.source,
        "data_root": args.data_root or "",
        "daily_root": args.daily_root or "",
        "hourly_root": args.hourly_root or "",
        "m15_root": getattr(args, "m15_root", None) or "",
        "m5_root": getattr(args, "m5_root", None) or "",
        "symbols_file": args.symbols_file or "",
        "mode": args.mode,
        "total_inputs": total,
        "total_candidates_before_dedupe": before_display,
        "total_candidates_before_display_quality": before,
        "display_quality_rejected": display_rejected,
        "total_candidates": len(candidates),
        "total_candidates_before_limit": after,
        "dedupe_removed": before_display - after,
        "status_counts": {},
        "setup_counts": {},
        "top_blockers": dict(sorted(rejected.items(), key=lambda kv: kv[1], reverse=True)[:20]),
        "top_warnings": dict(sorted(warnings_count.items(), key=lambda kv: kv[1], reverse=True)[:20]),
        "diagnostic_counts": {
            "hard_rejected": sum(v for k, v in rejected.items() if k.startswith("HARD_REJECT:")),
            "setup_candidates_seen": sum(v for k, v in rejected.items() if k.startswith("SETUP_SEEN:")),
            "no_setup": sum(v for k, v in rejected.items() if k.startswith("NO_SETUP:")),
            "score_rejected": sum(v for k, v in rejected.items() if k.startswith("SCORE_REJECT:")),
            "candidate_pool_seen": sum(v for k, v in rejected.items() if k.startswith("POOL_SEEN:")),
            "watch_pool_built": sum(1 for c in candidates if c.swing_status == "SWING_WATCH"),
            "ready_promoted": sum(1 for c in candidates if c.swing_status == "SWING_READY"),
            "soft_downgraded": sum(v for k, v in warnings_count.items() if k.startswith("SOFT_DOWNGRADE:")),
        },
        "file_errors": dict(list(file_errors.items())[:20]),
        "universe": universe_meta,
        "smart_money_records": len(smart_money_map),
        "news_risk_records": len(news_map),
        "market_context_loaded": bool(market_context),
        "institutional_model": {
            "version": "v1.3.3_setup_tracking",
            "score_weights": INSTITUTIONAL_SCORE_WEIGHTS,
            "ready_score": SWING_READY_SCORE,
            "watch_score": SWING_WATCH_SCORE,
            "setup_rr_gates": SETUP_RR_GATES,
            "watch_rr_gates": SETUP_WATCH_RR_GATES,
            "max_output_candidates": int(getattr(args, "max_output_candidates", 10) or 10),
        },
        "note": "Universal institutional Swing Scanner using Daily + 1H swing inputs with $5-$200 price discipline, dollar-volume liquidity, live 1H/15m/5m confirmation, and full-universe support. Raw 1m remains preprocessing source only. Unique-ticker shortlist and display-quality floor are enabled; Ready remains strict. No broker execution. Scanner-owned setup_generated_at_et/setup_price tracking enabled.",
    }

    for c in candidates:
        summary["status_counts"][c.swing_status] = summary["status_counts"].get(c.swing_status, 0) + 1
        summary["setup_counts"][c.setup_type] = summary["setup_counts"].get(c.setup_type, 0) + 1

    return candidates, summary



def current_et_iso() -> str:
    """Return current America/New_York timestamp for setup first-seen tracking."""
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
        return datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")
    except Exception:
        return datetime.now().isoformat(timespec="seconds")


def setup_tracking_key(row: Dict[str, Any]) -> str:
    symbol = str(row.get("symbol", "")).upper().strip()
    setup_type = str(row.get("setup_type", "")).upper().strip()
    return f"{symbol}|{setup_type}" if symbol else ""


def load_previous_setup_tracking(csv_path: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """
    Preserve true first-seen setup timestamp/price across scanner reruns.

    Key rule:
    - Same symbol + same setup_type keeps original setup_generated_at_et/setup_price.
    - Symbol-only fallback is used only when a previous setup_type is missing.
    """
    by_key: Dict[str, Dict[str, Any]] = {}
    by_symbol: Dict[str, Dict[str, Any]] = {}
    if not csv_path.exists():
        return by_key, by_symbol

    try:
        prev = pd.read_csv(csv_path).fillna("")
    except Exception:
        return by_key, by_symbol

    keep_fields = [
        "setup_generated_at_et",
        "setup_price",
        "setup_data_time_et",
    ]

    for _, r in prev.iterrows():
        row = r.to_dict()
        symbol = str(row.get("symbol", "")).upper().strip()
        key = setup_tracking_key(row)
        if not symbol:
            continue
        if not any(str(row.get(f, "")).strip() for f in keep_fields):
            continue
        packed = {f: row.get(f, "") for f in keep_fields}
        if key:
            by_key[key] = packed
        if symbol not in by_symbol:
            by_symbol[symbol] = packed

    return by_key, by_symbol


def apply_setup_tracking(rows: List[Dict[str, Any]], csv_path: Path, summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Add stable Swing setup timestamp fields before CSV/JSON write.

    This is intentionally scanner-owned. The dashboard must not guess setup time
    from dashboard build time or file modified time.
    """
    now_et = current_et_iso()
    prev_by_key, prev_by_symbol = load_previous_setup_tracking(csv_path)

    first_seen_new = 0
    first_seen_preserved = 0

    for row in rows:
        symbol = str(row.get("symbol", "")).upper().strip()
        key = setup_tracking_key(row)

        previous = prev_by_key.get(key) or prev_by_symbol.get(symbol) or {}

        prev_generated = str(previous.get("setup_generated_at_et", "")).strip()
        prev_price = safe_float(previous.get("setup_price"), 0)
        prev_data_time = str(previous.get("setup_data_time_et", "")).strip()

        if prev_generated:
            row["setup_generated_at_et"] = prev_generated
            row["setup_price"] = round4(prev_price) if prev_price > 0 else round4(row.get("close_price") or row.get("price") or row.get("entry_trigger"))
            row["setup_data_time_et"] = prev_data_time or str(row.get("close_time_et") or row.get("latest_date_et") or "")
            first_seen_preserved += 1
        else:
            row["setup_generated_at_et"] = now_et
            row["setup_price"] = round4(row.get("close_price") or row.get("price") or row.get("entry_trigger"))
            row["setup_data_time_et"] = str(row.get("close_time_et") or row.get("latest_date_et") or "")
            first_seen_new += 1

        setup_price = safe_float(row.get("setup_price"), 0)
        current_price = safe_float(row.get("price") or row.get("close_price"), 0)
        row["move_since_setup_pct"] = round4(pct(current_price, setup_price)) if setup_price > 0 and current_price > 0 else 0.0

    summary["setup_tracking"] = {
        "tracked_rows": len(rows),
        "new_first_seen": first_seen_new,
        "preserved_first_seen": first_seen_preserved,
        "generated_at_et_for_new_rows": now_et,
        "note": "setup_generated_at_et/setup_price are scanner-owned first-seen fields. Dashboard should not infer setup time from file modified time.",
    }
    summary["setup_generated_at_et"] = now_et
    return rows

def write_outputs(candidates: Sequence[SwingCandidate], summary: Dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "swing_candidates_latest.csv"
    json_path = out_dir / "swing_candidates_latest.json"
    summary_path = out_dir / "swing_scanner_summary.json"

    rows = [asdict(c) for c in candidates]
    rows = apply_setup_tracking(rows, csv_path, summary)
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

    tracking = summary.get("setup_tracking", {}) or {}
    if tracking:
        print(
            "Setup tracking: "
            f"{tracking.get('tracked_rows', 0)} rows "
            f"({tracking.get('new_first_seen', 0)} new, "
            f"{tracking.get('preserved_first_seen', 0)} preserved)",
            flush=True,
        )
    print(f"Saved: {csv_path}")
    print(f"Saved: {json_path}")
    print(f"Saved: {summary_path}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Universal 1-3 Day Swing Scanner")
    p.add_argument("--source", choices=["daily-hourly", "parquet", "alpaca"], default="daily-hourly",
                   help="Data source. daily-hourly=pre-aggregated Daily + 1H swing bars; parquet=legacy raw intraday folder; alpaca=Alpaca live API.")
    p.add_argument("--daily-root", default=None,
                   help="Daily parquet folder for --source daily-hourly. Example: /opt/strategy-discovery/data/sp500_swing_daily")
    p.add_argument("--hourly-root", default=None,
                   help="1H parquet folder for --source daily-hourly. Example: /opt/strategy-discovery/data/live_swing_1h")
    p.add_argument("--m15-root", default=None,
                   help="15m parquet folder for --source daily-hourly. Example: /opt/strategy-discovery/data/live_swing_15m")
    p.add_argument("--m5-root", default=None,
                   help="5m parquet folder for --source daily-hourly. Example: /opt/strategy-discovery/data/live_swing_5m")
    p.add_argument("--data-root", default=None,
                   help="Legacy raw intraday parquet folder. Required only for --source parquet. Not recommended for swing scans.")
    p.add_argument("--symbols", default=None,
                   help="Optional comma-separated symbol seed/filter. Not required for Alpaca auto-universe mode.")
    p.add_argument("--symbols-file", default=None,
                   help="Optional text/CSV symbol seed/filter. First column must be symbol.")
    p.add_argument("--auto-universe", dest="auto_universe", action="store_true", default=True,
                   help="Alpaca mode: auto-discover swing universe from market screeners + local seed files. Default.")
    p.add_argument("--no-auto-universe", dest="auto_universe", action="store_false",
                   help="Alpaca mode: disable auto discovery and require --symbols or --symbols-file.")
    p.add_argument("--max-universe-symbols", type=int, default=int(os.getenv("SWING_MAX_UNIVERSE_SYMBOLS", "650")),
                   help="Maximum live Alpaca swing universe size after auto discovery.")
    p.add_argument("--universe-cache", default=os.getenv("SWING_UNIVERSE_CACHE", "swing_results/swing_universe_latest.csv"),
                   help="Where to write the latest auto-discovered swing universe.")
    p.add_argument("--limit-files", type=int, default=None,
                   help="Testing only: limit parquet files.")
    p.add_argument("--limit-symbols", type=int, default=None,
                   help="Testing only: limit Alpaca symbols after universe discovery.")
    p.add_argument("--mode", choices=["independent", "day-to-swing", "both"], default="independent",
                   help="Currently independent scanner is primary. day-to-swing reserved for EOD integration.")
    p.add_argument("--output-dir", default="/opt/elite-scanner/swing_results")
    p.add_argument("--earnings-csv", default=None)
    p.add_argument("--alpaca-feed", default=os.getenv("ALPACA_FEED", "sip"), choices=["sip", "iex", "otc"])
    p.add_argument("--min-price", type=float, default=5.0,
                   help="Hard minimum price. User swing discipline default is $5.")
    p.add_argument("--max-price", type=float, default=200.0,
                   help="Hard maximum price. User swing discipline default is $200.")
    p.add_argument("--min-avg-volume", type=float, default=500_000.0,
                   help="Share-volume liquidity floor used together with --min-avg-dollar-volume.")
    p.add_argument("--min-avg-dollar-volume", type=float, default=20_000_000.0,
                   help="20-day average dollar-volume floor. Hard reject only when share volume AND dollar volume are both below floor.")
    p.add_argument("--max-atr-pct", type=float, default=15.0)
    p.add_argument("--min-warmup-days", type=int, default=220)
    p.add_argument("--max-output-candidates", type=int, default=10,
                   help="Dashboard shortlist cap. Default 10; use 0 to disable cap.")
    p.add_argument("--smart-money-file", default=os.getenv("SMART_MONEY_OUTPUT_FILE", "smart_money_scores.json"),
                   help="Optional smart_money_scores.json from smart_money_bars_proxy.py.")
    p.add_argument("--news-risk-file", default=os.getenv("SWING_NEWS_RISK_FILE", "swing_news_risk.json"),
                   help="Optional swing news/catalyst risk JSON.")
    p.add_argument("--market-context-file", default=os.getenv("SWING_MARKET_CONTEXT_FILE", "swing_market_context.json"),
                   help="Optional market/sector context JSON.")
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    print("=== UNIVERSAL SWING SCANNER ===")
    print(f"Version: {SCANNER_VERSION}")
    print(f"Source: {args.source}")
    print(f"Data root: {args.data_root or ''}")
    print(f"Daily root: {args.daily_root or ''}")
    print(f"Hourly root: {args.hourly_root or ''}")
    print(f"15m root: {getattr(args, 'm15_root', None) or ''}")
    print(f"5m root: {getattr(args, 'm5_root', None) or ''}")
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
