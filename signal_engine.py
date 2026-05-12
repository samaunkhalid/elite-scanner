"""
signal_engine.py
----------------
Signal Desk v1 engine for the Elite Scanner project.

Purpose:
  - Read the scanner's focused candidates:
      * Top 12 potential_movers.csv
      * Top 8 active_momentum.csv
  - Monitor those names with Alpaca intraday bars/quotes.
  - Preserve protected TRIGGER_READY / ACTIVE_SIGNAL states across refreshes.
  - Write:
      * signal_desk.json
      * signal_state.json
      * suppressed_signals.csv

Core design:
  - 1-minute bars = execution timing and live invalidation.
  - 5-minute bars = setup structure, stop/target construction, and pattern quality.
  - Long-side only.
  - Red/risk-off market can still allow RELATIVE-STRENGTH WATCH candidates.
  - Invalid trade plans must NOT appear as WATCH.
  - Late-day setups require stronger volume, no bearish divergence, and EMA9 confirmation.

Important:
  - This engine generates dashboard signals only.
  - It does NOT place orders.
  - Manual chart confirmation is still required before any trade.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
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

POTENTIAL_LIMIT = 12
ACTIVE_LIMIT = 8

DATA_FEED = os.getenv("ALPACA_DATA_FEED", "sip").strip().lower() or "sip"
ALPACA_BASE_URL = "https://data.alpaca.markets/v2"

POTENTIAL_FILE = "potential_movers.csv"
ACTIVE_FILE = "active_momentum.csv"
MARKET_REGIME_FILE = "market_regime.json"

SIGNAL_DESK_FILE = "signal_desk.json"
SIGNAL_STATE_FILE = "signal_state.json"
SUPPRESSED_SIGNALS_FILE = "suppressed_signals.csv"

# State retention.
ACTIVE_STALE_MINUTES = 10
TRIGGER_READY_STALE_MINUTES = 45
RECENT_INVALIDATED_KEEP_MINUTES = 30

# Quality thresholds.
MIN_AVG_DOLLAR_VOL_M = 25.0
MAX_VWAP_EXTENSION_PCT = 5.0
ACTIVE_HOD_MAX_DISTANCE = -2.5
POTENTIAL_HOD_MAX_DISTANCE = -4.0
HOD_BREAKOUT_READY_DISTANCE = -0.75

MIN_RR_WATCH = 0.75
MIN_RR = 1.5
MIN_CONF_WATCH = 60.0
MIN_CONF_READY = 75.0
MIN_CONF_READY_LATE_DAY = 80.0
MIN_CONF_ACTIVE = 85.0

# Late-day / quality filters.
LATE_DAY_READY_START = dtime(14, 30)
VOLUME_FADE_RATIO = 0.60          # Recent 5x5m volume < 60% of morning reference = fading.
VOLUME_EXPANSION_RATIO = 1.05     # Recent 5x5m volume must be > prior 5x5m by 5% for late-day ready.
MIN_LATE_DAY_MORNING_VOL_RATIO = 0.60
BEARISH_DIVERGENCE_PENALTY = 10
VOLUME_FADE_PENALTY = 8
LATE_DAY_NO_VOLUME_EXPANSION_PENALTY = 5

# EMA 9 confirmation.
EMA_SPAN = 9
EMA9_BULLISH_BONUS_MAX = 10
EMA9_BELOW_PRICE_PENALTY = 5
EMA9_FALLING_PENALTY = 4
EMA9_BELOW_VWAP_PENALTY = 4

MAX_STOP_DIST_NORMAL = 3.0
MAX_STOP_DIST_HOD = 3.5

EXECUTION_TIMEFRAME = "1Min"
STRUCTURE_TIMEFRAME = "5Min"

DEBUG = os.getenv("SIGNAL_DEBUG", "0").strip() == "1"


# ==============================================================
# SAFE HELPERS
# ==============================================================

def log(msg: str) -> None:
    print(msg)


def debug(msg: str) -> None:
    if DEBUG:
        print(msg)


def safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        if isinstance(value, float) and pd.isna(value):
            return default
    except Exception:
        pass
    text = str(value)
    if text.lower() in {"nan", "none", "nat"}:
        return default
    return text


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and value.strip() in {"", "—", "nan", "None"}:
            return default
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pct(part: float, whole: float) -> float:
    if whole == 0:
        return 0.0
    return (part / whole) * 100.0


def pct_change(current: float, base: float) -> float:
    if base <= 0:
        return 0.0
    return ((current - base) / base) * 100.0


def normalize_status(status: Any) -> str:
    return safe_str(status, "WAIT").upper().replace(" ", "_")


def normalize_symbol(symbol: Any) -> str:
    return re.sub(r"[^A-Z0-9._-]", "", safe_str(symbol, "").upper().strip())


def parse_iso_dt(text: Any) -> Optional[datetime]:
    s = safe_str(text, "").strip()
    if not s:
        return None

    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass

    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def ny_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo("America/New_York"))
    return datetime.now()


def iso_now_et() -> str:
    return ny_now().isoformat(timespec="seconds")


def minutes_since(dt_text: Any, now: Optional[datetime] = None) -> Optional[float]:
    dt = parse_iso_dt(dt_text)
    if not dt:
        return None

    if now is None:
        now = ny_now()

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    try:
        if ZoneInfo:
            dt = dt.astimezone(ZoneInfo("America/New_York"))
        return (now - dt).total_seconds() / 60.0
    except Exception:
        return None


# ==============================================================
# TIME HELPERS
# ==============================================================

def get_market_phase(now: Optional[datetime] = None) -> str:
    """
    Returns:
      CLOSED
      PREMARKET
      OPENING_BLACKOUT
      VALID_MORNING
      LUNCH_BLACKOUT
      VALID_AFTERNOON
      FINAL_BLACKOUT
      AFTERHOURS
    """
    now = now or ny_now()

    if now.weekday() >= 5:
        return "CLOSED"

    t = now.time()

    if t < dtime(4, 0):
        return "CLOSED"
    if t < dtime(9, 30):
        return "PREMARKET"
    if t < dtime(9, 45):
        return "OPENING_BLACKOUT"
    if t < dtime(11, 30):
        return "VALID_MORNING"
    if t < dtime(13, 30):
        return "LUNCH_BLACKOUT"
    if t < dtime(15, 45):
        return "VALID_AFTERNOON"
    if t < dtime(16, 0):
        return "FINAL_BLACKOUT"
    if t < dtime(20, 0):
        return "AFTERHOURS"
    return "CLOSED"



def is_late_day(now: Optional[datetime] = None) -> bool:
    """
    Late-day setups need stronger proof because breakouts have less time
    and failed breakouts are more common after 2:30 PM ET.
    """
    now = now or ny_now()
    return now.time() >= LATE_DAY_READY_START and now.time() < dtime(16, 0)


def ready_confidence_required(phase: str) -> float:
    if phase == "VALID_AFTERNOON" and is_late_day():
        return MIN_CONF_READY_LATE_DAY
    return MIN_CONF_READY


def late_day_volume_confirmed(metrics: "IntradayMetrics") -> bool:
    if not is_late_day():
        return True

    # After 2:30 PM, stable volume is not enough for a trigger-ready signal.
    # We require recent expansion and no severe fade vs the morning reference.
    return (
        metrics.recent_volume_expanding
        and not metrics.volume_fading_vs_morning
        and (
            metrics.morning_avg_volume_5m <= 0
            or metrics.recent_to_morning_volume_ratio >= MIN_LATE_DAY_MORNING_VOL_RATIO
        )
    )


def is_market_open_phase(phase: str) -> bool:
    return phase in {
        "OPENING_BLACKOUT",
        "VALID_MORNING",
        "LUNCH_BLACKOUT",
        "VALID_AFTERNOON",
        "FINAL_BLACKOUT",
    }


def is_valid_signal_phase(phase: str) -> bool:
    return phase in {"VALID_MORNING", "VALID_AFTERNOON"}


def session_date_str(now: Optional[datetime] = None) -> str:
    now = now or ny_now()
    return now.date().isoformat()


def session_start_end_utc(now: Optional[datetime] = None) -> Tuple[str, str]:
    """
    Regular-session window for signal logic.
    Uses 9:30 AM ET to now.
    """
    now = now or ny_now()

    if ZoneInfo:
        start_ny = datetime(now.year, now.month, now.day, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        end_ny = now
        start_utc = start_ny.astimezone(timezone.utc)
        end_utc = end_ny.astimezone(timezone.utc)
    else:
        start_utc = datetime.utcnow().replace(hour=13, minute=30, second=0, microsecond=0)
        end_utc = datetime.utcnow().replace(tzinfo=timezone.utc)

    return (
        start_utc.isoformat().replace("+00:00", "Z"),
        end_utc.isoformat().replace("+00:00", "Z"),
    )


# ==============================================================
# FILE LOADERS
# ==============================================================

def load_json(path: str, default: Any) -> Any:
    p = Path(path)
    if not p.exists():
        return default

    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"  ⚠ Failed to load {path}: {e}")
        return default


def write_json(path: str, data: Any) -> None:
    with Path(path).open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_csv(path: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []

    try:
        df = pd.read_csv(p).fillna("")
        if limit is not None:
            df = df.head(limit)
        return df.to_dict("records")
    except Exception as e:
        log(f"  ⚠ Failed to load {path}: {e}")
        return []


def load_focus_candidates() -> Dict[str, Dict[str, Any]]:
    """
    Load Top 12 Potential + Top 8 Active.
    Returns symbol -> row.
    If duplicate appears, keep the first bucket priority:
      Potential first, Active second.
    """
    focus: Dict[str, Dict[str, Any]] = {}

    potential = load_csv(POTENTIAL_FILE, POTENTIAL_LIMIT)
    active = load_csv(ACTIVE_FILE, ACTIVE_LIMIT)

    for idx, row in enumerate(potential):
        sym = normalize_symbol(row.get("symbol"))
        if not sym:
            continue
        row = dict(row)
        row["symbol"] = sym
        row["signal_source_bucket"] = "POTENTIAL"
        row["signal_rank"] = idx + 1
        focus[sym] = row

    for idx, row in enumerate(active):
        sym = normalize_symbol(row.get("symbol"))
        if not sym:
            continue
        if sym in focus:
            focus[sym]["signal_source_bucket"] = "POTENTIAL+ACTIVE"
            continue
        row = dict(row)
        row["symbol"] = sym
        row["signal_source_bucket"] = "ACTIVE"
        row["signal_rank"] = idx + 1
        focus[sym] = row

    return focus


def load_signal_state() -> Dict[str, Dict[str, Any]]:
    data = load_json(SIGNAL_STATE_FILE, {})
    if isinstance(data, dict):
        if isinstance(data.get("signals"), dict):
            return data.get("signals", {})
        return data
    return {}


def write_signal_state(state: Dict[str, Dict[str, Any]], meta: Dict[str, Any]) -> None:
    write_json(
        SIGNAL_STATE_FILE,
        {
            "generated_at_et": iso_now_et(),
            "meta": meta,
            "signals": state,
        },
    )


def append_suppressed_signal(row: Dict[str, Any]) -> None:
    path = Path(SUPPRESSED_SIGNALS_FILE)
    exists = path.exists()

    fieldnames = [
        "timestamp_et",
        "symbol",
        "setup_type",
        "scanner_score",
        "live_signal_score",
        "confidence",
        "entry_trigger",
        "stop_loss",
        "target_1",
        "target_2",
        "reward_risk",
        "suppression_reason",
        "price_at_trigger",
        "vwap",
        "hod_distance_pct",
        "vwap_distance_pct",
        "sector_status",
        "market_regime",
        "notes",
    ]

    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not exists:
            writer.writeheader()

        writer.writerow({k: row.get(k, "") for k in fieldnames})


# ==============================================================
# ALPACA MARKET DATA
# ==============================================================

class AlpacaMarketData:
    def __init__(self, feed: str = DATA_FEED):
        self.api_key = os.getenv("ALPACA_API_KEY")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY")
        self.feed = feed

        self.headers = {
            "APCA-API-KEY-ID": self.api_key or "",
            "APCA-API-SECRET-KEY": self.secret_key or "",
        }

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def _chunks(self, symbols: List[str], size: int = 20) -> Iterable[List[str]]:
        for i in range(0, len(symbols), size):
            yield symbols[i:i + size]

    def fetch_bars(self, symbols: List[str], timeframe: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch today's regular-session bars.
        """
        if not self.available:
            log("  ⚠ Alpaca credentials missing; signal engine cannot fetch intraday bars.")
            return {}

        symbols = [normalize_symbol(s) for s in symbols if normalize_symbol(s)]
        symbols = list(dict.fromkeys(symbols))

        if not symbols:
            return {}

        start, end = session_start_end_utc()
        url = f"{ALPACA_BASE_URL}/stocks/bars"
        output: Dict[str, List[Dict[str, Any]]] = {}

        for batch in self._chunks(symbols, 20):
            page_token = None

            while True:
                params = {
                    "symbols": ",".join(batch),
                    "timeframe": timeframe,
                    "start": start,
                    "end": end,
                    "limit": 10000,
                    "adjustment": "raw",
                    "feed": self.feed,
                    "sort": "asc",
                }

                if page_token:
                    params["page_token"] = page_token

                try:
                    r = requests.get(url, headers=self.headers, params=params, timeout=25)

                    if r.status_code != 200:
                        log(f"  ⚠ Alpaca bars error {r.status_code}: {r.text[:300]}")
                        break

                    data = r.json()
                    bars = data.get("bars", {}) or {}

                    for sym, sym_bars in bars.items():
                        output.setdefault(sym, []).extend(sym_bars or [])

                    page_token = data.get("next_page_token")
                    if not page_token:
                        break

                    time.sleep(0.12)

                except Exception as e:
                    log(f"  ⚠ Alpaca bars request failed: {e}")
                    break

        return output

    def fetch_latest_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        if not self.available:
            return {}

        symbols = [normalize_symbol(s) for s in symbols if normalize_symbol(s)]
        symbols = list(dict.fromkeys(symbols))

        if not symbols:
            return {}

        url = f"{ALPACA_BASE_URL}/stocks/quotes/latest"
        output: Dict[str, Dict[str, Any]] = {}

        for batch in self._chunks(symbols, 50):
            params = {
                "symbols": ",".join(batch),
                "feed": self.feed,
            }

            try:
                r = requests.get(url, headers=self.headers, params=params, timeout=20)

                if r.status_code != 200:
                    log(f"  ⚠ Alpaca quotes error {r.status_code}: {r.text[:300]}")
                    continue

                data = r.json()
                quotes = data.get("quotes", {}) or {}

                for sym, quote in quotes.items():
                    output[sym] = quote or {}

            except Exception as e:
                log(f"  ⚠ Alpaca quotes request failed: {e}")

        return output


