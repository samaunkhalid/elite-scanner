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

Important:
  - This engine generates dashboard signals only.
  - It does NOT place orders.
  - Manual chart confirmation is still required before any trade.

Environment variables:
  ALPACA_API_KEY
  ALPACA_SECRET_KEY
  ALPACA_DATA_FEED       default: sip   allowed: sip, iex, delayed_sip
  SIGNAL_DEBUG           default: 0

Recommended workflow order:
  python elite_scanner.py
  python signal_engine.py
  python elite_dashboard.py
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta, timezone
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
ACTIVE_STALE_MINUTES = 10         # 2 GitHub refresh cycles at 5-min cadence.
TRIGGER_READY_STALE_MINUTES = 45  # Setup can stay ready longer, but not all day.
RECENT_INVALIDATED_KEEP_MINUTES = 30

# Quality thresholds.
MIN_AVG_DOLLAR_VOL_M = 25.0
MAX_VWAP_EXTENSION_PCT = 5.0
ACTIVE_HOD_MAX_DISTANCE = -2.5
POTENTIAL_HOD_MAX_DISTANCE = -4.0
HOD_BREAKOUT_READY_DISTANCE = -0.75
MIN_RR = 1.5
MIN_CONF_WATCH = 60.0
MIN_CONF_READY = 75.0
MIN_CONF_ACTIVE = 85.0

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


def parse_iso_dt(text: Any) -> Optional[datetime]:
    s = safe_str(text, "").strip()
    if not s:
        return None

    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass

    # Common fallback: "YYYY-MM-DD HH:MM:SS"
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


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


def normalize_status(status: Any) -> str:
    return safe_str(status, "WAIT").upper().replace(" ", "_")


def normalize_symbol(symbol: Any) -> str:
    return re.sub(r"[^A-Z0-9._-]", "", safe_str(symbol, "").upper().strip())


# ==============================================================
# TIME HELPERS
# ==============================================================

def ny_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo("America/New_York"))
    # Fallback approximation; GitHub Python 3.11 should have zoneinfo.
    return datetime.now()


def ny_datetime_for_today(hour: int, minute: int, now: Optional[datetime] = None) -> datetime:
    now = now or ny_now()

    if ZoneInfo:
        return datetime(
            now.year,
            now.month,
            now.day,
            hour,
            minute,
            tzinfo=ZoneInfo("America/New_York"),
        )

    return datetime(now.year, now.month, now.day, hour, minute)


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


def is_blackout_phase(phase: str) -> bool:
    return phase in {"OPENING_BLACKOUT", "LUNCH_BLACKOUT", "FINAL_BLACKOUT", "AFTERHOURS", "CLOSED"}


def session_date_str(now: Optional[datetime] = None) -> str:
    now = now or ny_now()
    return now.date().isoformat()


