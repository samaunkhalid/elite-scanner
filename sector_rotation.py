"""
sector_rotation.py
------------------
Standalone Alpaca SIP intraday sector rotation builder for Elite Scanner.

Phase 1 purpose:
  - Pull consolidated Alpaca SIP 1-minute bars for sector/benchmark ETFs.
  - Calculate 15/30/60-minute and session sector strength.
  - Track rank changes versus the previous sector_rotation.json.
  - Write sector_rotation.json and sector_rotation_history.json.
  - No scanner, dashboard, signal, or runner logic is changed in Phase 1.

Design decisions:
  - No 5-minute rotation signal. 15 minutes is the earliest rotation hint.
  - 30 minutes confirms short-term rotation.
  - 60 minutes confirms stronger intraday sector flow.
  - Session change shows full-day leadership.
  - Volume ratio is pace-adjusted versus recent daily average volume.
  - Rank change is positive when a sector moves up the leaderboard.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


# ==============================================================
# CONFIG
# ==============================================================

DATA_FEED = "sip"
ALPACA_DATA_BASE_URL = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets/v2").rstrip("/")
OUTPUT_FILE = "sector_rotation.json"
HISTORY_FILE = "sector_rotation_history.json"

INTRADAY_TIMEFRAME = "1Min"
DAILY_TIMEFRAME = "1Day"

# Core benchmark/proxy ETFs.
BENCHMARK_ETFS = {
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    "IWM": "Small Caps",
}

# Tradable sector / theme proxies for intraday rotation.
SECTOR_ETFS = {
    "XLK": "Technology",
    "SMH": "Semiconductors",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLV": "Healthcare",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLC": "Communication Services",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLB": "Materials",
    "IBB": "Biotech",
    "KRE": "Regional Banks",
    "KBE": "Banks",
    "ITA": "Aerospace & Defense",
    "ARKK": "High-Beta Growth",
}

ALL_ETFS = list(BENCHMARK_ETFS.keys()) + list(SECTOR_ETFS.keys())

REGULAR_SESSION_MINUTES = 390
HISTORY_MAX_ROWS = 150


# ==============================================================
# SAFE HELPERS
# ==============================================================

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and value.strip() == "":
            return default
        if pd.isna(value):
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def pct_change(current: float, base: float) -> float:
    if base <= 0:
        return 0.0
    return ((current - base) / base) * 100.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def ny_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo("America/New_York"))
    return datetime.now()


def iso_now_et() -> str:
    return ny_now().isoformat(timespec="seconds")


def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def et_iso(value: Any) -> str:
    dt = parse_ts(value)
    if not dt:
        return str(value or "")
    try:
        if ZoneInfo:
            dt = dt.astimezone(ZoneInfo("America/New_York"))
        return dt.isoformat(timespec="seconds")
    except Exception:
        return str(value or "")


def market_phase(now: Optional[datetime] = None) -> str:
    now = now or ny_now()

    if now.weekday() >= 5:
        return "CLOSED"

    t = now.time()
    if t < dtime(4, 0):
        return "CLOSED"
    if t < dtime(9, 30):
        return "PREMARKET"
    if t < dtime(16, 0):
        return "OPEN"
    if t < dtime(20, 0):
        return "AFTERHOURS"
    return "CLOSED"


def regular_session_start(now: Optional[datetime] = None) -> datetime:
    now = now or ny_now()
    if ZoneInfo:
        return datetime(now.year, now.month, now.day, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    return datetime(now.year, now.month, now.day, 9, 30)


def premarket_start(now: Optional[datetime] = None) -> datetime:
    now = now or ny_now()
    if ZoneInfo:
        return datetime(now.year, now.month, now.day, 4, 0, tzinfo=ZoneInfo("America/New_York"))
    return datetime(now.year, now.month, now.day, 4, 0)


def session_start_for_request(now: Optional[datetime] = None) -> datetime:
    now = now or ny_now()
    phase = market_phase(now)

    if phase in {"OPEN", "AFTERHOURS"}:
        return regular_session_start(now)

    if phase == "PREMARKET":
        return premarket_start(now)

    # Weekend/closed fallback: request last 3 calendar days to find the latest bars.
    return now - timedelta(days=3)


def elapsed_regular_minutes(now: Optional[datetime] = None) -> float:
    now = now or ny_now()
    start = regular_session_start(now)

    if now <= start:
        return 0.0

    end = min(now, start.replace(hour=16, minute=0, second=0, microsecond=0))
    return clamp((end - start).total_seconds() / 60.0, 0.0, REGULAR_SESSION_MINUTES)


def chunks(items: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ==============================================================
# API CLIENT
# ==============================================================

class AlpacaSectorClient:
    """Small raw-HTTP Alpaca market data client for sector rotation."""

    def __init__(self, timeout: int = 20) -> None:
        self.api_key = (
            os.getenv("ALPACA_API_KEY")
            or os.getenv("ALPACA_KEY")
            or os.getenv("APCA_API_KEY_ID")
            or ""
        ).strip()

        self.api_secret = (
            os.getenv("ALPACA_SECRET_KEY")
            or os.getenv("ALPACA_SECRET")
            or os.getenv("APCA_API_SECRET_KEY")
            or ""
        ).strip()

        self.timeout = timeout
        self.feed = DATA_FEED
        self.headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Accept": "application/json",
        }

    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.has_credentials():
            raise RuntimeError("Missing Alpaca credentials")

        params = dict(params or {})
        params["feed"] = self.feed

        url = f"{ALPACA_DATA_BASE_URL}{path}"
        response = requests.get(url, headers=self.headers, params=params, timeout=self.timeout)

        if response.status_code >= 400:
            body = response.text[:500] if getattr(response, "text", None) else ""
            raise RuntimeError(f"Alpaca API error {response.status_code}: {body}")

        return response.json() if response.content else {}

    def fetch_intraday_bars(self, symbols: List[str], start: datetime, end: datetime) -> Dict[str, pd.DataFrame]:
        out: Dict[str, pd.DataFrame] = {}

        for chunk in chunks(symbols, 50):
            params = {
                "symbols": ",".join(chunk),
                "timeframe": INTRADAY_TIMEFRAME,
                "start": to_utc_iso(start),
                "end": to_utc_iso(end),
                "limit": 10000,
                "adjustment": "raw",
                "sort": "asc",
            }

            data = self.get("/stocks/bars", params=params)
            bars = data.get("bars", {}) or {}

            for symbol in chunk:
                rows = bars.get(symbol, []) or []
                df = normalize_bars(rows)
                if not df.empty:
                    out[symbol] = df

        return out

    def fetch_daily_avg_volume(self, symbols: List[str], end: datetime, lookback_days: int = 45) -> Dict[str, float]:
        start = end - timedelta(days=lookback_days)
        out: Dict[str, float] = {}

        for chunk in chunks(symbols, 50):
            params = {
                "symbols": ",".join(chunk),
                "timeframe": DAILY_TIMEFRAME,
                "start": to_utc_iso(start),
                "end": to_utc_iso(end),
                "limit": 10000,
                "adjustment": "raw",
                "sort": "asc",
            }

            try:
                data = self.get("/stocks/bars", params=params)
            except Exception as exc:
                print(f"  ⚠ Daily volume fetch failed for {','.join(chunk[:5])}: {exc}")
                continue

            bars = data.get("bars", {}) or {}
            for symbol in chunk:
                rows = bars.get(symbol, []) or []
                df = normalize_bars(rows)
                if df.empty or "v" not in df.columns:
                    out[symbol] = 0.0
                    continue

                vol = pd.to_numeric(df["v"], errors="coerce").dropna()
                out[symbol] = float(vol.tail(20).mean()) if not vol.empty else 0.0

        return out


# ==============================================================
# BAR/METRIC CALCULATION
# ==============================================================

def normalize_bars(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    if df.empty or "t" not in df.columns:
        return pd.DataFrame()

    df["timestamp"] = pd.to_datetime(df["t"], utc=True, errors="coerce")

    for col in ["o", "h", "l", "c", "v", "vw"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["timestamp", "c"]).sort_values("timestamp").reset_index(drop=True)
    return df


def price_at_or_before(df: pd.DataFrame, target_time: datetime) -> float:
    if df.empty:
        return 0.0

    if target_time.tzinfo is None:
        target_time = target_time.replace(tzinfo=timezone.utc)

    target_utc = target_time.astimezone(timezone.utc)
    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    selected = df.loc[ts <= pd.Timestamp(target_utc)]

    if selected.empty:
        return safe_float(df.iloc[0].get("c"), 0.0)

    return safe_float(selected.iloc[-1].get("c"), 0.0)


def session_vwap(df: pd.DataFrame, fallback_price: float) -> float:
    if df.empty:
        return fallback_price

    vol = pd.to_numeric(df.get("v", pd.Series(dtype=float)), errors="coerce").fillna(0)

    if "vw" in df.columns:
        vw = pd.to_numeric(df["vw"], errors="coerce").fillna(0)
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

    return fallback_price


def volume_status(volume_ratio: float) -> str:
    if volume_ratio >= 1.50:
        return "High Volume Confirmation"
    if volume_ratio <= 0.80:
        return "Low Volume Warning"
    return "Normal Volume"


def calc_volume_ratio(current_volume: float, avg_daily_volume: float, now: datetime, phase: str) -> float:
    if current_volume <= 0 or avg_daily_volume <= 0:
        return 0.0

    if phase == "OPEN":
        elapsed = elapsed_regular_minutes(now)
        if elapsed <= 0:
            return 0.0
        expected_volume = avg_daily_volume * (elapsed / REGULAR_SESSION_MINUTES)
    elif phase == "AFTERHOURS":
        expected_volume = avg_daily_volume
    elif phase == "PREMARKET":
        # Premarket ETF volume is usually thin. This is used only as context.
        expected_volume = avg_daily_volume * 0.08
    else:
        expected_volume = avg_daily_volume

    if expected_volume <= 0:
        return 0.0

    return round(current_volume / expected_volume, 2)


def calc_one_etf_metrics(
    symbol: str,
    sector_name: str,
    df: pd.DataFrame,
    avg_daily_volume: float,
    now: datetime,
    phase: str,
) -> Dict[str, Any]:
    latest = df.iloc[-1]
    last_price = safe_float(latest.get("c"), 0.0)
    open_price = safe_float(df.iloc[0].get("o"), 0.0) or safe_float(df.iloc[0].get("c"), 0.0)

    change_pct = pct_change(last_price, open_price)
    change_15m = pct_change(last_price, price_at_or_before(df, now - timedelta(minutes=15)))
    change_30m = pct_change(last_price, price_at_or_before(df, now - timedelta(minutes=30)))
    change_60m = pct_change(last_price, price_at_or_before(df, now - timedelta(minutes=60)))

    vwap = session_vwap(df, last_price)
    vwap_dist = pct_change(last_price, vwap) if vwap > 0 else 0.0

    hod = safe_float(pd.to_numeric(df.get("h", pd.Series(dtype=float)), errors="coerce").max(), last_price)
    lod = safe_float(pd.to_numeric(df.get("l", pd.Series(dtype=float)), errors="coerce").min(), last_price)

    hod_dist = pct_change(last_price, hod) if hod > 0 else 0.0
    session_volume = safe_float(pd.to_numeric(df.get("v", pd.Series(dtype=float)), errors="coerce").fillna(0).sum(), 0.0)
    vol_ratio = calc_volume_ratio(session_volume, avg_daily_volume, now, phase)

    return {
        "symbol": symbol,
        "sector_name": sector_name,
        "price": round(last_price, 4),
        "open_price": round(open_price, 4),
        "change_pct": round(change_pct, 2),
        "change_15m_pct": round(change_15m, 2),
        "change_30m_pct": round(change_30m, 2),
        "change_60m_pct": round(change_60m, 2),
        "vwap": round(vwap, 4),
        "vwap_dist_pct": round(vwap_dist, 2),
        "above_vwap": bool(last_price >= vwap) if vwap > 0 else False,
        "hod": round(hod, 4),
        "lod": round(lod, 4),
        "hod_distance_pct": round(hod_dist, 2),
        "near_hod": bool(hod_dist >= -0.75),
        "session_volume": int(session_volume) if session_volume > 0 else 0,
        "avg_daily_volume_20d": int(avg_daily_volume) if avg_daily_volume > 0 else 0,
        "volume_ratio": vol_ratio,
        "volume_status": volume_status(vol_ratio),
        "latest_bar_time_et": et_iso(latest.get("t")),
        "bar_count": int(len(df)),
        "data_source": "Alpaca SIP",
    }


def base_score_from_metric(value: float, scale: float, max_points: float) -> float:
    if scale <= 0:
        return 0.0
    return clamp((value / scale) * max_points, -max_points, max_points)


def calc_rotation_score(row: Dict[str, Any]) -> int:
    """
    0-100 score.
    Positive sector flow, relative strength, VWAP/HOD location, rank improvement,
    and volume confirmation are rewarded.
    """
    score = 50.0

    score += base_score_from_metric(safe_float(row.get("change_pct"), 0), 1.20, 14)
    score += base_score_from_metric(safe_float(row.get("vs_spy_pct"), 0), 0.80, 14)
    score += base_score_from_metric(safe_float(row.get("vs_qqq_pct"), 0), 0.80, 10)
    score += base_score_from_metric(safe_float(row.get("change_30m_pct"), 0), 0.60, 10)
    score += base_score_from_metric(safe_float(row.get("change_60m_pct"), 0), 0.90, 10)

    vwap_dist = safe_float(row.get("vwap_dist_pct"), 0)
    hod_dist = safe_float(row.get("hod_distance_pct"), -999)
    rank_change = safe_float(row.get("rank_change"), 0)
    volume_ratio = safe_float(row.get("volume_ratio"), 0)

    if row.get("above_vwap"):
        score += 5
    else:
        score -= 6

    if -0.75 <= hod_dist <= 0.05:
        score += 5
    elif hod_dist <= -2.0:
        score -= 5

    if volume_ratio >= 1.50:
        score += 6
    elif 0 < volume_ratio <= 0.80:
        score -= 4

    if rank_change > 0:
        score += min(6, rank_change * 2)
    elif rank_change < 0:
        score += max(-6, rank_change * 2)

    return int(round(clamp(score, 0, 100)))


def rotation_label(row: Dict[str, Any]) -> str:
    score = safe_float(row.get("rotation_score"), 0)
    change = safe_float(row.get("change_pct"), 0)
    vs_spy = safe_float(row.get("vs_spy_pct"), 0)
    vwap = safe_float(row.get("vwap_dist_pct"), 0)
    ch30 = safe_float(row.get("change_30m_pct"), 0)
    ch60 = safe_float(row.get("change_60m_pct"), 0)

    if score >= 76 and change > 0 and vs_spy > 0 and vwap >= 0 and (ch30 >= 0 or ch60 >= 0):
        return "Strong"
    if score >= 61 and change >= 0 and (vs_spy >= 0 or ch30 >= 0):
        return "Supportive"
    if score <= 35 or (change < -0.35 and vs_spy < -0.25 and vwap < 0):
        return "Weak"
    if score <= 47 or (change < 0 and vs_spy < 0):
        return "Soft"
    return "Neutral"


def rotation_trend(row: Dict[str, Any]) -> str:
    ch15 = safe_float(row.get("change_15m_pct"), 0)
    ch30 = safe_float(row.get("change_30m_pct"), 0)
    ch60 = safe_float(row.get("change_60m_pct"), 0)
    rank_change = safe_float(row.get("rank_change"), 0)
    above_vwap = bool(row.get("above_vwap"))

    if ch60 < -0.20 and ch30 < -0.15 and not above_vwap:
        return "Rotation Out"

    if ch15 >= 0 and ch30 > 0 and ch60 > 0 and rank_change >= 1:
        return "Accelerating"

    if ch60 > 0 and ch30 < 0:
        return "Fading"

    if ch30 > 0 and ch60 >= 0:
        return "Improving"

    return "Stable"


# ==============================================================
# PREVIOUS RANK / HISTORY
# ==============================================================

def load_previous_ranks(path: str = OUTPUT_FILE) -> Dict[str, int]:
    p = Path(path)
    if not p.exists():
        return {}

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        sectors = data.get("sectors", {}) if isinstance(data, dict) else {}
        out = {}
        for symbol, row in sectors.items():
            rank = safe_int(row.get("rank"), 0)
            if rank > 0:
                out[str(symbol).upper()] = rank
        return out
    except Exception:
        return {}


def append_history(payload: Dict[str, Any], path: str = HISTORY_FILE) -> None:
    history_path = Path(path)
    rows: List[Dict[str, Any]] = []

    if history_path.exists():
        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                rows = data
        except Exception:
            rows = []

    compact = {
        "generated_at_et": payload.get("generated_at_et", ""),
        "market_phase": payload.get("market_phase", ""),
        "feed": payload.get("feed", ""),
        "ranked": [
            {
                "symbol": row.get("symbol"),
                "rank": row.get("rank"),
                "rank_change": row.get("rank_change"),
                "rotation_score": row.get("rotation_score"),
                "rotation_label": row.get("rotation_label"),
                "rotation_trend": row.get("rotation_trend"),
                "change_pct": row.get("change_pct"),
                "vs_spy_pct": row.get("vs_spy_pct"),
                "volume_ratio": row.get("volume_ratio"),
            }
            for row in payload.get("ranked", [])
        ],
    }

    rows.append(compact)
    rows = rows[-HISTORY_MAX_ROWS:]
    history_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


# ==============================================================
# BUILDER
# ==============================================================

def build_sector_rotation() -> Dict[str, Any]:
    now = ny_now()
    phase = market_phase(now)
    start = session_start_for_request(now)
    previous_ranks = load_previous_ranks()

    payload: Dict[str, Any] = {
        "generated_at_et": iso_now_et(),
        "feed": DATA_FEED,
        "data_source": "Alpaca SIP",
        "market_phase": phase,
        "session_start_et": start.isoformat(timespec="seconds") if hasattr(start, "isoformat") else "",
        "benchmark_symbols": list(BENCHMARK_ETFS.keys()),
        "sector_symbols": list(SECTOR_ETFS.keys()),
        "benchmarks": {},
        "sectors": {},
        "ranked": [],
        "errors": [],
        "notes": [
            "No 5-minute sector rotation signal is used.",
            "15-minute change is the earliest rotation hint.",
            "30/60-minute and session trends drive rotation labels.",
            "Sector rotation is context, not a hard trading filter.",
        ],
    }

    client = AlpacaSectorClient()

    if not client.has_credentials():
        payload["errors"].append("Missing Alpaca credentials.")
        write_payload(payload)
        return payload

    try:
        print(f"  Fetching Alpaca SIP 1Min bars for {len(ALL_ETFS)} ETFs...")
        intraday = client.fetch_intraday_bars(ALL_ETFS, start=start, end=now)
    except Exception as exc:
        payload["errors"].append(f"Intraday bar fetch failed: {exc}")
        write_payload(payload)
        return payload

    try:
        daily_avg_volume = client.fetch_daily_avg_volume(ALL_ETFS, end=now)
    except Exception as exc:
        payload["errors"].append(f"Daily volume fetch failed: {exc}")
        daily_avg_volume = {}

    raw_rows: Dict[str, Dict[str, Any]] = {}

    for symbol in ALL_ETFS:
        df = intraday.get(symbol)
        if df is None or df.empty:
            payload["errors"].append(f"No intraday bars for {symbol}.")
            continue

        sector_name = BENCHMARK_ETFS.get(symbol) or SECTOR_ETFS.get(symbol) or symbol

        row = calc_one_etf_metrics(
            symbol=symbol,
            sector_name=sector_name,
            df=df,
            avg_daily_volume=safe_float(daily_avg_volume.get(symbol), 0),
            now=now,
            phase=phase,
        )
        raw_rows[symbol] = row

    spy_change = safe_float(raw_rows.get("SPY", {}).get("change_pct"), 0)
    qqq_change = safe_float(raw_rows.get("QQQ", {}).get("change_pct"), 0)

    for symbol, row in raw_rows.items():
        row["vs_spy_pct"] = round(safe_float(row.get("change_pct"), 0) - spy_change, 2)
        row["vs_qqq_pct"] = round(safe_float(row.get("change_pct"), 0) - qqq_change, 2)

    # Rank sector ETFs only; benchmarks are separate.
    sector_rows = [raw_rows[symbol] for symbol in SECTOR_ETFS.keys() if symbol in raw_rows]
    sector_rows.sort(
        key=lambda r: (
            safe_float(r.get("vs_spy_pct"), 0),
            safe_float(r.get("change_pct"), 0),
            safe_float(r.get("change_30m_pct"), 0),
            safe_float(r.get("volume_ratio"), 0),
        ),
        reverse=True,
    )

    for idx, row in enumerate(sector_rows, start=1):
        symbol = str(row.get("symbol", "")).upper()
        previous_rank = previous_ranks.get(symbol)
        rank_change = previous_rank - idx if previous_rank else 0

        row["rank"] = idx
        row["previous_rank"] = previous_rank or ""
        row["rank_change"] = rank_change
        row["rotation_score"] = calc_rotation_score(row)
        row["rotation_label"] = rotation_label(row)
        row["rotation_trend"] = rotation_trend(row)

        payload["sectors"][symbol] = row

    for symbol in BENCHMARK_ETFS.keys():
        row = raw_rows.get(symbol)
        if not row:
            continue
        row["rank"] = ""
        row["previous_rank"] = ""
        row["rank_change"] = 0
        row["rotation_score"] = calc_rotation_score(row)
        row["rotation_label"] = rotation_label(row)
        row["rotation_trend"] = rotation_trend(row)
        payload["benchmarks"][symbol] = row

    payload["ranked"] = sorted(
        payload["sectors"].values(),
        key=lambda r: safe_int(r.get("rank"), 999),
    )

    write_payload(payload)
    append_history(payload)

    return payload


def write_payload(payload: Dict[str, Any]) -> None:
    Path(OUTPUT_FILE).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def print_summary(payload: Dict[str, Any]) -> None:
    print("\n" + "=" * 70)
    print("SECTOR ROTATION — ALPACA SIP")
    print("=" * 70)
    print(f"Generated: {payload.get('generated_at_et')}")
    print(f"Market phase: {payload.get('market_phase')}")
    print(f"Feed: {payload.get('feed')}")
    print(f"Sectors ranked: {len(payload.get('ranked', []))}")

    errors = payload.get("errors", []) or []
    if errors:
        print("\nWarnings / Errors:")
        for err in errors[:10]:
            print(f"  ⚠ {err}")

    ranked = payload.get("ranked", []) or []
    if ranked:
        print("\nTop sector rotation:")
        for row in ranked[:8]:
            rank_change = safe_int(row.get("rank_change"), 0)
            rank_text = f"{rank_change:+d}" if rank_change else "0"
            print(
                f"  #{row.get('rank'):>2} {row.get('symbol')} "
                f"{row.get('sector_name')} | "
                f"{safe_float(row.get('change_pct'), 0):+.2f}% | "
                f"Vs SPY {safe_float(row.get('vs_spy_pct'), 0):+.2f}% | "
                f"30m {safe_float(row.get('change_30m_pct'), 0):+.2f}% | "
                f"60m {safe_float(row.get('change_60m_pct'), 0):+.2f}% | "
                f"Vol {safe_float(row.get('volume_ratio'), 0):.2f}x | "
                f"Rank {rank_text} | "
                f"{row.get('rotation_label')} / {row.get('rotation_trend')}"
            )

    print(f"\nSaved: {OUTPUT_FILE}")
    print(f"Saved: {HISTORY_FILE}")
    print("=" * 70 + "\n")


def main() -> int:
    payload = build_sector_rotation()
    print_summary(payload)
    return 0 if not payload.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