# ==============================================================
# BAR ANALYSIS
# ==============================================================

@dataclass
class IntradayMetrics:
    symbol: str
    has_data: bool = False
    price: float = 0.0
    session_open: float = 0.0
    day_change_pct: float = 0.0
    vwap: float = 0.0
    above_vwap: bool = False
    vwap_dist_pct: float = 0.0
    hod: float = 0.0
    lod: float = 0.0
    hod_distance_pct: float = 0.0
    day_volume: float = 0.0
    latest_bar_time: str = ""

    avg_volume_5: float = 0.0
    avg_volume_prev_5: float = 0.0
    morning_avg_volume_5m: float = 0.0
    recent_to_morning_volume_ratio: float = 0.0
    volume_stable_or_increasing: bool = False
    volume_drying: bool = False
    volume_fading_vs_morning: bool = False
    recent_volume_expanding: bool = False

    bearish_momentum_divergence: bool = False
    macd_prior_high: float = 0.0
    macd_recent_high: float = 0.0
    price_prior_high: float = 0.0
    price_recent_high: float = 0.0
    momentum_status: str = "CLEAN"

    ema9: float = 0.0
    ema9_prev: float = 0.0
    ema9_slope_pct: float = 0.0
    price_above_ema9: bool = False
    ema9_rising: bool = False
    ema9_falling: bool = False
    ema9_above_vwap: bool = False
    ema9_crossed_above_vwap_recent: bool = False
    ema9_status: str = "UNKNOWN"

    pullback_holding_vwap: bool = False
    pullback_high: float = 0.0
    pullback_low: float = 0.0
    recent_swing_low: float = 0.0
    opening_range_low: float = 0.0
    vwap_touch_count: int = 0
    consolidating_near_high: bool = False

    base_compression: bool = False
    base_range_pct: float = 0.0
    base_high: float = 0.0
    base_low: float = 0.0
    base_volume_constructive: bool = False
    higher_low_or_flat_base: bool = False
    price_near_base_breakout: bool = False
    structure_bar_count: int = 0


def typical_price(bar: Dict[str, Any]) -> float:
    h = safe_float(bar.get("h"), 0)
    l = safe_float(bar.get("l"), 0)
    c = safe_float(bar.get("c"), 0)
    if h > 0 and l > 0 and c > 0:
        return (h + l + c) / 3.0
    return c


def clean_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for b in bars:
        c = safe_float(b.get("c"), 0)
        if c > 0:
            out.append(b)
    return out


def ema_series(values: List[float], span: int) -> List[float]:
    if not values:
        return []

    alpha = 2.0 / (span + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1.0 - alpha) * out[-1])
    return out


def macd_line(values: List[float]) -> List[float]:
    if len(values) < 26:
        return []

    fast = ema_series(values, 12)
    slow = ema_series(values, 26)
    return [f - s for f, s in zip(fast, slow)]


def analyze_execution_bars(symbol: str, bars: List[Dict[str, Any]]) -> IntradayMetrics:
    metrics = IntradayMetrics(symbol=symbol)

    clean = clean_bars(bars)
    if not clean:
        return metrics

    metrics.has_data = True
    metrics.price = safe_float(clean[-1].get("c"), 0)
    metrics.session_open = safe_float(clean[0].get("o"), safe_float(clean[0].get("c"), 0))
    metrics.day_change_pct = pct_change(metrics.price, metrics.session_open) if metrics.session_open > 0 else 0
    metrics.hod = max(safe_float(b.get("h"), 0) for b in clean)
    metrics.lod = min(safe_float(b.get("l"), metrics.price) for b in clean if safe_float(b.get("l"), 0) > 0)
    metrics.latest_bar_time = safe_str(clean[-1].get("t"), "")

    pv = 0.0
    vol = 0.0
    for b in clean:
        bar_vol = safe_float(b.get("v"), 0)
        pv += typical_price(b) * bar_vol
        vol += bar_vol

    metrics.day_volume = vol
    metrics.vwap = pv / vol if vol > 0 else metrics.price
    metrics.above_vwap = metrics.price >= metrics.vwap if metrics.vwap > 0 else False
    metrics.vwap_dist_pct = pct_change(metrics.price, metrics.vwap) if metrics.vwap > 0 else 0
    metrics.hod_distance_pct = pct_change(metrics.price, metrics.hod) if metrics.hod > 0 else 0

    first15 = clean[:15]
    if first15:
        lows = [safe_float(b.get("l"), 0) for b in first15 if safe_float(b.get("l"), 0) > 0]
        metrics.opening_range_low = min(lows) if lows else metrics.lod

    return metrics