def session_start_end_utc(now: Optional[datetime] = None) -> Tuple[str, str]:
    """
    Regular-session window for signal logic.
    Uses 9:30 AM ET to now.
    This avoids mixing premarket into regular-session VWAP/HOD signals.
    """
    now = now or ny_now()

    if ZoneInfo:
        start_ny = datetime(now.year, now.month, now.day, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        end_ny = now
        start_utc = start_ny.astimezone(timezone.utc)
        end_utc = end_ny.astimezone(timezone.utc)
    else:
        # Approximate fallback.
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
        row["signal_source_bucket"] = "POTENTIAL"
        row["signal_rank"] = idx + 1
        focus[sym] = row

    for idx, row in enumerate(active):
        sym = normalize_symbol(row.get("symbol"))
        if not sym:
            continue
        if sym in focus:
            # Keep potential priority but note both buckets.
            focus[sym]["signal_source_bucket"] = "POTENTIAL+ACTIVE"
            continue
        row = dict(row)
        row["signal_source_bucket"] = "ACTIVE"
        row["signal_rank"] = idx + 1
        focus[sym] = row

    return focus


def load_signal_state() -> Dict[str, Dict[str, Any]]:
    data = load_json(SIGNAL_STATE_FILE, {})
    if isinstance(data, dict):
        # Newer format: {"signals": {...}}
        if isinstance(data.get("signals"), dict):
            return data.get("signals", {})
        # Older direct symbol map.
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

    def fetch_intraday_bars(self, symbols: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch today's regular-session 1-minute bars for signal logic.
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
                    "timeframe": "1Min",
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
        """
        Fetch latest bid/ask for spread-aware stop buffer.
        If unavailable, caller falls back to ATR/price buffer.
        """
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
    volume_stable_or_increasing: bool = False
    volume_drying: bool = False
    pullback_holding_vwap: bool = False
    pullback_high: float = 0.0
    pullback_low: float = 0.0
    recent_swing_low: float = 0.0
    opening_range_low: float = 0.0
    vwap_touch_count: int = 0
    consolidating_near_high: bool = False


def typical_price(bar: Dict[str, Any]) -> float:
    h = safe_float(bar.get("h"), 0)
    l = safe_float(bar.get("l"), 0)
    c = safe_float(bar.get("c"), 0)
    if h > 0 and l > 0 and c > 0:
        return (h + l + c) / 3.0
    return c


def analyze_bars(symbol: str, bars: List[Dict[str, Any]]) -> IntradayMetrics:
    metrics = IntradayMetrics(symbol=symbol)

    if not bars:
        return metrics

    clean = []
    for b in bars:
        c = safe_float(b.get("c"), 0)
        v = safe_float(b.get("v"), 0)
        if c > 0:
            clean.append(b)

    if not clean:
        return metrics

    metrics.has_data = True
    metrics.price = safe_float(clean[-1].get("c"), 0)
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

    recent = clean[-5:] if len(clean) >= 5 else clean
    prev = clean[-10:-5] if len(clean) >= 10 else clean[:-5]

    recent_vols = [safe_float(b.get("v"), 0) for b in recent]
    prev_vols = [safe_float(b.get("v"), 0) for b in prev]

    metrics.avg_volume_5 = sum(recent_vols) / len(recent_vols) if recent_vols else 0
    metrics.avg_volume_prev_5 = sum(prev_vols) / len(prev_vols) if prev_vols else 0
    metrics.volume_stable_or_increasing = (
        metrics.avg_volume_prev_5 <= 0 or metrics.avg_volume_5 >= 0.85 * metrics.avg_volume_prev_5
    )
    metrics.volume_drying = (
        metrics.avg_volume_prev_5 > 0 and metrics.avg_volume_5 <= 0.80 * metrics.avg_volume_prev_5
    )

    last3 = clean[-3:] if len(clean) >= 3 else clean
    metrics.pullback_high = max(safe_float(b.get("h"), 0) for b in last3)
    metrics.pullback_low = min(safe_float(b.get("l"), metrics.price) for b in last3 if safe_float(b.get("l"), 0) > 0)
    metrics.recent_swing_low = metrics.pullback_low

    # Opening range low: first 15 regular-session minutes available.
    first15 = clean[:15]
    if first15:
        lows = [safe_float(b.get("l"), 0) for b in first15 if safe_float(b.get("l"), 0) > 0]
        metrics.opening_range_low = min(lows) if lows else metrics.lod

    # VWAP hold: last 2 of 3 lows should be at or very close to VWAP.
    hold_count = 0
    for b in last3:
        low = safe_float(b.get("l"), 0)
        close = safe_float(b.get("c"), 0)
        if metrics.vwap > 0 and low >= metrics.vwap * 0.997 and close >= metrics.vwap:
            hold_count += 1
    metrics.pullback_holding_vwap = hold_count >= min(2, len(last3))

    # VWAP touch count: bars whose range touched VWAP or close was very near it.
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

    # Consolidating near high: last 5 lows remain close to HOD and range is not expanding wildly.
    if recent and metrics.hod > 0:
        recent_lows = [safe_float(b.get("l"), 0) for b in recent if safe_float(b.get("l"), 0) > 0]
        recent_highs = [safe_float(b.get("h"), 0) for b in recent if safe_float(b.get("h"), 0) > 0]

        if recent_lows and recent_highs:
            min_recent_low = min(recent_lows)
            recent_range_pct = pct_change(max(recent_highs), min_recent_low)
            metrics.consolidating_near_high = (
                pct_change(min_recent_low, metrics.hod) >= -1.5
                and recent_range_pct <= 2.0
            )

    return metrics


def quote_spread_dollars(quote: Dict[str, Any]) -> Optional[float]:
    if not quote:
        return None

    # Alpaca quote fields: bp = bid price, ap = ask price.
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


def watch_criteria_pass(row: Dict[str, Any], metrics: IntradayMetrics, regime: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons = []

    if high_risk_extreme(row):
        reasons.append("High risk/extreme category")
    if safe_float(row.get("dollar_vol_M"), 0) < MIN_AVG_DOLLAR_VOL_M:
        reasons.append("Dollar volume below $25M")
    if severe_risk_off(regime):
        reasons.append("Market severe risk-off")
    if sector_weak(row):
        reasons.append("Sector weak")
    if not vwap_condition_pass(metrics, safe_float(row.get("atr_pct"), 0)):
        reasons.append("VWAP condition failed")
    if not hod_condition_pass(metrics, safe_str(row.get("signal_source_bucket"), "")):
        reasons.append("Too far below HOD")
    if metrics.vwap_dist_pct > MAX_VWAP_EXTENSION_PCT:
        reasons.append("Too extended above VWAP")

    return len(reasons) == 0, reasons


def setup_vwap_pullback_ready(metrics: IntradayMetrics, rr: float, confidence: float) -> Tuple[bool, str]:
    if not metrics.has_data:
        return False, "No intraday bars"

    if not metrics.above_vwap:
        return False, "Below VWAP"

    if not metrics.pullback_holding_vwap:
        return False, "No 2-3 candle VWAP hold"

    if not metrics.volume_drying and not metrics.volume_stable_or_increasing:
        return False, "Pullback volume not constructive"

    if metrics.vwap_touch_count >= 4:
        return False, "4th+ VWAP touch"

    if metrics.hod_distance_pct < ACTIVE_HOD_MAX_DISTANCE:
        return False, "Too far from HOD"

    if rr < MIN_RR:
        return False, "R/R below 1.5"

    if confidence < MIN_CONF_READY:
        return False, "Confidence below 75"

    return True, "VWAP pullback holding; waiting for break above pullback high"


def setup_hod_breakout_ready(metrics: IntradayMetrics, rr: float, confidence: float) -> Tuple[bool, str]:
    if not metrics.has_data:
        return False, "No intraday bars"

    if not metrics.above_vwap:
        return False, "Below VWAP"

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

    if confidence < MIN_CONF_READY:
        return False, "Confidence below 75"

    return True, "HOD breakout setup; waiting for break above HOD"


def choose_setup(metrics: IntradayMetrics, plan: Dict[str, Any], confidence: float, phase: str) -> Tuple[str, str]:
    """
    Returns (setup_type, reason) for TRIGGER_READY candidate.
    Priority:
      1. VWAP Pullback
      2. HOD Breakout
    """
    rr = safe_float(plan.get("reward_risk"), 0)

    vwap_ready, vwap_reason = setup_vwap_pullback_ready(metrics, rr, confidence)
    if vwap_ready:
        return "VWAP_PULLBACK_CONTINUATION", vwap_reason

    # HOD breakout is not allowed during lunch. Since lunch is a blackout phase,
    # this function usually only runs in valid signal phases.
    hod_ready, hod_reason = setup_hod_breakout_ready(metrics, rr, confidence)
    if hod_ready:
        return "HOD_BREAKOUT_CONTINUATION", hod_reason

    return "", f"No trigger-ready setup. VWAP: {vwap_reason}; HOD: {hod_reason}"


def trigger_level_for_setup(setup_type: str, metrics: IntradayMetrics) -> float:
    if setup_type == "VWAP_PULLBACK_CONTINUATION":
        return metrics.pullback_high * 1.0002
    if setup_type == "HOD_BREAKOUT_CONTINUATION":
        return metrics.hod * 1.0002
    # Fresh post-blackout trigger fallback: recent local high.
    return max(metrics.pullback_high, metrics.hod) * 1.0002


def support_level_for_setup(setup_type: str, metrics: IntradayMetrics, entry: float) -> float:
    candidates = []

    if metrics.vwap > 0 and abs(pct_change(metrics.vwap, entry)) <= 2.0:
        candidates.append(metrics.vwap)

    if metrics.pullback_low > 0 and abs(pct_change(metrics.pullback_low, entry)) <= 2.5:
        candidates.append(metrics.pullback_low)

    if metrics.opening_range_low > 0 and abs(pct_change(metrics.opening_range_low, entry)) <= 3.0:
        candidates.append(metrics.opening_range_low)

    if metrics.recent_swing_low > 0 and abs(pct_change(metrics.recent_swing_low, entry)) <= 3.0:
        candidates.append(metrics.recent_swing_low)

    if candidates:
        return min(candidates)

    # Last-resort structure support.
    return metrics.vwap if metrics.vwap > 0 else entry * 0.985


def round_level_candidates_above(price: float) -> List[float]:
    if price <= 0:
        return []

    # Next whole and half-dollar levels above entry.
    next_half = math.ceil(price * 2) / 2.0
    next_whole = math.ceil(price)

    levels = []

    for level in [next_half, next_whole]:
        if level > price * 1.001:
            levels.append(level)

    # For lower-priced names, also include quarter increments.
    if price < 20:
        next_quarter = math.ceil(price * 4) / 4.0
        if next_quarter > price * 1.001:
            levels.append(next_quarter)

    return sorted(set(levels))


def vwap_extension_levels(vwap: float, entry: float) -> List[float]:
    if vwap <= 0:
        return []

    levels = [vwap * (1 + x / 100.0) for x in [0.5, 1.0, 1.5, 2.0]]
    return [x for x in levels if x > entry * 1.001]


def adjust_before_magnetic(level: float) -> float:
    if level <= 0:
        return level
    return level * 0.999  # ~0.1% before obvious level.


def build_trade_plan(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    setup_type: str,
    quote: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build entry, stop, targets, and R/R.
    The function stays conservative; if the stop or target is unrealistic,
    plan["valid"] becomes False.
    """
    atr_pct = safe_float(row.get("atr_pct"), 0)
    price = metrics.price or safe_float(row.get("price"), 0)

    entry = trigger_level_for_setup(setup_type, metrics)
    if entry <= 0 and price > 0:
        entry = price

    spread = quote_spread_dollars(quote or {})
    daily_atr_dollars = price * (atr_pct / 100.0) if price > 0 and atr_pct > 0 else 0.0

    buffer_options = [
        0.08 * daily_atr_dollars if daily_atr_dollars > 0 else 0,
        0.001 * price if price > 0 else 0,
    ]

    spread_used = False
    if spread is not None and spread > 0:
        buffer_options.append(2 * spread)
        spread_used = True

    buffer_dollars = max(buffer_options) if buffer_options else 0
    support = support_level_for_setup(setup_type, metrics, entry)
    stop = support - buffer_dollars

    risk = entry - stop
    stop_distance_pct = pct(risk, entry) if entry > 0 else 999.0

    valid = True
    rejection_reason = ""

    if entry <= 0 or stop <= 0 or risk <= 0:
        valid = False
        rejection_reason = "Invalid entry/stop"
    elif stop_distance_pct > 3.0 and not (atr_pct > 5.0 and stop_distance_pct <= 3.5):
        valid = False
        rejection_reason = "Stop distance too wide"
    elif atr_pct > 0 and stop_distance_pct > 0.75 * atr_pct:
        valid = False
        rejection_reason = "Stop distance exceeds 0.75x ATR"

    one_r = entry + risk
    two_r = entry + 2 * risk

    resistance_levels = []

    # Only include levels above entry.
    for level in [
        metrics.hod,
        safe_float(row.get("premarket_high"), 0),
        safe_float(row.get("prior_day_high"), 0),
        safe_float(row.get("resistance"), 0),
    ]:
        if level > entry * 1.001:
            resistance_levels.append(level)

    resistance_levels.extend(round_level_candidates_above(entry))
    resistance_levels.extend(vwap_extension_levels(metrics.vwap, entry))

    resistance_levels = sorted(set(round(x, 4) for x in resistance_levels if x > entry * 1.001))

    target_candidates = [one_r] + resistance_levels
    target_1_raw = min(target_candidates) if target_candidates else one_r

    # If target is one of the obvious levels, set just before it.
    if any(abs(target_1_raw - level) / level <= 0.003 for level in resistance_levels if level > 0):
        target_1 = adjust_before_magnetic(target_1_raw)
    else:
        target_1 = target_1_raw

    rr = (target_1 - entry) / risk if risk > 0 else 0

    if rr < MIN_RR:
        valid = False
        rejection_reason = "Target 1 R/R below 1.5"

    next_levels = [x for x in resistance_levels if x > target_1 * 1.001]
    target_2_candidates = [two_r] + next_levels
    target_2 = min(target_2_candidates) if target_2_candidates else two_r

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
        "buffer_dollars": round(buffer_dollars, 4),
        "spread_used": spread_used,
        "spread_dollars": round(spread, 4) if spread is not None else None,
        "daily_atr_dollars": round(daily_atr_dollars, 4),
    }


def live_signal_score(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    plan: Dict[str, Any],
    regime: Dict[str, Any],
    phase: str,
) -> float:
    """
    Live score normalized to 100.
    """
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

    # 2. HOD/range position: 15
    if metrics.hod_distance_pct >= -0.75:
        hod_score = 15
    elif metrics.hod_distance_pct >= -2.5:
        hod_score = 10
    elif metrics.hod_distance_pct >= -4.0:
        hod_score = 6
    else:
        hod_score = 0

    # 3. Volume confirmation: 15
    if metrics.volume_stable_or_increasing:
        volume_score = 15
    elif metrics.volume_drying and metrics.pullback_holding_vwap:
        volume_score = 12
    elif metrics.avg_volume_5 > 0:
        volume_score = 6
    else:
        volume_score = 0

    # 4. Risk/reward: 15
    rr = safe_float(plan.get("reward_risk"), 0)
    if rr >= 2.5:
        rr_score = 15
    elif rr >= 2.0:
        rr_score = 12
    elif rr >= 1.5:
        rr_score = 8
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

    market_score = 0 if severe_risk_off(regime) else 5

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
        penalty += 4

    if metrics.hod_distance_pct < -4.0:
        penalty += 3

    score = (
        vwap_score
        + hod_score
        + volume_score
        + rr_score
        + sector_score
        + market_score
        + time_score
        - min(10, penalty)
    )

    return round(clamp(score, 0, 100), 2)


def final_confidence(scanner_score: float, live_score: float) -> float:
    return round(clamp(0.40 * scanner_score + 0.60 * live_score, 0, 100), 1)


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
        "price": round(metrics.price, 4),
        "vwap": round(metrics.vwap, 4),
        "hod": round(metrics.hod, 4),
        "vwap_dist_pct": round(metrics.vwap_dist_pct, 2),
        "hod_distance_pct": round(metrics.hod_distance_pct, 2),
        "above_vwap": metrics.above_vwap,
        "sector_status": safe_str(row.get("sector_status"), ""),
        "market_phase": phase,
        "reason": reason,
        "invalidation": "Lose VWAP, lose trigger, break pullback low, stop would hit, or signal becomes stale.",
        "last_checked": now_text,
        "updated_at": now_text,
        "session_date": session_date_str(),
        "actionable": status == "ACTIVE_SIGNAL",
        "actionability": "ACTIVE" if status in {"TRIGGER_READY", "ACTIVE_SIGNAL"} else "WATCH",
        "suppression_reason": "",
        "risk_flags": safe_str(row.get("risk_flags"), ""),
        "company_name": safe_str(row.get("company_name"), ""),
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
    return metrics.price >= trigger and metrics.above_vwap


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

    pullback_low = safe_float(signal.get("pullback_low"), 0)
    trigger = safe_float(signal.get("entry_trigger"), 0)

    if not metrics.above_vwap:
        return True, "Lost VWAP before trigger", "FAILED_SETUP"

    if pullback_low > 0 and metrics.price < pullback_low:
        return True, "Broke pullback low before trigger", "FAILED_SETUP"

    if expired_by_age(signal, TRIGGER_READY_STALE_MINUTES):
        return True, "Trigger-ready setup became stale", "MISSED_WINDOW"

    # If price moved too far above trigger without becoming active, do not chase.
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
    """
    TRIGGER_READY fired during blackout:
      - Do not promote to ACTIVE_SIGNAL.
      - Stay TRIGGER_READY but suppressed.
      - Log to suppressed_signals.csv.
      - Require a fresh post-blackout trigger later.
    """
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


def reassess_after_blackout(
    signal: Dict[str, Any],
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    phase: str,
) -> Optional[Dict[str, Any]]:
    """
    If a trigger fired during blackout, reassess after valid window resumes.
    Return None if no special handling needed.
    """
    if not signal.get("requires_fresh_trigger"):
        return None

    blackout_price = safe_float(signal.get("blackout_trigger_price"), 0)
    if blackout_price <= 0 or not metrics.has_data:
        return None

    extension = pct_change(metrics.price, blackout_price)

    if extension > 2.0:
        return make_invalidated(
            signal,
            row,
            metrics,
            "Triggered during blackout and is now extended. No chase.",
            "MISSED_WINDOW",
            phase,
        )

    if metrics.price < blackout_price or not metrics.above_vwap:
        return make_invalidated(
            signal,
            row,
            metrics,
            "Lost trigger/VWAP hold after blackout trigger.",
            "FAILED_SETUP",
            phase,
        )

    # Still clean: remain TRIGGER_READY, but require a fresh trigger from current structure.
    out = dict(signal)
    fresh_trigger = max(metrics.pullback_high, metrics.hod) * 1.0002 if metrics.has_data else safe_float(signal.get("entry_trigger"), 0)
    now_text = iso_now_et()

    out.update({
        "signal_status": "TRIGGER_READY",
        "actionability": "ACTIVE",
        "actionable": False,
        "entry_trigger": round(fresh_trigger, 4),
        "suppression_reason": "",
        "blackout_trigger_price": "",
        "blackout_trigger_time": "",
        "requires_fresh_trigger": True,
        "reason": "Setup still valid after blackout. Awaiting fresh post-blackout trigger.",
        "last_checked": now_text,
        "updated_at": now_text,
        "ready_since": now_text,
        "market_phase": phase,
        "price": round(metrics.price, 4),
        "vwap": round(metrics.vwap, 4),
        "hod": round(metrics.hod, 4),
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
            "actionable": True,
            "actionability": "ACTIVE",
        })
        return out

    if status == "TRIGGER_READY":
        # If trigger fired during blackout, suppress it.
        if trigger_fired(existing, metrics) and not is_valid_signal_phase(phase):
            return suppress_trigger_during_blackout(existing, row, metrics, phase, regime)

        # After a suppressed blackout trigger, reassess once valid phase resumes.
        if is_valid_signal_phase(phase) and existing.get("requires_fresh_trigger"):
            reassessed = reassess_after_blackout(existing, row, metrics, phase)
            if reassessed:
                return reassessed

        invalid, reason, category = ready_invalidated(existing, metrics)
        if invalid:
            return make_invalidated(existing, row, metrics, reason, category, phase)

        if trigger_fired(existing, metrics) and is_valid_signal_phase(phase):
            # Rebuild confidence using the current plan/metrics.
            setup_type = safe_str(existing.get("setup_type"), "")
            plan = build_trade_plan(row, metrics, setup_type, quote)
            live = live_signal_score(row, metrics, plan, regime, phase)
            conf = final_confidence(safe_float(row.get("score"), safe_float(existing.get("scanner_score"), 0)), live)

            if plan.get("valid") and conf >= MIN_CONF_ACTIVE and safe_float(plan.get("reward_risk"), 0) >= MIN_RR:
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
                )
                out["triggered_at"] = now_text
                out["detected_at"] = existing.get("detected_at") or existing.get("ready_since") or now_text
                out["ready_since"] = existing.get("ready_since") or now_text
                out["pullback_low"] = existing.get("pullback_low", metrics.pullback_low)
                return out

            return make_invalidated(
                existing,
                row,
                metrics,
                plan.get("rejection_reason") or "Trigger fired but active-signal quality failed.",
                "FAILED_SETUP",
                phase,
            )

        # Still ready; refresh live fields.
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
            "actionable": False,
            "actionability": "ACTIVE" if not existing.get("suppression_reason") else "SUPPRESSED",
        })
        return out

    # Invalidated can remain briefly for dashboard context.
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
) -> Dict[str, Any]:
    symbol = normalize_symbol(row.get("symbol"))

    if not metrics.has_data:
        # No Alpaca bars: no signal.
        return {}

    watch_ok, watch_reasons = watch_criteria_pass(row, metrics, regime)
    if not watch_ok:
        debug(f"  {symbol}: WATCH fail: {watch_reasons}")
        return {}

    # Use a provisional setup type to compute a conservative plan.
    provisional_setup = "VWAP_PULLBACK_CONTINUATION" if metrics.pullback_holding_vwap else "HOD_BREAKOUT_CONTINUATION"
    plan = build_trade_plan(row, metrics, provisional_setup, quote)
    live = live_signal_score(row, metrics, plan, regime, phase)
    conf = final_confidence(safe_float(row.get("score"), 0), live)

    if phase == "PREMARKET":
        if conf >= MIN_CONF_WATCH:
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
            )
            out["detected_at"] = iso_now_et()
            return out
        return {}

    # Blackout or closed: do not create new TRIGGER_READY or ACTIVE_SIGNAL.
    # WATCH can exist during opening/final/lunch only if market is open, but no urgency.
    if not is_valid_signal_phase(phase):
        if is_market_open_phase(phase) and conf >= MIN_CONF_WATCH:
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
            )
            out["detected_at"] = iso_now_et()
            return out
        return {}

    # Valid signal phase: decide whether it is trigger-ready.
    if plan.get("valid") and conf >= MIN_CONF_READY:
        setup_type, setup_reason = choose_setup(metrics, plan, conf, phase)

        if setup_type:
            plan = build_trade_plan(row, metrics, setup_type, quote)
            live = live_signal_score(row, metrics, plan, regime, phase)
            conf = final_confidence(safe_float(row.get("score"), 0), live)

            if plan.get("valid") and conf >= MIN_CONF_READY:
                out = signal_base(
                    row,
                    metrics,
                    plan,
                    "TRIGGER_READY",
                    setup_type,
                    conf,
                    live,
                    setup_reason,
                    phase,
                )
                now_text = iso_now_et()
                out["ready_since"] = now_text
                out["detected_at"] = now_text
                out["pullback_low"] = round(metrics.pullback_low, 4)
                return out

    # Otherwise WATCH only.
    if conf >= MIN_CONF_WATCH:
        out = signal_base(
            row,
            metrics,
            plan,
            "WATCH",
            "MONITORING",
            conf,
            live,
            "Clean candidate. Waiting for VWAP pullback or HOD breakout structure.",
            phase,
        )
        out["detected_at"] = iso_now_et()
        return out

    return {}


# ==============================================================
# MAIN ENGINE
# ==============================================================

def build_row_lookup(focus: Dict[str, Dict[str, Any]], prior_state: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Protected TRIGGER_READY / ACTIVE_SIGNAL names might drop from Top 20.
    If no current row exists, use stored minimal fields from prior state.
    """
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
            "risk_category": "NORMAL",
            "dollar_vol_M": MIN_AVG_DOLLAR_VOL_M,
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
    """
    Dashboard-friendly list.
    Order: ACTIVE, TRIGGER_READY, WATCH, INVALIDATED.
    """
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


def run_signal_engine() -> None:
    now = ny_now()
    phase = get_market_phase(now)

    log("============================================================")
    log("Signal Desk v1 Engine")
    log("============================================================")
    log(f"Time: {now.isoformat(timespec='seconds')}")
    log(f"Market phase: {phase}")
    log(f"Alpaca feed: {DATA_FEED}")

    focus = load_focus_candidates()
    prior_state = load_signal_state()
    regime = load_json(MARKET_REGIME_FILE, {})

    monitor_symbols = prepare_monitor_symbols(focus, prior_state)
    row_lookup = build_row_lookup(focus, prior_state)

    log(f"Focus universe: {len(focus)} current scanner names")
    log(f"Monitor universe: {len(monitor_symbols)} including protected signals")

    market_data = AlpacaMarketData(DATA_FEED)

    bars_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    quotes_by_symbol: Dict[str, Dict[str, Any]] = {}

    if market_data.available and monitor_symbols:
        # Fetch bars during market phases. In premarket/closed, this may return empty for regular session.
        bars_by_symbol = market_data.fetch_intraday_bars(monitor_symbols)
        quotes_by_symbol = market_data.fetch_latest_quotes(monitor_symbols)
    elif not market_data.available:
        log("  ⚠ No Alpaca credentials. signal_desk.json will be empty or historical only.")

    new_state: Dict[str, Dict[str, Any]] = {}

    for sym in monitor_symbols:
        row = row_lookup.get(sym, {"symbol": sym})
        metrics = analyze_bars(sym, bars_by_symbol.get(sym, []))
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
        in_current_top20 = sym in focus
        if not in_current_top20:
            continue

        processed = process_new_or_watch(row, metrics, quote, regime, phase)
        if processed:
            new_state[sym] = processed

    signals = build_signal_outputs(new_state)
    counts = summarize_signals(signals)

    signal_desk_payload = {
        "generated_at_et": iso_now_et(),
        "market_phase": phase,
        "alpaca_feed": DATA_FEED,
        "universe": {
            "potential_limit": POTENTIAL_LIMIT,
            "active_limit": ACTIVE_LIMIT,
            "current_focus_count": len(focus),
            "monitor_count": len(monitor_symbols),
        },
        "counts": counts,
        "signals": signals,
    }

    write_json(SIGNAL_DESK_FILE, signal_desk_payload)

    write_signal_state(
        new_state,
        {
            "market_phase": phase,
            "alpaca_feed": DATA_FEED,
            "current_focus_count": len(focus),
            "monitor_count": len(monitor_symbols),
            "counts": counts,
        },
    )

    log("Signal Desk output written:")
    log(f"  {SIGNAL_DESK_FILE}")
    log(f"  {SIGNAL_STATE_FILE}")
    log(f"Counts: {counts}")

    if not signals:
        log("No signal at this moment. Scanner is monitoring top candidates.")


if __name__ == "__main__":
    run_signal_engine()