def enrich_structure_from_5min(metrics: IntradayMetrics, bars_5m: List[Dict[str, Any]]) -> IntradayMetrics:
    clean = clean_bars(bars_5m)
    metrics.structure_bar_count = len(clean)

    if not clean:
        return metrics

    recent3 = clean[-3:] if len(clean) >= 3 else clean
    recent5 = clean[-5:] if len(clean) >= 5 else clean

    # Structure volume.
    prev5 = clean[-10:-5] if len(clean) >= 10 else clean[:-5]
    recent_vols = [safe_float(b.get("v"), 0) for b in recent5]
    prev_vols = [safe_float(b.get("v"), 0) for b in prev5]

    # Morning reference: avoid the first few opening bars if enough data exists,
    # then compare the latest 25 minutes against earlier-session participation.
    if len(clean) >= 30:
        morning_ref = clean[6:30]
    elif len(clean) >= 16:
        morning_ref = clean[3:16]
    else:
        morning_ref = clean[: max(1, len(clean) // 2)]

    morning_vols = [safe_float(b.get("v"), 0) for b in morning_ref]
    metrics.avg_volume_5 = sum(recent_vols) / len(recent_vols) if recent_vols else 0
    metrics.avg_volume_prev_5 = sum(prev_vols) / len(prev_vols) if prev_vols else 0
    metrics.morning_avg_volume_5m = sum(morning_vols) / len(morning_vols) if morning_vols else 0
    metrics.recent_to_morning_volume_ratio = (
        metrics.avg_volume_5 / metrics.morning_avg_volume_5m
        if metrics.morning_avg_volume_5m > 0
        else 0
    )

    metrics.recent_volume_expanding = (
        metrics.avg_volume_prev_5 > 0
        and metrics.avg_volume_5 >= VOLUME_EXPANSION_RATIO * metrics.avg_volume_prev_5
    )
    metrics.volume_stable_or_increasing = (
        metrics.avg_volume_prev_5 <= 0
        or metrics.avg_volume_5 >= 0.85 * metrics.avg_volume_prev_5
    )
    metrics.volume_drying = (
        metrics.avg_volume_prev_5 > 0 and metrics.avg_volume_5 <= 0.80 * metrics.avg_volume_prev_5
    )
    metrics.volume_fading_vs_morning = (
        metrics.morning_avg_volume_5m > 0
        and metrics.avg_volume_5 < VOLUME_FADE_RATIO * metrics.morning_avg_volume_5m
    )
    metrics.base_volume_constructive = (
        metrics.avg_volume_prev_5 <= 0
        or metrics.avg_volume_5 <= 1.20 * metrics.avg_volume_prev_5
        or metrics.volume_drying
    )

    # EMA 9 confirmation from 5-minute structure.
    closes = [safe_float(b.get("c"), 0) for b in clean]
    ema9_values = ema_series(closes, EMA_SPAN)
    if ema9_values:
        metrics.ema9 = ema9_values[-1]
        metrics.ema9_prev = ema9_values[-2] if len(ema9_values) >= 2 else ema9_values[-1]
        metrics.ema9_slope_pct = pct_change(metrics.ema9, metrics.ema9_prev) if metrics.ema9_prev > 0 else 0
        metrics.price_above_ema9 = metrics.price >= metrics.ema9 if metrics.ema9 > 0 else False
        metrics.ema9_rising = metrics.ema9 >= metrics.ema9_prev * 0.999
        metrics.ema9_falling = metrics.ema9 < metrics.ema9_prev * 0.999
        metrics.ema9_above_vwap = metrics.ema9 >= metrics.vwap if metrics.vwap > 0 else False

        # EMA9 crossing VWAP from below is treated as a bullish confirmation.
        # Uses current session VWAP as the reference line to avoid noisy per-bar VWAP math.
        recent_ema = ema9_values[-6:] if len(ema9_values) >= 6 else ema9_values
        if metrics.vwap > 0 and len(recent_ema) >= 2:
            was_below = min(recent_ema[:-1]) <= metrics.vwap
            now_above = recent_ema[-1] >= metrics.vwap
            metrics.ema9_crossed_above_vwap_recent = bool(was_below and now_above)

        if metrics.price_above_ema9 and metrics.ema9_above_vwap and not metrics.ema9_falling:
            metrics.ema9_status = "BULLISH_ALIGNMENT"
        elif metrics.ema9_crossed_above_vwap_recent and metrics.price_above_ema9:
            metrics.ema9_status = "RECENT_BULLISH_CROSS"
        elif not metrics.price_above_ema9:
            metrics.ema9_status = "PRICE_BELOW_EMA9"
        elif metrics.ema9_falling:
            metrics.ema9_status = "EMA9_FALLING"
        elif not metrics.ema9_above_vwap:
            metrics.ema9_status = "EMA9_BELOW_VWAP"
        else:
            metrics.ema9_status = "NEUTRAL"

    # Momentum / divergence using 5-minute MACD.
    macd = macd_line(closes)
    if len(clean) >= 30 and len(macd) >= 12:
        recent_window = clean[-12:]
        recent_macd = macd[-12:]
        first_half_bars = recent_window[:6]
        second_half_bars = recent_window[6:]
        first_half_macd = recent_macd[:6]
        second_half_macd = recent_macd[6:]

        metrics.price_prior_high = max(safe_float(b.get("h"), 0) for b in first_half_bars)
        metrics.price_recent_high = max(safe_float(b.get("h"), 0) for b in second_half_bars)
        metrics.macd_prior_high = max(first_half_macd) if first_half_macd else 0
        metrics.macd_recent_high = max(second_half_macd) if second_half_macd else 0

        price_higher_high = metrics.price_recent_high > metrics.price_prior_high * 1.0005
        macd_lower_high = metrics.macd_recent_high < metrics.macd_prior_high * 0.995

        if price_higher_high and macd_lower_high:
            metrics.bearish_momentum_divergence = True
            metrics.momentum_status = "BEARISH_DIVERGENCE"

    # Pullback structure from last 3x 5-min bars.
    metrics.pullback_high = max(safe_float(b.get("h"), 0) for b in recent3)
    lows3 = [safe_float(b.get("l"), 0) for b in recent3 if safe_float(b.get("l"), 0) > 0]
    metrics.pullback_low = min(lows3) if lows3 else metrics.price
    metrics.recent_swing_low = metrics.pullback_low

    # VWAP hold: 2 of last 3 structure bars should close above VWAP and not undercut it heavily.
    hold_count = 0
    for b in recent3:
        low = safe_float(b.get("l"), 0)
        close = safe_float(b.get("c"), 0)
        if metrics.vwap > 0 and low >= metrics.vwap * 0.995 and close >= metrics.vwap:
            hold_count += 1
    metrics.pullback_holding_vwap = hold_count >= min(2, len(recent3))

    # VWAP touch count on 5-min structure, not noisy 1-min wiggles.
    touches = 0
    for b in clean:
        high = safe_float(b.get("h"), 0)
        low = safe_float(b.get("l"), 0)
        close = safe_float(b.get("c"), 0)
        if metrics.vwap > 0 and (
            (low <= metrics.vwap <= high)
            or abs(pct_change(close, metrics.vwap)) <= 0.20
        ):
            touches += 1
    metrics.vwap_touch_count = touches

    # Base/flag compression from last 5x 5-min bars.
    recent_highs = [safe_float(b.get("h"), 0) for b in recent5 if safe_float(b.get("h"), 0) > 0]
    recent_lows = [safe_float(b.get("l"), 0) for b in recent5 if safe_float(b.get("l"), 0) > 0]

    if recent_highs and recent_lows:
        metrics.base_high = max(recent_highs)
        metrics.base_low = min(recent_lows)
        metrics.base_range_pct = pct(metrics.base_high - metrics.base_low, metrics.base_low) if metrics.base_low > 0 else 0

        # Higher-low or flat base:
        first_half = recent5[: max(1, len(recent5) // 2)]
        second_half = recent5[max(1, len(recent5) // 2):]
        first_lows = [safe_float(b.get("l"), 0) for b in first_half if safe_float(b.get("l"), 0) > 0]
        second_lows = [safe_float(b.get("l"), 0) for b in second_half if safe_float(b.get("l"), 0) > 0]

        if first_lows and second_lows:
            first_min = min(first_lows)
            second_min = min(second_lows)
            metrics.higher_low_or_flat_base = second_min >= first_min * 0.997

        metrics.price_near_base_breakout = metrics.price >= metrics.base_high * 0.995 if metrics.base_high > 0 else False

        metrics.base_compression = (
            metrics.base_range_pct <= 2.0
            and metrics.higher_low_or_flat_base
            and metrics.base_volume_constructive
            and metrics.price_near_base_breakout
        )

        recent_range_pct = pct(metrics.base_high - metrics.base_low, metrics.base_low) if metrics.base_low > 0 else 999
        metrics.consolidating_near_high = (
            metrics.hod > 0
            and pct_change(metrics.base_low, metrics.hod) >= -1.8
            and recent_range_pct <= 2.2
        )

    return metrics


def quote_spread_dollars(quote: Dict[str, Any]) -> Optional[float]:
    if not quote:
        return None

    bid = safe_float(quote.get("bp"), 0)
    ask = safe_float(quote.get("ap"), 0)

    if bid > 0 and ask > bid:
        return ask - bid

    return None


# ==============================================================
# RULEBOOK CALCULATIONS
# ==============================================================

def vwap_reclaim_zone(atr_pct: float) -> Tuple[float, float]:
    if atr_pct < 2.5:
        return -0.4, 0.3
    if atr_pct <= 5.0:
        return -0.6, 0.5
    return -0.9, 0.7


def vwap_condition_pass(metrics: IntradayMetrics, atr_pct: float) -> bool:
    if not metrics.has_data or metrics.vwap <= 0:
        return False

    low, high = vwap_reclaim_zone(atr_pct)
    return metrics.vwap_dist_pct >= 0 or (low <= metrics.vwap_dist_pct <= high)


def hod_condition_pass(metrics: IntradayMetrics, source_bucket: str) -> bool:
    if not metrics.has_data:
        return False

    if "POTENTIAL" in source_bucket:
        return metrics.hod_distance_pct >= POTENTIAL_HOD_MAX_DISTANCE

    return metrics.hod_distance_pct >= ACTIVE_HOD_MAX_DISTANCE


def severe_risk_off(regime: Dict[str, Any]) -> bool:
    bias = safe_str(regime.get("bias"), "").upper()
    label = safe_str(regime.get("label"), "").lower()
    vix = safe_float(regime.get("vix_level"), 0)
    spy = safe_float(regime.get("spy_change"), 0)
    qqq = safe_float(regime.get("qqq_change"), 0)

    return (
        bias == "CAUTION"
        or vix >= 25
        or "risk-off" in label
        or spy <= -1.5
        or qqq <= -1.8
    )


def sector_weak(row: Dict[str, Any]) -> bool:
    status = safe_str(row.get("sector_status"), "").upper()
    sector_score = safe_float(row.get("sector_score"), 0)
    return status in {"WEAK", "ROTATION_OUT"} or sector_score <= -3


def high_risk_extreme(row: Dict[str, Any]) -> bool:
    risk = safe_str(row.get("risk_category"), "").upper()
    bucket = safe_str(row.get("setup_bucket"), "").upper()
    return risk in {"HIGH_RISK_EXTREME", "EXTREME"} or bucket == "HIGH_RISK_EXTREME"


def is_relative_strength_long(row: Dict[str, Any], metrics: IntradayMetrics, regime: Dict[str, Any]) -> bool:
    if not metrics.has_data:
        return False

    spy = safe_float(regime.get("spy_change"), 0)
    qqq = safe_float(regime.get("qqq_change"), 0)
    iwm = safe_float(regime.get("iwm_change"), 0)

    benchmark = min(spy, qqq, iwm, 0)

    # Stock can be green, or at least outperform a sharply red benchmark by 2%.
    green = metrics.day_change_pct >= 0.40
    clear_outperformance = benchmark < 0 and metrics.day_change_pct >= benchmark + 2.0

    return (
        (green or clear_outperformance)
        and metrics.price > 0
        and vwap_condition_pass(metrics, safe_float(row.get("atr_pct"), 0))
        and not sector_weak(row)
        and not high_risk_extreme(row)
    )


def watch_criteria_pass(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    regime: Dict[str, Any],
) -> Tuple[bool, List[str], bool]:
    reasons: List[str] = []
    rs_long = is_relative_strength_long(row, metrics, regime)

    if high_risk_extreme(row):
        reasons.append("High risk/extreme category")
    if safe_float(row.get("dollar_vol_M"), 0) < MIN_AVG_DOLLAR_VOL_M:
        reasons.append("Dollar volume below $25M")
    if severe_risk_off(regime) and not rs_long:
        reasons.append("Market severe risk-off")
    if sector_weak(row):
        reasons.append("Sector weak")
    if not vwap_condition_pass(metrics, safe_float(row.get("atr_pct"), 0)):
        reasons.append("VWAP condition failed")
    if not rs_long and not hod_condition_pass(metrics, safe_str(row.get("signal_source_bucket"), "")):
        reasons.append("Too far below HOD")
    if metrics.vwap_dist_pct > MAX_VWAP_EXTENSION_PCT:
        reasons.append("Too extended above VWAP")

    return len(reasons) == 0, reasons, rs_long


# ==============================================================
# TRADE PLAN CONSTRUCTION — 5-MIN STRUCTURE
# ==============================================================

def trigger_level_for_setup(setup_type: str, metrics: IntradayMetrics) -> float:
    if setup_type == "VWAP_PULLBACK_CONTINUATION":
        return metrics.pullback_high * 1.0002
    if setup_type == "BASE_SQUEEZE_BREAKOUT":
        return metrics.base_high * 1.0002
    if setup_type == "HOD_BREAKOUT_CONTINUATION":
        return metrics.hod * 1.0002
    return max(metrics.pullback_high, metrics.base_high, metrics.hod, metrics.price) * 1.0002


def support_level_for_setup(setup_type: str, metrics: IntradayMetrics, entry: float) -> Tuple[float, str]:
    """
    Use only current 5-minute intraday structure.
    Do not use far-away daily support.
    """
    candidates: List[Tuple[float, str]] = []

    if setup_type == "VWAP_PULLBACK_CONTINUATION":
        if metrics.pullback_low > 0:
            candidates.append((metrics.pullback_low, "5Min pullback high/low"))
        if metrics.base_low > 0:
            candidates.append((metrics.base_low, "5Min base low"))

    elif setup_type == "BASE_SQUEEZE_BREAKOUT":
        if metrics.base_low > 0:
            candidates.append((metrics.base_low, "5Min base high/low"))
        if metrics.pullback_low > 0:
            candidates.append((metrics.pullback_low, "5Min pullback low"))

    elif setup_type == "HOD_BREAKOUT_CONTINUATION":
        if metrics.base_low > 0:
            candidates.append((metrics.base_low, "5Min HOD/base support"))
        if metrics.pullback_low > 0:
            candidates.append((metrics.pullback_low, "5Min pullback support"))

    else:
        if metrics.pullback_low > 0:
            candidates.append((metrics.pullback_low, "5Min pullback high/low"))
        if metrics.base_low > 0:
            candidates.append((metrics.base_low, "5Min base high/low"))

    # Keep only supports reasonably close to entry.
    filtered = []
    for level, label in candidates:
        if level > 0 and level < entry and pct(entry - level, entry) <= 3.0:
            filtered.append((level, label))

    if filtered:
        # Conservative but not broken: use closest valid structure support to avoid absurd stops.
        return max(filtered, key=lambda x: x[0])

    # No usable structure support.
    return 0.0, "No usable 5Min support within 3%"


def stop_buffer_pct(row: Dict[str, Any]) -> float:
    atr_pct = safe_float(row.get("atr_pct"), 0)

    if atr_pct <= 2.0:
        return 0.008
    if atr_pct <= 5.0:
        return 0.010
    return 0.012


def nearest_round_level_above(price: float) -> List[float]:
    if price <= 0:
        return []

    levels = []
    increments = [0.25, 0.5, 1.0] if price < 20 else [0.5, 1.0, 2.5, 5.0]

    for inc in increments:
        level = math.ceil(price / inc) * inc
        if level > price * 1.001:
            levels.append(level)

    return sorted(set(round(x, 4) for x in levels))


def resistance_levels_above(entry: float, metrics: IntradayMetrics, row: Dict[str, Any]) -> List[float]:
    levels: List[float] = []

    for level in [
        metrics.hod,
        metrics.base_high,
        safe_float(row.get("premarket_high"), 0),
        safe_float(row.get("prior_day_high"), 0),
        safe_float(row.get("resistance"), 0),
    ]:
        if level > entry * 1.001:
            levels.append(level)

    levels.extend(nearest_round_level_above(entry))

    # Extension levels above VWAP.
    if metrics.vwap > 0:
        for ext in [1.0, 1.5, 2.0, 3.0, 4.0]:
            level = metrics.vwap * (1 + ext / 100.0)
            if level > entry * 1.001:
                levels.append(level)

    return sorted(set(round(x, 4) for x in levels if x > entry * 1.001))


def pick_target_1(entry: float, risk: float, metrics: IntradayMetrics, row: Dict[str, Any]) -> Tuple[float, float]:
    min_target = entry + MIN_RR * risk
    levels = resistance_levels_above(entry, metrics, row)
    usable = [x for x in levels if x >= min_target]

    if usable:
        return min(usable), min_target

    return min_target, min_target


def build_trade_plan(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    setup_type: str,
    quote: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build entry, stop, targets, and R/R using 5-minute structure.
    Invalid plans remain diagnostics only and cannot become WATCH.
    """
    price = metrics.price or safe_float(row.get("price"), 0)

    entry = trigger_level_for_setup(setup_type, metrics)
    if entry <= 0 and price > 0:
        entry = price * 1.0002

    support, structure_label = support_level_for_setup(setup_type, metrics, entry)

    valid = True
    rejection_reason = ""

    if entry <= 0 or support <= 0:
        valid = False
        rejection_reason = "No usable 5Min structure support"
        stop = 0.0
        risk = 0.0
        stop_distance_pct = 999.0
        target_1 = 0.0
        target_2 = 0.0
        rr = 0.0
        min_target_1 = 0.0
    else:
        buffer = stop_buffer_pct(row)
        spread = quote_spread_dollars(quote or {})

        # Breathing room under real 5-min support.
        stop = support * (1 - buffer)

        # If spread is wider than the normal buffer, add it as extra dollars.
        if spread is not None and spread > 0 and price > 0:
            spread_pct = spread / price
            if spread_pct > buffer:
                stop = support - (2 * spread)

        risk = entry - stop
        stop_distance_pct = pct(risk, entry) if entry > 0 else 999.0

        max_stop = MAX_STOP_DIST_HOD if setup_type == "HOD_BREAKOUT_CONTINUATION" else MAX_STOP_DIST_NORMAL

        if entry <= 0 or stop <= 0 or risk <= 0:
            valid = False
            rejection_reason = "Invalid entry/stop"
        elif stop_distance_pct > max_stop:
            valid = False
            rejection_reason = f"Stop distance {stop_distance_pct:.2f}% > max {max_stop:.1f}%"

        target_1, min_target_1 = pick_target_1(entry, risk, metrics, row)
        target_2 = max(entry + 2.0 * risk, target_1 + 0.5 * risk)

        rr = (target_1 - entry) / risk if risk > 0 else 0.0

        if valid and rr < MIN_RR:
            valid = False
            rejection_reason = "Target 1 R/R below 1.5"

    return {
        "valid": valid,
        "rejection_reason": rejection_reason,
        "entry_trigger": round(entry, 4),
        "stop_loss": round(stop, 4),
        "target_1": round(target_1, 4),
        "target_2": round(target_2, 4),
        "reward_risk": round(rr, 2),
        "stop_distance_pct": round(stop_distance_pct, 2),
        "support_level": round(support, 4),
        "structure_label": structure_label,
        "min_target_1": round(min_target_1, 4),
        "buffer_pct_used": round(stop_buffer_pct(row) * 100, 2),
        "spread_dollars": round(quote_spread_dollars(quote or {}), 4) if quote_spread_dollars(quote or {}) is not None else None,
    }


def choose_best_provisional_plan(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    quote: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    """
    Build all setup plans and choose the best valid one.
    Invalid plans are kept only if no valid plan exists, for diagnostics.
    """
    setup_order = [
        "VWAP_PULLBACK_CONTINUATION",
        "BASE_SQUEEZE_BREAKOUT",
        "HOD_BREAKOUT_CONTINUATION",
    ]

    plans = []
    for setup in setup_order:
        plan = build_trade_plan(row, metrics, setup, quote)
        plans.append((setup, plan))

    valid_plans = [(s, p) for s, p in plans if p.get("valid") and safe_float(p.get("reward_risk"), 0) >= MIN_RR_WATCH]

    if valid_plans:
        # Prefer better R/R, but keep setup order as tie-break.
        valid_plans.sort(key=lambda x: (-safe_float(x[1].get("reward_risk"), 0), setup_order.index(x[0])))
        return valid_plans[0]

    # Return the "least bad" plan for diagnostics.
    plans.sort(key=lambda x: (-safe_float(x[1].get("reward_risk"), 0), safe_float(x[1].get("stop_distance_pct"), 999)))
    return plans[0]


# ==============================================================
# SETUP READINESS
# ==============================================================


def ema9_confirmation_score(metrics: IntradayMetrics) -> float:
    """
    EMA9 is a confirmation layer, not a primary WATCH trigger.
    Positive alignment improves score. Bad alignment penalizes later.
    """
    if metrics.ema9 <= 0:
        return 0.0

    score = 0.0
    if metrics.price_above_ema9:
        score += 3.0
    if metrics.ema9_rising:
        score += 2.0
    if metrics.ema9_above_vwap:
        score += 3.0
    if metrics.ema9_crossed_above_vwap_recent:
        score += 4.0

    return round(min(EMA9_BULLISH_BONUS_MAX, score), 2)


def ema9_ready_confirmation(metrics: IntradayMetrics, late_day: Optional[bool] = None) -> Tuple[bool, str]:
    """
    Trigger Ready should have EMA9 confirmation.
    WATCH does not require it, because a valid VWAP pullback may still be forming.
    """
    if not metrics.has_data:
        return False, "No intraday bars"

    if metrics.ema9 <= 0:
        return True, "EMA9 unavailable"

    if not metrics.price_above_ema9:
        return False, "Price below EMA9"

    if metrics.ema9_falling:
        return False, "EMA9 falling"

    if late_day is None:
        late_day = is_late_day()

    if late_day:
        if not (metrics.ema9_above_vwap or metrics.ema9_crossed_above_vwap_recent):
            return False, "Late-day setup needs EMA9 above VWAP or recent EMA9/VWAP bullish cross"

    return True, "EMA9 confirmation valid"


def setup_vwap_pullback_ready(metrics: IntradayMetrics, rr: float, confidence: float) -> Tuple[bool, str]:
    if not metrics.has_data:
        return False, "No intraday bars"

    if not metrics.above_vwap:
        return False, "Below VWAP"

    ema_ok, ema_reason = ema9_ready_confirmation(metrics)
    if not ema_ok:
        return False, ema_reason

    if metrics.bearish_momentum_divergence:
        return False, "Bearish MACD/momentum divergence"

    if metrics.volume_fading_vs_morning:
        return False, "Volume fading vs morning reference"

    if is_late_day() and not late_day_volume_confirmed(metrics):
        return False, "Late-day setup needs volume expansion"

    if not metrics.pullback_holding_vwap:
        return False, "No clean 5-min VWAP hold"

    if metrics.volume_drying and not metrics.base_volume_constructive:
        return False, "Pullback volume not constructive"

    if metrics.vwap_touch_count >= 4:
        return False, "4th+ VWAP touch"

    if rr < MIN_RR:
        return False, "R/R below 1.5"

    required_conf = ready_confidence_required("VALID_AFTERNOON" if is_late_day() else "VALID_MORNING")
    if confidence < required_conf:
        return False, f"Confidence below {required_conf:.0f}"

    return True, "5-min VWAP pullback holding; waiting for break above pullback high"


def base_squeeze_not_ready_reason(metrics: IntradayMetrics, rr: float, confidence: float) -> str:
    reasons = []

    if not metrics.has_data:
        reasons.append("No intraday bars")
    if not metrics.above_vwap:
        reasons.append("Below VWAP")
    ema_ok, ema_reason = ema9_ready_confirmation(metrics)
    if not ema_ok:
        reasons.append(ema_reason)
    if metrics.bearish_momentum_divergence:
        reasons.append("bearish MACD/momentum divergence")
    if metrics.volume_fading_vs_morning:
        reasons.append("volume fading vs morning reference")
    if is_late_day() and not late_day_volume_confirmed(metrics):
        reasons.append("late-day setup needs volume expansion")
    if metrics.base_range_pct > 2.0:
        reasons.append(f"base range {metrics.base_range_pct:.2f}% > 2.0%")
    if not metrics.higher_low_or_flat_base:
        reasons.append("no higher-low/flat-base structure")
    if not metrics.base_volume_constructive:
        reasons.append("base volume not contracting/stable")
    if not metrics.price_near_base_breakout:
        reasons.append("price not close to base breakout")
    if metrics.hod > 0 and pct_change(metrics.base_low, metrics.hod) < -2.5:
        reasons.append("Base too far from HOD")
    if rr < MIN_RR:
        reasons.append("R/R below 1.5")
    required_conf = ready_confidence_required("VALID_AFTERNOON" if is_late_day() else "VALID_MORNING")
    if confidence < required_conf:
        reasons.append(f"Confidence below {required_conf:.0f}")

    return "; ".join(reasons) if reasons else "Base/flag squeeze not ready"


def setup_base_squeeze_ready(metrics: IntradayMetrics, rr: float, confidence: float) -> Tuple[bool, str]:
    if not metrics.has_data:
        return False, "No intraday bars"

    if not metrics.above_vwap:
        return False, "Below VWAP"

    ema_ok, ema_reason = ema9_ready_confirmation(metrics)
    if not ema_ok:
        return False, ema_reason

    if metrics.bearish_momentum_divergence:
        return False, "Bearish MACD/momentum divergence"

    if metrics.volume_fading_vs_morning:
        return False, "Volume fading vs morning reference"

    if is_late_day() and not late_day_volume_confirmed(metrics):
        return False, "Late-day setup needs volume expansion"

    if not metrics.base_compression:
        return False, f"No clean base/flag compression: {base_squeeze_not_ready_reason(metrics, rr, confidence)}"

    if metrics.hod > 0 and pct_change(metrics.base_low, metrics.hod) < -2.5:
        return False, "Base too far from HOD"

    if rr < MIN_RR:
        return False, "R/R below 1.5"

    required_conf = ready_confidence_required("VALID_AFTERNOON" if is_late_day() else "VALID_MORNING")
    if confidence < required_conf:
        return False, f"Confidence below {required_conf:.0f}"

    return True, "5-min base/flag squeeze; waiting for break above compression high"


def setup_hod_breakout_ready(metrics: IntradayMetrics, rr: float, confidence: float) -> Tuple[bool, str]:
    if not metrics.has_data:
        return False, "No intraday bars"

    if not metrics.above_vwap:
        return False, "Below VWAP"

    ema_ok, ema_reason = ema9_ready_confirmation(metrics)
    if not ema_ok:
        return False, ema_reason

    if metrics.bearish_momentum_divergence:
        return False, "Bearish MACD/momentum divergence"

    if metrics.volume_fading_vs_morning:
        return False, "Volume fading vs morning reference"

    if is_late_day() and not late_day_volume_confirmed(metrics):
        return False, "Late-day setup needs volume expansion"

    if metrics.hod_distance_pct < HOD_BREAKOUT_READY_DISTANCE:
        return False, "Not close enough to HOD"

    if metrics.vwap_dist_pct > MAX_VWAP_EXTENSION_PCT:
        return False, "Too extended above VWAP"

    if not metrics.consolidating_near_high:
        return False, "Not consolidating near HOD"

    if not metrics.volume_stable_or_increasing:
        return False, "Volume not stable/increasing"

    if rr < MIN_RR:
        return False, "R/R below 1.5"

    required_conf = ready_confidence_required("VALID_AFTERNOON" if is_late_day() else "VALID_MORNING")
    if confidence < required_conf:
        return False, f"Confidence below {required_conf:.0f}"

    return True, "5-min HOD breakout setup; waiting for break above HOD"


def choose_setup(metrics: IntradayMetrics, plan: Dict[str, Any], confidence: float, phase: str) -> Tuple[str, str, List[str]]:
    rr = safe_float(plan.get("reward_risk"), 0)
    reasons = []

    vwap_ready, vwap_reason = setup_vwap_pullback_ready(metrics, rr, confidence)
    if vwap_ready:
        return "VWAP_PULLBACK_CONTINUATION", vwap_reason, reasons
    reasons.append(f"VWAP pullback not ready: {vwap_reason}")

    base_ready, base_reason = setup_base_squeeze_ready(metrics, rr, confidence)
    if base_ready:
        return "BASE_SQUEEZE_BREAKOUT", base_reason, reasons
    reasons.append(f"Base/flag squeeze not ready: {base_reason}")

    hod_ready, hod_reason = setup_hod_breakout_ready(metrics, rr, confidence)
    if hod_ready:
        return "HOD_BREAKOUT_CONTINUATION", hod_reason, reasons
    reasons.append(f"HOD breakout not ready: {hod_reason}")

    return "", "No trigger-ready setup", reasons


# ==============================================================
# SCORING
# ==============================================================

def live_signal_score(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    plan: Dict[str, Any],
    regime: Dict[str, Any],
    phase: str,
) -> float:
    if not metrics.has_data:
        return 0.0

    # 1. VWAP structure: 20
    vwap_score = 0.0
    if metrics.above_vwap:
        if 0 <= metrics.vwap_dist_pct <= 2.0:
            vwap_score = 20
        elif 2.0 < metrics.vwap_dist_pct <= 3.0:
            vwap_score = 16
        elif 3.0 < metrics.vwap_dist_pct <= 5.0:
            vwap_score = 9
        else:
            vwap_score = 3
    else:
        low, high = vwap_reclaim_zone(safe_float(row.get("atr_pct"), 0))
        vwap_score = 6 if low <= metrics.vwap_dist_pct <= high else 0

    # 2. EMA9 confirmation: 10
    ema_score = ema9_confirmation_score(metrics)

    # 3. HOD/range position: 15
    if metrics.hod_distance_pct >= -0.75:
        hod_score = 15
    elif metrics.hod_distance_pct >= -2.5:
        hod_score = 10
    elif metrics.hod_distance_pct >= -4.0:
        hod_score = 6
    else:
        hod_score = 0

    # 3. Volume/structure: 15
    if metrics.recent_volume_expanding and not metrics.volume_fading_vs_morning:
        volume_score = 15
    elif metrics.base_compression and not metrics.volume_fading_vs_morning:
        volume_score = 13
    elif metrics.volume_stable_or_increasing and not metrics.volume_fading_vs_morning:
        volume_score = 11
    elif metrics.base_volume_constructive and not metrics.volume_fading_vs_morning:
        volume_score = 8
    elif metrics.avg_volume_5 > 0:
        volume_score = 4
    else:
        volume_score = 0

    # 4. Risk/reward and plan validity: 15
    rr = safe_float(plan.get("reward_risk"), 0)
    plan_valid = bool(plan.get("valid"))
    if plan_valid and rr >= 2.5:
        rr_score = 15
    elif plan_valid and rr >= 2.0:
        rr_score = 12
    elif plan_valid and rr >= 1.5:
        rr_score = 10
    elif plan_valid and rr >= 0.75:
        rr_score = 5
    else:
        rr_score = 0

    # 5. Sector + market alignment: 15
    sector_status = safe_str(row.get("sector_status"), "").upper()
    if sector_status == "LEADING":
        sector_score = 10
    elif sector_status in {"IMPROVING", "NEUTRAL", "UNKNOWN", ""}:
        sector_score = 6
    elif sector_status == "WEAK":
        sector_score = 0
    else:
        sector_score = 4

    rs_long = is_relative_strength_long(row, metrics, regime)
    if severe_risk_off(regime):
        market_score = 5 if rs_long else 0
    else:
        market_score = 5

    # 6. Time-of-day: 10
    if phase in {"VALID_MORNING", "VALID_AFTERNOON"}:
        time_score = 10
    elif phase == "PREMARKET":
        time_score = 4
    else:
        time_score = 0

    # 7. Extension / touch penalty: up to -10
    penalty = 0
    if metrics.vwap_dist_pct > 5.0:
        penalty += 7
    elif metrics.vwap_dist_pct > 3.0:
        penalty += 4

    if metrics.vwap_touch_count >= 4:
        penalty += 3

    if metrics.ema9 > 0:
        if not metrics.price_above_ema9:
            penalty += EMA9_BELOW_PRICE_PENALTY
        if metrics.ema9_falling:
            penalty += EMA9_FALLING_PENALTY
        if not metrics.ema9_above_vwap and not metrics.ema9_crossed_above_vwap_recent:
            penalty += EMA9_BELOW_VWAP_PENALTY

    if metrics.hod_distance_pct < -4.0:
        penalty += 3

    if metrics.volume_fading_vs_morning:
        penalty += VOLUME_FADE_PENALTY

    if metrics.bearish_momentum_divergence:
        penalty += BEARISH_DIVERGENCE_PENALTY

    if is_late_day() and not late_day_volume_confirmed(metrics):
        penalty += LATE_DAY_NO_VOLUME_EXPANSION_PENALTY

    score = (
        vwap_score
        + hod_score
        + ema_score
        + volume_score
        + rr_score
        + sector_score
        + market_score
        + time_score
        - min(25, penalty)
    )

    return round(clamp(score, 0, 100), 2)


def final_confidence(scanner_score: float, live_score: float) -> float:
    return round(clamp(0.40 * scanner_score + 0.60 * live_score, 0, 100), 1)


# ==============================================================
# DIAGNOSTICS
# ==============================================================

def not_ready_reasons(metrics: IntradayMetrics, plan: Dict[str, Any], confidence: float) -> List[str]:
    reasons: List[str] = []

    if not plan.get("valid"):
        reasons.append(f"Plan invalid: {safe_str(plan.get('rejection_reason'), 'Invalid plan')}")

    required_conf = ready_confidence_required("VALID_AFTERNOON" if is_late_day() else "VALID_MORNING")
    if confidence < required_conf:
        reasons.append(f"Confidence {confidence:.1f} < ready minimum {required_conf:.0f}")

    rr = safe_float(plan.get("reward_risk"), 0)
    if rr < MIN_RR:
        reasons.append(f"R/R {rr:.2f} < minimum 1.5")

    _, vwap_reason = setup_vwap_pullback_ready(metrics, rr, confidence)
    reasons.append(f"VWAP pullback not ready: {vwap_reason}")

    _, base_reason = setup_base_squeeze_ready(metrics, rr, confidence)
    reasons.append(f"Base/flag squeeze not ready: {base_reason}")

    _, hod_reason = setup_hod_breakout_ready(metrics, rr, confidence)
    reasons.append(f"HOD breakout not ready: {hod_reason}")

    # De-duplicate while preserving order.
    out = []
    seen = set()
    for r in reasons:
        if r not in seen:
            out.append(r)
            seen.add(r)
    return out


def diagnostic_candidate(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    plan: Dict[str, Any],
    live: float,
    conf: float,
    rejected_reasons: List[str],
    phase: str,
    regime: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "symbol": normalize_symbol(row.get("symbol")),
        "diagnostic_status": "REJECTED",
        "scanner_score": safe_float(row.get("score"), 0),
        "source_bucket": safe_str(row.get("signal_source_bucket"), ""),
        "signal_rank": safe_int(row.get("signal_rank"), 0),
        "market_phase": phase,
        "sector_status": safe_str(row.get("sector_status"), ""),
        "risk_category": safe_str(row.get("risk_category"), "NORMAL"),
        "setup_bucket": safe_str(row.get("setup_bucket"), ""),
        "dollar_vol_M": safe_float(row.get("dollar_vol_M"), 0),
        "atr_pct": safe_float(row.get("atr_pct"), 0),
        "price": round(metrics.price, 4),
        "session_open": round(metrics.session_open, 4),
        "day_change_pct": round(metrics.day_change_pct, 2),
        "relative_strength_long": is_relative_strength_long(row, metrics, regime),
        "vwap": round(metrics.vwap, 4),
        "hod": round(metrics.hod, 4),
        "vwap_dist_pct": round(metrics.vwap_dist_pct, 2),
        "hod_distance_pct": round(metrics.hod_distance_pct, 2),
        "above_vwap": metrics.above_vwap,
        "ema9": round(metrics.ema9, 4),
        "ema9_prev": round(metrics.ema9_prev, 4),
        "ema9_slope_pct": round(metrics.ema9_slope_pct, 3),
        "price_above_ema9": metrics.price_above_ema9,
        "ema9_rising": metrics.ema9_rising,
        "ema9_falling": metrics.ema9_falling,
        "ema9_above_vwap": metrics.ema9_above_vwap,
        "ema9_crossed_above_vwap_recent": metrics.ema9_crossed_above_vwap_recent,
        "ema9_status": metrics.ema9_status,
        "base_compression": metrics.base_compression,
        "base_range_pct": round(metrics.base_range_pct, 2),
        "base_high": round(metrics.base_high, 4),
        "base_low": round(metrics.base_low, 4),
        "execution_timeframe": EXECUTION_TIMEFRAME,
        "structure_timeframe": STRUCTURE_TIMEFRAME,
        "structure_bar_count": metrics.structure_bar_count,
        "avg_volume_5": round(metrics.avg_volume_5, 2),
        "avg_volume_prev_5": round(metrics.avg_volume_prev_5, 2),
        "morning_avg_volume_5m": round(metrics.morning_avg_volume_5m, 2),
        "recent_to_morning_volume_ratio": round(metrics.recent_to_morning_volume_ratio, 2),
        "volume_fading_vs_morning": metrics.volume_fading_vs_morning,
        "recent_volume_expanding": metrics.recent_volume_expanding,
        "bearish_momentum_divergence": metrics.bearish_momentum_divergence,
        "momentum_status": metrics.momentum_status,
        "live_signal_score": round(live, 1),
        "confidence": round(conf, 1),
        "reward_risk": safe_float(plan.get("reward_risk"), 0),
        "plan_valid": bool(plan.get("valid")),
        "rejected_reasons": rejected_reasons,
        "not_ready_reasons": not_ready_reasons(metrics, plan, conf),
        "last_checked": iso_now_et(),
        "entry_trigger": plan.get("entry_trigger"),
        "stop_loss": plan.get("stop_loss"),
        "target_1": plan.get("target_1"),
        "target_2": plan.get("target_2"),
        "stop_distance_pct": plan.get("stop_distance_pct"),
        "support_level": plan.get("support_level"),
        "structure_label": plan.get("structure_label"),
        "min_target_1": plan.get("min_target_1"),
        "buffer_pct_used": plan.get("buffer_pct_used"),
    }


# ==============================================================
# SIGNAL STATE LOGIC
# ==============================================================

def signal_base(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    plan: Dict[str, Any],
    status: str,
    setup_type: str,
    confidence: float,
    live_score: float,
    reason: str,
    phase: str,
    regime: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    symbol = normalize_symbol(row.get("symbol"))
    now_text = iso_now_et()

    return {
        "symbol": symbol,
        "signal_status": status,
        "setup_type": setup_type or "MONITORING",
        "confidence": round(confidence, 1),
        "live_signal_score": round(live_score, 1),
        "scanner_score": safe_float(row.get("score"), 0),
        "source_bucket": safe_str(row.get("signal_source_bucket"), ""),
        "signal_rank": safe_int(row.get("signal_rank"), 0),
        "entry_trigger": plan.get("entry_trigger"),
        "stop_loss": plan.get("stop_loss"),
        "target_1": plan.get("target_1"),
        "target_2": plan.get("target_2"),
        "reward_risk": plan.get("reward_risk"),
        "stop_distance_pct": plan.get("stop_distance_pct"),
        "support_level": plan.get("support_level"),
        "structure_label": plan.get("structure_label"),
        "min_target_1": plan.get("min_target_1"),
        "buffer_pct_used": plan.get("buffer_pct_used"),
        "atr_pct": safe_float(row.get("atr_pct"), 0),
        "dollar_vol_M": safe_float(row.get("dollar_vol_M"), 0),
        "risk_category": safe_str(row.get("risk_category"), "NORMAL"),
        "setup_bucket": safe_str(row.get("setup_bucket"), ""),
        "price": round(metrics.price, 4),
        "session_open": round(metrics.session_open, 4),
        "day_change_pct": round(metrics.day_change_pct, 2),
        "relative_strength_long": is_relative_strength_long(row, metrics, regime or {}),
        "vwap": round(metrics.vwap, 4),
        "hod": round(metrics.hod, 4),
        "vwap_dist_pct": round(metrics.vwap_dist_pct, 2),
        "hod_distance_pct": round(metrics.hod_distance_pct, 2),
        "above_vwap": metrics.above_vwap,
        "ema9": round(metrics.ema9, 4),
        "ema9_prev": round(metrics.ema9_prev, 4),
        "ema9_slope_pct": round(metrics.ema9_slope_pct, 3),
        "price_above_ema9": metrics.price_above_ema9,
        "ema9_rising": metrics.ema9_rising,
        "ema9_falling": metrics.ema9_falling,
        "ema9_above_vwap": metrics.ema9_above_vwap,
        "ema9_crossed_above_vwap_recent": metrics.ema9_crossed_above_vwap_recent,
        "ema9_status": metrics.ema9_status,
        "base_compression": metrics.base_compression,
        "base_range_pct": round(metrics.base_range_pct, 2),
        "base_high": round(metrics.base_high, 4),
        "base_low": round(metrics.base_low, 4),
        "execution_timeframe": EXECUTION_TIMEFRAME,
        "structure_timeframe": STRUCTURE_TIMEFRAME,
        "structure_bar_count": metrics.structure_bar_count,
        "avg_volume_5": round(metrics.avg_volume_5, 2),
        "avg_volume_prev_5": round(metrics.avg_volume_prev_5, 2),
        "morning_avg_volume_5m": round(metrics.morning_avg_volume_5m, 2),
        "recent_to_morning_volume_ratio": round(metrics.recent_to_morning_volume_ratio, 2),
        "volume_fading_vs_morning": metrics.volume_fading_vs_morning,
        "recent_volume_expanding": metrics.recent_volume_expanding,
        "bearish_momentum_divergence": metrics.bearish_momentum_divergence,
        "momentum_status": metrics.momentum_status,
        "sector_status": safe_str(row.get("sector_status"), ""),
        "market_phase": phase,
        "reason": reason,
        "invalidation": "Lose VWAP, lose trigger, break pullback/base low, stop would hit, or signal becomes stale.",
        "last_checked": now_text,
        "updated_at": now_text,
        "session_date": session_date_str(),
        "actionable": status == "ACTIVE_SIGNAL",
        "actionability": (
            "ACTIVE" if status == "ACTIVE_SIGNAL"
            else "TRIGGER_READY" if status == "TRIGGER_READY"
            else "WATCH"
        ),
        "suppression_reason": "",
        "risk_flags": safe_str(row.get("risk_flags"), ""),
        "company_name": safe_str(row.get("company_name"), ""),
        "not_ready_reasons": not_ready_reasons(metrics, plan, confidence) if status == "WATCH" else [],
    }


def make_invalidated(
    existing: Dict[str, Any],
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    reason: str,
    category: str,
    phase: str,
) -> Dict[str, Any]:
    symbol = normalize_symbol(existing.get("symbol") or row.get("symbol"))
    now_text = iso_now_et()

    out = dict(existing)
    out.update({
        "symbol": symbol,
        "signal_status": "INVALIDATED",
        "invalidation_reason": category,
        "reason": reason,
        "actionable": False,
        "actionability": "INVALIDATED",
        "last_checked": now_text,
        "invalidated_at": now_text,
        "updated_at": now_text,
        "market_phase": phase,
        "price": round(metrics.price, 4) if metrics.has_data else out.get("price", 0),
        "vwap": round(metrics.vwap, 4) if metrics.has_data else out.get("vwap", 0),
        "hod": round(metrics.hod, 4) if metrics.has_data else out.get("hod", 0),
        "vwap_dist_pct": round(metrics.vwap_dist_pct, 2) if metrics.has_data else out.get("vwap_dist_pct", 0),
        "hod_distance_pct": round(metrics.hod_distance_pct, 2) if metrics.has_data else out.get("hod_distance_pct", 0),
    })
    return out


def is_previous_session(signal: Dict[str, Any]) -> bool:
    return safe_str(signal.get("session_date"), "") not in {"", session_date_str()}


def expired_by_age(signal: Dict[str, Any], max_minutes: int, now: Optional[datetime] = None) -> bool:
    keys = ["triggered_at", "ready_since", "detected_at", "updated_at"]
    for key in keys:
        val = signal.get(key)
        age = minutes_since(val, now)
        if age is not None:
            return age > max_minutes
    return False


def trigger_fired(existing: Dict[str, Any], metrics: IntradayMetrics) -> bool:
    trigger = safe_float(existing.get("entry_trigger"), 0)
    if trigger <= 0 or not metrics.has_data:
        return False
    return metrics.price >= trigger and metrics.above_vwap and metrics.price_above_ema9


def active_invalidated(signal: Dict[str, Any], metrics: IntradayMetrics) -> Tuple[bool, str, str]:
    if not metrics.has_data:
        return True, "No intraday data available", "EXTERNAL_RISK"

    trigger = safe_float(signal.get("entry_trigger"), 0)
    stop = safe_float(signal.get("stop_loss"), 0)

    if not metrics.above_vwap:
        return True, "Lost VWAP after active signal", "FAILED_SETUP"

    if trigger > 0 and metrics.price < trigger:
        return True, "Price fell back below trigger", "FAILED_SETUP"

    if stop > 0 and metrics.price <= stop:
        return True, "Stop would have been hit", "FAILED_SETUP"

    if expired_by_age(signal, ACTIVE_STALE_MINUTES):
        return True, "Active signal stale for more than 2 refresh cycles", "MISSED_WINDOW"

    return False, "", ""


def ready_invalidated(signal: Dict[str, Any], metrics: IntradayMetrics) -> Tuple[bool, str, str]:
    if not metrics.has_data:
        return True, "No intraday data available", "EXTERNAL_RISK"

    support = safe_float(signal.get("support_level"), 0)
    trigger = safe_float(signal.get("entry_trigger"), 0)

    if not metrics.above_vwap:
        return True, "Lost VWAP before trigger", "FAILED_SETUP"

    if support > 0 and metrics.price < support:
        return True, "Broke setup support before trigger", "FAILED_SETUP"

    if metrics.ema9 > 0 and not metrics.price_above_ema9:
        return True, "Price lost EMA9 before trigger", "FAILED_SETUP"

    if is_late_day() and metrics.ema9_falling:
        return True, "Late-day setup EMA9 turned down before trigger", "FAILED_SETUP"

    if metrics.bearish_momentum_divergence:
        return True, "Bearish momentum divergence developed before trigger", "FAILED_SETUP"

    if metrics.volume_fading_vs_morning:
        return True, "Volume faded versus morning reference before trigger", "FAILED_SETUP"

    if is_late_day() and not late_day_volume_confirmed(metrics):
        return True, "Late-day trigger-ready setup lacks volume expansion", "FAILED_SETUP"

    if expired_by_age(signal, TRIGGER_READY_STALE_MINUTES):
        return True, "Trigger-ready setup became stale", "MISSED_WINDOW"

    if trigger > 0 and pct_change(metrics.price, trigger) > 2.0:
        return True, "Price moved too far above trigger without valid active signal", "MISSED_WINDOW"

    return False, "", ""


def suppress_trigger_during_blackout(
    signal: Dict[str, Any],
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    phase: str,
    regime: Dict[str, Any],
) -> Dict[str, Any]:
    now_text = iso_now_et()
    out = dict(signal)
    out.update({
        "signal_status": "TRIGGER_READY",
        "actionable": False,
        "actionability": "SUPPRESSED",
        "suppression_reason": f"{phase}_TRIGGER",
        "blackout_trigger_price": round(metrics.price, 4),
        "blackout_trigger_time": now_text,
        "requires_fresh_trigger": True,
        "reason": f"Trigger fired during {phase.lower().replace('_', ' ')}. No active signal generated.",
        "last_checked": now_text,
        "updated_at": now_text,
        "market_phase": phase,
        "price": round(metrics.price, 4),
        "vwap": round(metrics.vwap, 4),
        "hod": round(metrics.hod, 4),
    })

    append_suppressed_signal({
        "timestamp_et": now_text,
        "symbol": normalize_symbol(row.get("symbol")),
        "setup_type": safe_str(signal.get("setup_type"), ""),
        "scanner_score": safe_float(row.get("score"), 0),
        "live_signal_score": safe_float(signal.get("live_signal_score"), 0),
        "confidence": safe_float(signal.get("confidence"), 0),
        "entry_trigger": safe_float(signal.get("entry_trigger"), 0),
        "stop_loss": safe_float(signal.get("stop_loss"), 0),
        "target_1": safe_float(signal.get("target_1"), 0),
        "target_2": safe_float(signal.get("target_2"), 0),
        "reward_risk": safe_float(signal.get("reward_risk"), 0),
        "suppression_reason": f"{phase}_TRIGGER",
        "price_at_trigger": metrics.price,
        "vwap": metrics.vwap,
        "hod_distance_pct": metrics.hod_distance_pct,
        "vwap_distance_pct": metrics.vwap_dist_pct,
        "sector_status": safe_str(row.get("sector_status"), ""),
        "market_regime": safe_str(regime.get("label"), ""),
        "notes": "Trigger suppressed by Signal Desk v1 blackout rule.",
    })

    return out


def process_existing_signal(
    existing: Dict[str, Any],
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    quote: Dict[str, Any],
    regime: Dict[str, Any],
    phase: str,
) -> Dict[str, Any]:
    status = normalize_status(existing.get("signal_status"))
    now_text = iso_now_et()

    if is_previous_session(existing):
        return make_invalidated(existing, row, metrics, "Previous session signal expired.", "MISSED_WINDOW", phase)

    if status == "ACTIVE_SIGNAL":
        invalid, reason, category = active_invalidated(existing, metrics)
        if invalid:
            return make_invalidated(existing, row, metrics, reason, category, phase)

        out = dict(existing)
        out.update({
            "last_checked": now_text,
            "updated_at": now_text,
            "market_phase": phase,
            "price": round(metrics.price, 4),
            "vwap": round(metrics.vwap, 4),
            "hod": round(metrics.hod, 4),
            "vwap_dist_pct": round(metrics.vwap_dist_pct, 2),
            "hod_distance_pct": round(metrics.hod_distance_pct, 2),
            "ema9": round(metrics.ema9, 4),
            "price_above_ema9": metrics.price_above_ema9,
            "ema9_status": metrics.ema9_status,
            "actionable": True,
            "actionability": "ACTIVE",
        })
        return out

    if status == "TRIGGER_READY":
        if trigger_fired(existing, metrics) and not is_valid_signal_phase(phase):
            return suppress_trigger_during_blackout(existing, row, metrics, phase, regime)

        invalid, reason, category = ready_invalidated(existing, metrics)
        if invalid:
            return make_invalidated(existing, row, metrics, reason, category, phase)

        if trigger_fired(existing, metrics) and is_valid_signal_phase(phase):
            setup_type = safe_str(existing.get("setup_type"), "")
            plan = build_trade_plan(row, metrics, setup_type, quote)
            live = live_signal_score(row, metrics, plan, regime, phase)
            conf = final_confidence(safe_float(row.get("score"), safe_float(existing.get("scanner_score"), 0)), live)

            active_quality_ok = (
                plan.get("valid")
                and conf >= MIN_CONF_ACTIVE
                and safe_float(plan.get("reward_risk"), 0) >= MIN_RR
                and not metrics.bearish_momentum_divergence
                and not metrics.volume_fading_vs_morning
                and metrics.price_above_ema9
                and not metrics.ema9_falling
                and late_day_volume_confirmed(metrics)
            )

            if active_quality_ok:
                out = signal_base(
                    row,
                    metrics,
                    plan,
                    "ACTIVE_SIGNAL",
                    setup_type,
                    conf,
                    live,
                    f"Trigger fired and still holds above VWAP. {safe_str(existing.get('reason'), '')}",
                    phase,
                    regime,
                )
                out["triggered_at"] = now_text
                out["detected_at"] = existing.get("detected_at") or existing.get("ready_since") or now_text
                out["ready_since"] = existing.get("ready_since") or now_text
                return out

            return make_invalidated(
                existing,
                row,
                metrics,
                plan.get("rejection_reason") or "Trigger fired but active-signal quality failed.",
                "FAILED_SETUP",
                phase,
            )

        out = dict(existing)
        out.update({
            "last_checked": now_text,
            "updated_at": now_text,
            "market_phase": phase,
            "price": round(metrics.price, 4),
            "vwap": round(metrics.vwap, 4),
            "hod": round(metrics.hod, 4),
            "vwap_dist_pct": round(metrics.vwap_dist_pct, 2),
            "hod_distance_pct": round(metrics.hod_distance_pct, 2),
            "ema9": round(metrics.ema9, 4),
            "price_above_ema9": metrics.price_above_ema9,
            "ema9_status": metrics.ema9_status,
            "actionable": False,
            "actionability": "TRIGGER_READY" if not existing.get("suppression_reason") else "SUPPRESSED",
        })
        return out

    if status == "INVALIDATED":
        age = minutes_since(existing.get("invalidated_at") or existing.get("updated_at"))
        if age is not None and age <= RECENT_INVALIDATED_KEEP_MINUTES:
            out = dict(existing)
            out["last_checked"] = now_text
            return out

    return {}


def process_new_or_watch(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    quote: Dict[str, Any],
    regime: Dict[str, Any],
    phase: str,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """
    Returns:
      (signal, diagnostic)
    Exactly one can be non-empty.
    """
    symbol = normalize_symbol(row.get("symbol"))

    if not metrics.has_data:
        return {}, None

    watch_ok, watch_reasons, rs_long = watch_criteria_pass(row, metrics, regime)

    setup_type, plan = choose_best_provisional_plan(row, metrics, quote)
    live = live_signal_score(row, metrics, plan, regime, phase)
    conf = final_confidence(safe_float(row.get("score"), 0), live)

    rejected_reasons = list(watch_reasons)

    if conf < MIN_CONF_WATCH:
        rejected_reasons.append(f"Confidence {conf:.1f} < WATCH minimum 60")

    if not plan.get("valid"):
        rejected_reasons.append(f"Plan invalid: {safe_str(plan.get('rejection_reason'), 'Invalid plan')}")

    if plan.get("valid") and safe_float(plan.get("reward_risk"), 0) < MIN_RR_WATCH:
        rejected_reasons.append(f"R/R {safe_float(plan.get('reward_risk'), 0):.2f} < WATCH minimum 0.75")

    # Premarket: monitor only, but still require usable plan for WATCH.
    if phase == "PREMARKET":
        if watch_ok and plan.get("valid") and safe_float(plan.get("reward_risk"), 0) >= MIN_RR_WATCH and conf >= MIN_CONF_WATCH:
            out = signal_base(
                row,
                metrics,
                plan,
                "WATCH",
                "PREMARKET_MONITOR",
                conf,
                live,
                "Premarket monitoring only. No trigger-ready or active signals before regular session.",
                phase,
                regime,
            )
            out["detected_at"] = iso_now_et()
            return out, None

        return {}, diagnostic_candidate(row, metrics, plan, live, conf, rejected_reasons, phase, regime)

    # Outside regular market open phases: no new signals.
    if not is_market_open_phase(phase):
        return {}, diagnostic_candidate(row, metrics, plan, live, conf, rejected_reasons or [f"Market phase {phase}"], phase, regime)

    # Blackout phases: WATCH only if usable; no READY.
    if not is_valid_signal_phase(phase):
        if watch_ok and plan.get("valid") and safe_float(plan.get("reward_risk"), 0) >= MIN_RR_WATCH and conf >= MIN_CONF_WATCH:
            out = signal_base(
                row,
                metrics,
                plan,
                "WATCH",
                "BLACKOUT_MONITOR",
                conf,
                live,
                f"Valid monitor candidate, but {phase.lower().replace('_', ' ')} prevents new signals.",
                phase,
                regime,
            )
            out["detected_at"] = iso_now_et()
            return out, None

        return {}, diagnostic_candidate(row, metrics, plan, live, conf, rejected_reasons, phase, regime)

    # Valid signal phase:
    # If the plan is invalid, it must NOT show as WATCH.
    if not watch_ok or not plan.get("valid") or safe_float(plan.get("reward_risk"), 0) < MIN_RR_WATCH or conf < MIN_CONF_WATCH:
        debug(f"  {symbol}: rejected: {rejected_reasons}")
        return {}, diagnostic_candidate(row, metrics, plan, live, conf, rejected_reasons, phase, regime)

    # Decide whether it is trigger-ready.
    ready_min_conf = ready_confidence_required(phase)
    if plan.get("valid") and safe_float(plan.get("reward_risk"), 0) >= MIN_RR and conf >= ready_min_conf:
        setup_ready_type, setup_reason, setup_fail_reasons = choose_setup(metrics, plan, conf, phase)

        if setup_ready_type:
            # Rebuild plan using the exact ready setup type.
            exact_plan = build_trade_plan(row, metrics, setup_ready_type, quote)
            exact_live = live_signal_score(row, metrics, exact_plan, regime, phase)
            exact_conf = final_confidence(safe_float(row.get("score"), 0), exact_live)

            exact_ready_min_conf = ready_confidence_required(phase)
            if exact_plan.get("valid") and safe_float(exact_plan.get("reward_risk"), 0) >= MIN_RR and exact_conf >= exact_ready_min_conf:
                out = signal_base(
                    row,
                    metrics,
                    exact_plan,
                    "TRIGGER_READY",
                    setup_ready_type,
                    exact_conf,
                    exact_live,
                    setup_reason,
                    phase,
                    regime,
                )
                now_text = iso_now_et()
                out["ready_since"] = now_text
                out["detected_at"] = now_text
                return out, None

        # Good plan, but setup itself is not ready. WATCH.
        out = signal_base(
            row,
            metrics,
            plan,
            "WATCH",
            "MONITORING",
            conf,
            live,
            "Clean candidate with usable 5-min trade plan. Waiting for VWAP pullback, base squeeze, or HOD breakout trigger.",
            phase,
            regime,
        )
        out["detected_at"] = iso_now_et()
        out["not_ready_reasons"] = setup_fail_reasons or not_ready_reasons(metrics, plan, conf)
        return out, None

    # WATCH only if plan is valid and at least minimally usable.
    out = signal_base(
        row,
        metrics,
        plan,
        "WATCH",
        "MONITORING",
        conf,
        live,
        "Relative-strength long candidate with usable 5-min trade plan. Waiting for setup quality to improve.",
        phase,
        regime,
    )
    out["detected_at"] = iso_now_et()
    return out, None


# ==============================================================
# MAIN ENGINE HELPERS
# ==============================================================

def build_row_lookup(focus: Dict[str, Dict[str, Any]], prior_state: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    rows = dict(focus)

    for sym, signal in prior_state.items():
        status = normalize_status(signal.get("signal_status"))
        if status not in {"TRIGGER_READY", "ACTIVE_SIGNAL"}:
            continue

        if sym in rows:
            continue

        rows[sym] = {
            "symbol": sym,
            "score": safe_float(signal.get("scanner_score"), 0),
            "sector_status": safe_str(signal.get("sector_status"), "UNKNOWN"),
            "signal_source_bucket": "PROTECTED",
            "risk_category": safe_str(signal.get("risk_category"), "NORMAL"),
            "dollar_vol_M": safe_float(signal.get("dollar_vol_M"), MIN_AVG_DOLLAR_VOL_M),
            "atr_pct": safe_float(signal.get("atr_pct"), 3.0),
            "company_name": safe_str(signal.get("company_name"), sym),
        }

    return rows


def prepare_monitor_symbols(focus: Dict[str, Dict[str, Any]], prior_state: Dict[str, Dict[str, Any]]) -> List[str]:
    symbols = set(focus.keys())

    for sym, signal in prior_state.items():
        status = normalize_status(signal.get("signal_status"))
        if status in {"TRIGGER_READY", "ACTIVE_SIGNAL"}:
            symbols.add(sym)

    return sorted(symbols)


def build_signal_outputs(new_state: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rank = {
        "ACTIVE_SIGNAL": 1,
        "ACTIVE": 1,
        "TRIGGER_READY": 2,
        "READY": 2,
        "WATCH": 3,
        "INVALIDATED": 4,
    }

    signals = [s for s in new_state.values() if s and normalize_status(s.get("signal_status")) != "WAIT"]

    signals.sort(
        key=lambda x: (
            rank.get(normalize_status(x.get("signal_status")), 9),
            -safe_float(x.get("confidence"), 0),
            -safe_float(x.get("reward_risk"), 0),
            safe_str(x.get("symbol"), ""),
        )
    )

    return signals


def summarize_signals(signals: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {
        "active": 0,
        "trigger_ready": 0,
        "watch": 0,
        "invalidated": 0,
        "suppressed": 0,
    }

    for s in signals:
        status = normalize_status(s.get("signal_status"))
        actionability = safe_str(s.get("actionability"), "").upper()

        if status == "ACTIVE_SIGNAL":
            counts["active"] += 1
        elif status == "TRIGGER_READY":
            counts["trigger_ready"] += 1
        elif status == "WATCH":
            counts["watch"] += 1
        elif status == "INVALIDATED":
            counts["invalidated"] += 1

        if actionability == "SUPPRESSED" or s.get("suppression_reason"):
            counts["suppressed"] += 1

    return counts


# ==============================================================
# MAIN ENGINE
# ==============================================================

def run_signal_engine() -> None:
    now = ny_now()
    phase = get_market_phase(now)

    log("============================================================")
    log("Signal Desk v1 Engine")
    log("============================================================")
    log(f"Time: {now.isoformat(timespec='seconds')}")
    log(f"Market phase: {phase}")
    log(f"Alpaca feed: {DATA_FEED}")
    log(f"Execution timeframe: {EXECUTION_TIMEFRAME}")
    log(f"Structure timeframe: {STRUCTURE_TIMEFRAME}")

    focus = load_focus_candidates()
    prior_state = load_signal_state()
    regime = load_json(MARKET_REGIME_FILE, {})

    monitor_symbols = prepare_monitor_symbols(focus, prior_state)
    row_lookup = build_row_lookup(focus, prior_state)

    log(f"Focus universe: {len(focus)} current scanner names")
    log(f"Monitor universe: {len(monitor_symbols)} including protected signals")

    market_data = AlpacaMarketData(DATA_FEED)

    bars_1m_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    bars_5m_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    quotes_by_symbol: Dict[str, Dict[str, Any]] = {}

    if market_data.available and monitor_symbols:
        bars_1m_by_symbol = market_data.fetch_bars(monitor_symbols, EXECUTION_TIMEFRAME)
        bars_5m_by_symbol = market_data.fetch_bars(monitor_symbols, STRUCTURE_TIMEFRAME)
        quotes_by_symbol = market_data.fetch_latest_quotes(monitor_symbols)
    elif not market_data.available:
        log("  ⚠ No Alpaca credentials. signal_desk.json will be empty or historical only.")

    new_state: Dict[str, Dict[str, Any]] = {}
    rejected_candidates: List[Dict[str, Any]] = []

    for sym in monitor_symbols:
        row = row_lookup.get(sym, {"symbol": sym})
        metrics = analyze_execution_bars(sym, bars_1m_by_symbol.get(sym, []))
        metrics = enrich_structure_from_5min(metrics, bars_5m_by_symbol.get(sym, []))

        quote = quotes_by_symbol.get(sym, {})
        existing = prior_state.get(sym, {})
        existing_status = normalize_status(existing.get("signal_status"))

        # Market closed: no new signals; expire protected signals.
        if phase in {"CLOSED", "AFTERHOURS"}:
            if existing_status in {"TRIGGER_READY", "ACTIVE_SIGNAL"}:
                invalid = make_invalidated(
                    existing,
                    row,
                    metrics,
                    f"Market phase {phase}; signal expired.",
                    "MISSED_WINDOW",
                    phase,
                )
                new_state[sym] = invalid
            continue

        if existing_status in {"TRIGGER_READY", "ACTIVE_SIGNAL", "INVALIDATED"}:
            processed = process_existing_signal(existing, row, metrics, quote, regime, phase)
            if processed:
                new_state[sym] = processed
                continue

        # WATCH is not protected. If it fell out of current focus, remove it.
        in_current_focus = sym in focus
        if not in_current_focus:
            continue

        signal, diagnostic = process_new_or_watch(row, metrics, quote, regime, phase)

        if signal:
            new_state[sym] = signal
        elif diagnostic:
            rejected_candidates.append(diagnostic)

    signals = build_signal_outputs(new_state)
    counts = summarize_signals(signals)

    rejected_candidates.sort(
        key=lambda x: (
            -safe_float(x.get("confidence"), 0),
            -safe_float(x.get("reward_risk"), 0),
            safe_str(x.get("symbol"), ""),
        )
    )

    signal_desk_payload = {
        "generated_at_et": iso_now_et(),
        "market_phase": phase,
        "alpaca_feed": DATA_FEED,
        "execution_timeframe": EXECUTION_TIMEFRAME,
        "structure_timeframe": STRUCTURE_TIMEFRAME,
        "universe": {
            "potential_limit": POTENTIAL_LIMIT,
            "active_limit": ACTIVE_LIMIT,
            "current_focus_count": len(focus),
            "monitor_count": len(monitor_symbols),
        },
        "counts": counts,
        "signals": signals,
        "rejected_candidates": rejected_candidates,
    }

    write_json(SIGNAL_DESK_FILE, signal_desk_payload)

    write_signal_state(
        new_state,
        {
            "market_phase": phase,
            "alpaca_feed": DATA_FEED,
            "execution_timeframe": EXECUTION_TIMEFRAME,
            "structure_timeframe": STRUCTURE_TIMEFRAME,
            "current_focus_count": len(focus),
            "monitor_count": len(monitor_symbols),
            "counts": counts,
            "rejected_count": len(rejected_candidates),
        },
    )

    log("Signal Desk output written:")
    log(f"  {SIGNAL_DESK_FILE}")
    log(f"  {SIGNAL_STATE_FILE}")
    log(f"Counts: {counts}")
    log(f"Rejected diagnostics: {len(rejected_candidates)}")

    if not signals:
        log("No signal at this moment. Scanner is monitoring top candidates.")


if __name__ == "__main__":
    run_signal_engine()
