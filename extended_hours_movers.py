#!/usr/bin/env python3
"""
extended_hours_movers.py
------------------------

Separate monitor-only scanner for pre-market and after-hours movers.

Purpose:
  - Find true extended-hours movers using Alpaca SIP data.
  - Write premarket_movers.csv/json or after_hours_movers.csv/json.
  - Keep extended-hours discovery completely separate from the regular scanner,
    Smart Money Phase 1 validation, and Signal Desk lifecycle.

This script intentionally does NOT:
  - run signal_engine.py
  - create WATCH / TRIGGER_READY / ACTIVE_SIGNAL
  - write signal_state.json or signal_outcomes.csv
  - modify regular scanner candidate files
  - use elite_scanner.py filters

Default filters are built for high-volume extended-hours breakout/continuation monitoring:
  - price range
  - listed exchange
  - fresh current extended-hours price
  - minimum current extended-hours % move versus regular close
  - minimum total and recent extended-hours volume/notional
  - minimum extended-hours bar count
  - price-adjusted spread control
  - common-stock cleanup to remove ETFs, warrants, units, rights, funds and similar products
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


VERSION = "extended_hours_movers_v1.2_current_breakout_continuation"

PROJECT_DIR = Path(os.getenv("ELITE_PROJECT_DIR", Path(__file__).resolve().parent)).resolve()

DATA_BASE_URL = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
TRADING_BASE_URL = os.getenv("ALPACA_TRADING_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
DATA_FEED = os.getenv("ALPACA_DATA_FEED", "sip").strip().lower() or "sip"

ASSET_CACHE_FILE = PROJECT_DIR / os.getenv("EXTENDED_ASSET_CACHE_FILE", "extended_hours_universe.csv")
ASSET_CACHE_MAX_AGE_SECONDS = int(os.getenv("EXTENDED_ASSET_CACHE_MAX_AGE_SECONDS", str(12 * 60 * 60)))

MIN_PRICE = float(os.getenv("EXTENDED_MIN_PRICE", "1.00"))
MAX_PRICE = float(os.getenv("EXTENDED_MAX_PRICE", "200.00"))

PREMARKET_MIN_MOVE_PCT = float(os.getenv("PREMARKET_MIN_MOVE_PCT", "2.0"))
AFTER_HOURS_MIN_MOVE_PCT = float(os.getenv("AFTER_HOURS_MIN_MOVE_PCT", "2.0"))

# Extended-hours scanner goal:
#   high-volume breakouts and continuations that are still active now.
# It should NOT rank stale spike highs, single-print movers, or wide-spread names.
MIN_EXTENDED_VOLUME = int(os.getenv("EXTENDED_MIN_VOLUME", "50000"))
MIN_EXTENDED_NOTIONAL = float(os.getenv("EXTENDED_MIN_NOTIONAL", "500000"))
ABSOLUTE_MIN_EXTENDED_VOLUME = int(os.getenv("EXTENDED_ABSOLUTE_MIN_VOLUME", "1000"))
MIN_EXTENDED_BARS = int(os.getenv("EXTENDED_MIN_BARS", "3"))

PREMARKET_MAX_STALE_MINUTES = float(os.getenv("PREMARKET_MAX_STALE_MINUTES", "10"))
AFTER_HOURS_MAX_STALE_MINUTES = float(os.getenv("AFTER_HOURS_MAX_STALE_MINUTES", "15"))

RECENT_10_MIN_VOLUME = int(os.getenv("EXTENDED_RECENT_10_MIN_VOLUME", "3000"))
RECENT_20_MIN_VOLUME = int(os.getenv("EXTENDED_RECENT_20_MIN_VOLUME", "8000"))
RECENT_20_MIN_NOTIONAL = float(os.getenv("EXTENDED_RECENT_20_MIN_NOTIONAL", "50000"))

# Price-adjusted spread tolerance. Low-priced stocks can have wider percentage
# spreads after-hours; higher-priced stocks must be tighter.
MAX_SPREAD_UNDER_5 = float(os.getenv("EXTENDED_MAX_SPREAD_UNDER_5", "12.0"))
MAX_SPREAD_UNDER_10 = float(os.getenv("EXTENDED_MAX_SPREAD_UNDER_10", "10.0"))
MAX_SPREAD_10_PLUS = float(os.getenv("EXTENDED_MAX_SPREAD_10_PLUS", "8.0"))

EXCLUDE_NON_COMMON_STOCKS = os.getenv("EXTENDED_EXCLUDE_NON_COMMON", "1").strip().lower() in {"1", "true", "yes", "y"}

MAX_OUTPUT_ROWS = int(os.getenv("EXTENDED_MAX_OUTPUT_ROWS", "30"))
SNAPSHOT_CHUNK_SIZE = int(os.getenv("EXTENDED_SNAPSHOT_CHUNK_SIZE", "200"))
BAR_CHUNK_SIZE = int(os.getenv("EXTENDED_BAR_CHUNK_SIZE", "100"))

REQUEST_TIMEOUT = int(os.getenv("EXTENDED_REQUEST_TIMEOUT", "25"))

VALID_EXCHANGES = {
    "NASDAQ",
    "NYSE",
    "AMEX",
    "NYSEARCA",
    "NYSEAMERICAN",
    "ARCA",
    "BATS",
}


@dataclass
class Asset:
    symbol: str
    name: str = ""
    exchange: str = ""


def et_tz():
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo is unavailable. Use Python 3.9+.")
    return ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(et_tz())


def iso_et(dt: Optional[datetime] = None) -> str:
    dt = dt or now_et()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=et_tz())
    return dt.astimezone(et_tz()).isoformat(timespec="seconds")


def to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=et_tz())
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ts_to_et(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(et_tz())
    except Exception:
        return None


def previous_weekday(dt: datetime) -> datetime:
    out = dt - timedelta(days=1)
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


def session_date_for(session: str, current: Optional[datetime] = None) -> datetime:
    current = current or now_et()
    session = session.lower()

    if session == "afterhours":
        # If manually run before 16:00, inspect the previous market day's
        # after-hours window instead of today's not-yet-started window.
        if current.time() < datetime.strptime("16:00", "%H:%M").time():
            return previous_weekday(current)
        return current

    # Premarket belongs to the current trading date.
    return current


def session_window(session: str, current: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    current = current or now_et()
    d = session_date_for(session, current)

    if session.lower() == "premarket":
        return (
            d.replace(hour=4, minute=0, second=0, microsecond=0),
            d.replace(hour=9, minute=30, second=0, microsecond=0),
        )

    return (
        d.replace(hour=16, minute=0, second=0, microsecond=0),
        d.replace(hour=20, minute=0, second=0, microsecond=0),
    )


def in_session_window(ts: Optional[datetime], session: str, current: Optional[datetime] = None) -> bool:
    if ts is None:
        return False
    start, end = session_window(session, current)
    return start <= ts <= end


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
        return int(float(value))
    except Exception:
        return default


def clean_symbol(value: Any) -> str:
    sym = str(value or "").strip().upper()
    if not sym:
        return ""

    # Keep common U.S. equity symbols/classes. Exclude warrants, preferred,
    # units, futures, crypto and odd web screener symbols.
    if any(ch in sym for ch in ["^", "=", "/", "+", "-"]):
        return ""

    if not re.fullmatch(r"[A-Z0-9.]{1,12}", sym):
        return ""

    return sym




NON_COMMON_NAME_KEYWORDS = (
    "warrant",
    "warrants",
    "right",
    "rights",
    "unit",
    "units",
    "preferred",
    "preference",
    "depositary share",
    "depositary shares",
    "notes due",
    "senior notes",
    "subordinated notes",
    "bond",
    "bonds",
    "debenture",
    "debentures",
    "etf",
    "etfs",
    "etn",
    "etns",
    "exchange traded",
    "exchange-traded",
    "closed-end fund",
    "closed end fund",
    "fund",
    "funds",
    "trust",
    "leveraged",
    "leverage shares",
    "inverse",
    "2x",
    "3x",
    "ultra",
    "daily bull",
    "daily bear",
)


def looks_like_non_common_symbol(symbol: str) -> bool:
    """
    Conservative symbol-level cleanup for extended-hours monitor mode.

    This rejects common warrant/right/unit suffix patterns without rejecting
    normal short common-stock tickers like W, NOW, LAW, HROW, etc.
    """
    s = clean_symbol(symbol)
    if not s:
        return True

    compact = s.replace(".", "")

    # Common warrant/right/unit suffixes after a base ticker.
    if compact.endswith(("WW", "WS", "WT", "WTS", "WSA", "WSB")) and len(compact) >= 4:
        return True

    # Many warrants/rights/units are ABCDW / ABCDR / ABCDU.
    # Use length >= 5 so normal common tickers ending W/R/U are not removed.
    if len(compact) >= 5 and compact[-1] in {"W", "R", "U"}:
        return True

    return False


def looks_like_non_common_name(name: str) -> bool:
    text = str(name or "").lower()
    if not text:
        return False

    # Avoid substring false positives such as "Bright" matching "right".
    for keyword in NON_COMMON_NAME_KEYWORDS:
        if re.search(r"\b" + re.escape(keyword) + r"\b", text):
            return True
    return False


def is_allowed_extended_asset(symbol: str, name: str) -> bool:
    if not EXCLUDE_NON_COMMON_STOCKS:
        return True
    if looks_like_non_common_symbol(symbol):
        return False
    if looks_like_non_common_name(name):
        return False
    return True


def headers() -> Dict[str, str]:
    key = (
        os.getenv("ALPACA_API_KEY")
        or os.getenv("ALPACA_KEY")
        or os.getenv("APCA_API_KEY_ID")
        or ""
    ).strip()

    secret = (
        os.getenv("ALPACA_SECRET_KEY")
        or os.getenv("ALPACA_SECRET")
        or os.getenv("APCA_API_SECRET_KEY")
        or ""
    ).strip()

    if not key or not secret:
        raise RuntimeError("Missing Alpaca credentials. Set ALPACA_API_KEY/ALPACA_SECRET_KEY or APCA_API_KEY_ID/APCA_API_SECRET_KEY.")

    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = REQUEST_TIMEOUT) -> Any:
    r = requests.get(url, headers=headers(), params=params or {}, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"Alpaca API error {r.status_code}: {r.text[:500]}")
    return r.json() if r.content else {}


def chunks(items: Sequence[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield list(items[i:i + size])


def cache_is_fresh(path: Path, max_age_seconds: int) -> bool:
    if not path.exists():
        return False
    try:
        return (time.time() - path.stat().st_mtime) <= max_age_seconds
    except Exception:
        return False


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if rows:
        fields: List[str] = []
        seen = set()
        for row in rows:
            for key in row.keys():
                if key not in seen:
                    seen.add(key)
                    fields.append(key)
    else:
        fields = [
            "symbol",
            "company_name",
            "exchange",
            "price",
            "change_pct",
            "score",
            "setup_bucket",
            "risk_category",
            "tags",
        ]

    tmp = tempfile.NamedTemporaryFile("w", delete=False, newline="", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with tmp:
            writer = csv.DictWriter(tmp, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp.name, path)
    finally:
        if os.path.exists(tmp.name):
            try:
                os.remove(tmp.name)
            except Exception:
                pass


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except Exception:
                pass


def read_asset_cache(path: Path) -> List[Asset]:
    assets: List[Asset] = []
    seen = set()
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                sym = clean_symbol(row.get("symbol"))
                if not sym or sym in seen:
                    continue

                name = str(row.get("name") or sym)
                exchange = str(row.get("exchange") or "").strip().upper()

                # Re-apply the current common-stock and exchange rules to cached
                # assets. This prevents old cache files from keeping ETFs,
                # warrants, units, or other non-common products after filters are
                # tightened.
                if not is_allowed_extended_asset(sym, name):
                    continue
                if exchange and exchange not in VALID_EXCHANGES:
                    continue

                seen.add(sym)
                assets.append(Asset(sym, name, exchange))
    except Exception:
        return []
    return assets


def write_asset_cache(path: Path, assets: List[Asset]) -> None:
    rows = [{"symbol": a.symbol, "name": a.name, "exchange": a.exchange} for a in assets]
    write_csv(path, rows)


def fetch_assets() -> List[Asset]:
    if cache_is_fresh(ASSET_CACHE_FILE, ASSET_CACHE_MAX_AGE_SECONDS):
        cached = read_asset_cache(ASSET_CACHE_FILE)
        if cached:
            print(f"Loaded {len(cached)} cached active assets from {ASSET_CACHE_FILE.name}")
            return cached

    url = f"{TRADING_BASE_URL}/v2/assets"
    params = {
        "status": "active",
        "asset_class": "us_equity",
    }

    data = get_json(url, params=params)
    if not isinstance(data, list):
        raise RuntimeError("Unexpected Alpaca assets response")

    assets: List[Asset] = []
    seen = set()

    for item in data:
        if not isinstance(item, dict):
            continue

        symbol = clean_symbol(item.get("symbol"))
        if not symbol or symbol in seen:
            continue

        name = str(item.get("name") or symbol)

        if not is_allowed_extended_asset(symbol, name):
            continue

        exchange = str(item.get("exchange") or "").strip().upper()
        if exchange and exchange not in VALID_EXCHANGES:
            continue

        # Keep active U.S. common equities / ADR-like listings for monitor mode.
        # Tradability can be false for some listings, but extended-hours mover
        # monitoring can still display them if data exists.
        seen.add(symbol)
        assets.append(Asset(symbol, name, exchange))

    assets.sort(key=lambda x: x.symbol)
    write_asset_cache(ASSET_CACHE_FILE, assets)
    print(f"Fetched and cached {len(assets)} active assets")
    return assets


def fetch_snapshots(symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}

    for group in chunks(list(symbols), SNAPSHOT_CHUNK_SIZE):
        try:
            data = get_json(
                f"{DATA_BASE_URL}/v2/stocks/snapshots",
                params={
                    "symbols": ",".join(group),
                    "feed": DATA_FEED,
                },
            )
        except Exception as exc:
            print(f"  ⚠ Snapshot chunk failed ({len(group)} symbols): {exc}")
            continue

        snapshots = data.get("snapshots") if isinstance(data, dict) else {}
        if isinstance(snapshots, dict):
            for sym, snap in snapshots.items():
                if isinstance(snap, dict):
                    out[str(sym).upper()] = snap
            continue

        # Defensive fallback if API returns direct symbol mapping.
        if isinstance(data, dict):
            for sym, snap in data.items():
                if isinstance(snap, dict):
                    out[str(sym).upper()] = snap

    return out


def bar_close(bar: Dict[str, Any]) -> float:
    return safe_float(bar.get("c") or bar.get("close"), 0.0)


def fetch_regular_close_anchors(symbols: Sequence[str], session: str, current: Optional[datetime] = None) -> Dict[str, float]:
    if session.lower() != "afterhours":
        return {}

    d = session_date_for("afterhours", current)
    start_et = d.replace(hour=15, minute=45, second=0, microsecond=0)
    end_et = d.replace(hour=16, minute=5, second=0, microsecond=0)
    close_et = d.replace(hour=16, minute=0, second=0, microsecond=0)

    anchors: Dict[str, float] = {}

    for group in chunks(list(symbols), BAR_CHUNK_SIZE):
        try:
            data = get_json(
                f"{DATA_BASE_URL}/v2/stocks/bars",
                params={
                    "symbols": ",".join(group),
                    "timeframe": "1Min",
                    "start": to_utc_iso(start_et),
                    "end": to_utc_iso(end_et),
                    "adjustment": "raw",
                    "feed": DATA_FEED,
                    "limit": 10000,
                    "sort": "asc",
                },
            )
        except Exception as exc:
            print(f"  ⚠ Close-anchor chunk failed ({len(group)} symbols): {exc}")
            continue

        bars_by_symbol = data.get("bars", {}) if isinstance(data, dict) else {}
        if not isinstance(bars_by_symbol, dict):
            continue

        for sym in group:
            selected = 0.0
            rows = bars_by_symbol.get(sym, []) or []
            for bar in rows:
                if not isinstance(bar, dict):
                    continue
                ts = parse_ts_to_et(bar.get("t"))
                if not ts or ts >= close_et:
                    continue
                c = bar_close(bar)
                if c > 0:
                    selected = c
            if selected > 0:
                anchors[sym] = selected

    return anchors


def fetch_extended_bar_stats(symbols: Sequence[str], session: str, current: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    start_et, end_et = session_window(session, current)
    now = current or now_et()
    if start_et <= now <= end_et:
        end_et = now

    recent_10_start = now - timedelta(minutes=10)
    recent_20_start = now - timedelta(minutes=20)

    stats: Dict[str, Dict[str, Any]] = {}

    for group in chunks(list(symbols), BAR_CHUNK_SIZE):
        try:
            data = get_json(
                f"{DATA_BASE_URL}/v2/stocks/bars",
                params={
                    "symbols": ",".join(group),
                    "timeframe": "1Min",
                    "start": to_utc_iso(start_et),
                    "end": to_utc_iso(end_et),
                    "adjustment": "raw",
                    "feed": DATA_FEED,
                    "limit": 10000,
                    "sort": "asc",
                },
            )
        except Exception as exc:
            print(f"  ⚠ Extended-bar chunk failed ({len(group)} symbols): {exc}")
            continue

        bars_by_symbol = data.get("bars", {}) if isinstance(data, dict) else {}
        if not isinstance(bars_by_symbol, dict):
            continue

        for sym in group:
            total_vol = 0
            total_notional = 0.0
            recent_10_vol = 0
            recent_10_notional = 0.0
            recent_20_vol = 0
            recent_20_notional = 0.0
            high = 0.0
            low = 0.0
            latest_close = 0.0
            latest_bar_time: Optional[datetime] = None
            bars_seen = 0

            for bar in bars_by_symbol.get(sym, []) or []:
                if not isinstance(bar, dict):
                    continue
                ts = parse_ts_to_et(bar.get("t"))
                if not in_session_window(ts, session, current):
                    continue

                c = bar_close(bar)
                h = safe_float(bar.get("h") or bar.get("high"), c)
                l = safe_float(bar.get("l") or bar.get("low"), c)
                v = safe_int(bar.get("v") or bar.get("volume"), 0)
                vw = safe_float(bar.get("vw") or bar.get("vwap"), c)
                px_for_notional = vw if vw > 0 else c
                notional = max(0, v) * px_for_notional

                if c > 0 and (latest_bar_time is None or ts is not None and ts >= latest_bar_time):
                    latest_close = c
                    latest_bar_time = ts

                if h > 0:
                    high = max(high, h)
                if l > 0:
                    low = l if low <= 0 else min(low, l)

                total_vol += max(0, v)
                total_notional += notional
                bars_seen += 1

                if ts is not None and ts >= recent_10_start:
                    recent_10_vol += max(0, v)
                    recent_10_notional += notional
                if ts is not None and ts >= recent_20_start:
                    recent_20_vol += max(0, v)
                    recent_20_notional += notional

            if bars_seen:
                stats[sym] = {
                    "extended_volume": float(total_vol),
                    "extended_notional": float(total_notional),
                    "extended_high": high,
                    "extended_low": low,
                    "extended_latest_bar_close": latest_close,
                    "extended_latest_bar_time": latest_bar_time.isoformat(timespec="seconds") if latest_bar_time else "",
                    "extended_bar_count": float(bars_seen),
                    "recent_10_min_volume": float(recent_10_vol),
                    "recent_10_min_notional": float(recent_10_notional),
                    "recent_20_min_volume": float(recent_20_vol),
                    "recent_20_min_notional": float(recent_20_notional),
                }

    return stats

def snapshot_price_candidates(snapshot: Dict[str, Any], session: str, current: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    Return extended-hours price candidates from the Alpaca snapshot.

    These are used for broad recall and as fallback if the explicit 1-minute
    extended bar request has no fresh bar. Final ranking still recalculates the
    move from the freshest valid current price.
    """
    out: List[Dict[str, Any]] = []

    minute = snapshot.get("minuteBar") or snapshot.get("minute_bar") or {}
    if isinstance(minute, dict):
        price = safe_float(minute.get("c") or minute.get("close"), 0.0)
        ts = parse_ts_to_et(minute.get("t") or minute.get("timestamp"))
        vol = safe_int(minute.get("v") or minute.get("volume"), 0)
        if price > 0 and in_session_window(ts, session, current):
            out.append({
                "price": price,
                "source": "snapshot_minute_bar",
                "timestamp": ts,
                "size": vol,
                "bid": 0.0,
                "ask": 0.0,
            })

    trade = snapshot.get("latestTrade") or snapshot.get("latest_trade") or {}
    if isinstance(trade, dict):
        price = safe_float(trade.get("p") or trade.get("price"), 0.0)
        ts = parse_ts_to_et(trade.get("t") or trade.get("timestamp"))
        size = safe_int(trade.get("s") or trade.get("size"), 0)
        if price > 0 and in_session_window(ts, session, current):
            out.append({
                "price": price,
                "source": "latest_trade",
                "timestamp": ts,
                "size": size,
                "bid": 0.0,
                "ask": 0.0,
            })

    quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
    if isinstance(quote, dict):
        bid = safe_float(quote.get("bp") or quote.get("bid_price"), 0.0)
        ask = safe_float(quote.get("ap") or quote.get("ask_price"), 0.0)
        ts = parse_ts_to_et(quote.get("t") or quote.get("timestamp"))
        if bid > 0 and ask > 0 and ask >= bid and in_session_window(ts, session, current):
            mid = (bid + ask) / 2.0
            out.append({
                "price": mid,
                "source": "quote_mid",
                "timestamp": ts,
                "size": 0,
                "bid": bid,
                "ask": ask,
            })

    out.sort(key=lambda item: item["timestamp"] or datetime.min.replace(tzinfo=et_tz()), reverse=True)
    return out


def latest_price_from_snapshot(snapshot: Dict[str, Any], session: str, current: Optional[datetime] = None) -> Tuple[float, str, Optional[datetime], int, float, float]:
    """
    Return the freshest valid extended-hours snapshot price.

    This is only a fallback/rough-selection helper. The validation pass prefers
    explicit extended-hours 1-minute bar close if a fresh bar exists.
    """
    for item in snapshot_price_candidates(snapshot, session, current):
        if is_fresh_ts(item.get("timestamp"), session, current):
            return (
                safe_float(item.get("price"), 0.0),
                str(item.get("source") or ""),
                item.get("timestamp"),
                safe_int(item.get("size"), 0),
                safe_float(item.get("bid"), 0.0),
                safe_float(item.get("ask"), 0.0),
            )
    return 0.0, "", None, 0, 0.0, 0.0

def snapshot_prev_close(snapshot: Dict[str, Any]) -> float:
    prev = snapshot.get("prevDailyBar") or snapshot.get("prev_daily_bar") or {}
    if not isinstance(prev, dict):
        return 0.0
    return safe_float(prev.get("c") or prev.get("close"), 0.0)


def snapshot_daily_close(snapshot: Dict[str, Any]) -> float:
    daily = snapshot.get("dailyBar") or snapshot.get("daily_bar") or {}
    if not isinstance(daily, dict):
        return 0.0
    return safe_float(daily.get("c") or daily.get("close"), 0.0)


def spread_pct(bid: float, ask: float, price: float) -> float:
    if bid <= 0 or ask <= 0 or price <= 0 or ask < bid:
        return 0.0
    return ((ask - bid) / price) * 100.0


def max_spread_pct_for_price(price: float) -> float:
    if price < 5:
        return MAX_SPREAD_UNDER_5
    if price < 10:
        return MAX_SPREAD_UNDER_10
    return MAX_SPREAD_10_PLUS


def max_stale_minutes_for_session(session: str) -> float:
    if str(session).lower() == "premarket":
        return PREMARKET_MAX_STALE_MINUTES
    return AFTER_HOURS_MAX_STALE_MINUTES


def age_minutes(ts: Optional[datetime], current: Optional[datetime] = None) -> Optional[float]:
    if ts is None:
        return None
    current = current or now_et()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=et_tz())
    return max(0.0, (current - ts.astimezone(et_tz())).total_seconds() / 60.0)


def is_fresh_ts(ts: Optional[datetime], session: str, current: Optional[datetime] = None) -> bool:
    age = age_minutes(ts, current)
    if age is None:
        return False
    return age <= max_stale_minutes_for_session(session)


def score_mover(change_pct: float, volume: float, notional: float, spread: float) -> int:
    score = 0
    score += min(55, max(0, change_pct) * 3.0)
    if volume >= 500_000:
        score += 20
    elif volume >= 100_000:
        score += 15
    elif volume >= 25_000:
        score += 10
    elif volume >= 5_000:
        score += 5

    if notional >= 5_000_000:
        score += 15
    elif notional >= 1_000_000:
        score += 12
    elif notional >= 250_000:
        score += 8
    elif notional >= 50_000:
        score += 4

    if spread and spread <= 1.0:
        score += 5
    elif spread and spread >= 5.0:
        score -= 5

    return int(max(0, min(100, round(score))))


def tier_for_score(score: int) -> int:
    if score >= 80:
        return 1
    if score >= 65:
        return 2
    if score >= 45:
        return 3
    return 4


def passes_liquidity(
    change_pct: float,
    volume: float,
    notional: float,
    bar_count: int,
    recent_10_volume: float,
    recent_20_volume: float,
    recent_20_notional: float,
) -> Tuple[bool, str]:
    """
    Extended-hours quality gate for high-volume breakouts/continuations.

    This is deliberately NOT the regular scanner quality filter. It removes:
      - single-print spikes
      - stale moves with no current participation
      - low-volume fake movers
      - names without meaningful extended-hours notional
    """
    if bar_count < MIN_EXTENDED_BARS:
        return False, "too_few_extended_bars"

    if volume < ABSOLUTE_MIN_EXTENDED_VOLUME:
        return False, "absolute_volume_too_low"

    if volume < MIN_EXTENDED_VOLUME:
        return False, "total_volume_too_low"

    if notional < MIN_EXTENDED_NOTIONAL:
        return False, "total_notional_too_low"

    if not (recent_10_volume >= RECENT_10_MIN_VOLUME or recent_20_volume >= RECENT_20_MIN_VOLUME):
        return False, "recent_volume_too_low"

    if recent_20_notional < RECENT_20_MIN_NOTIONAL:
        return False, "recent_notional_too_low"

    return True, "passed"

def resolve_current_extended_price(
    symbol: str,
    snapshot_row: Dict[str, Any],
    stats: Dict[str, Any],
    session: str,
    current: Optional[datetime] = None,
) -> Tuple[float, str, Optional[datetime], float, float]:
    """
    Resolve the current extended-hours price used for ranking.

    Priority:
      1. Fresh explicit 1-minute extended bar close from /v2/stocks/bars.
      2. Fresh latest trade/minute-bar snapshot fallback.
      3. Fresh quote midpoint only if the spread is acceptable.

    Never use the extended-hours high as the current price.
    """
    current = current or now_et()

    latest_bar_close = safe_float(stats.get("extended_latest_bar_close"), 0.0)
    latest_bar_time = parse_ts_to_et(stats.get("extended_latest_bar_time"))

    if latest_bar_close > 0 and is_fresh_ts(latest_bar_time, session, current):
        return latest_bar_close, "extended_bar_close", latest_bar_time, 0.0, 0.0

    latest = safe_float(snapshot_row.get("latest_price"), 0.0)
    latest_ts = snapshot_row.get("latest_time")
    if isinstance(latest_ts, str):
        latest_ts = parse_ts_to_et(latest_ts)

    bid = safe_float(snapshot_row.get("bid"), 0.0)
    ask = safe_float(snapshot_row.get("ask"), 0.0)
    source = str(snapshot_row.get("price_source_detail") or "")

    if latest > 0 and is_fresh_ts(latest_ts, session, current):
        if source == "quote_mid":
            spr = spread_pct(bid, ask, latest)
            if spr > 0 and spr <= max_spread_pct_for_price(latest):
                return latest, source, latest_ts, bid, ask
        else:
            return latest, source, latest_ts, bid, ask

    return 0.0, "", None, 0.0, 0.0


def build_movers(session: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    session = session.lower()
    if session not in {"premarket", "afterhours"}:
        raise ValueError("session must be premarket or afterhours")

    current = now_et()
    generated = iso_et(current)

    assets = fetch_assets()
    symbols = [a.symbol for a in assets]
    asset_map = {a.symbol: a for a in assets}

    print(f"Scanning {len(symbols)} listed symbols for {session} movers using Alpaca {DATA_FEED.upper()} snapshots")

    snapshots = fetch_snapshots(symbols)

    min_move = PREMARKET_MIN_MOVE_PCT if session == "premarket" else AFTER_HOURS_MIN_MOVE_PCT

    rough: List[Dict[str, Any]] = []
    skipped_no_snapshot = 0
    skipped_no_price = 0
    skipped_price_range = 0
    skipped_no_anchor = 0
    skipped_below_move = 0

    # After-hours anchor: first use daily close, then refresh the strongest
    # candidates with explicit 15:59 close anchor.
    for asset in assets:
        symbol = asset.symbol
        snap = snapshots.get(symbol)
        if not snap:
            skipped_no_snapshot += 1
            continue

        latest, source, latest_ts, latest_size, bid, ask = latest_price_from_snapshot(snap, session, current)
        if latest <= 0:
            skipped_no_price += 1
            continue

        if latest < MIN_PRICE or latest > MAX_PRICE:
            skipped_price_range += 1
            continue

        if session == "premarket":
            anchor = snapshot_prev_close(snap)
            anchor_label = "previous_regular_close"
        else:
            anchor = snapshot_daily_close(snap)
            anchor_label = "regular_16_close_snapshot"

        if anchor <= 0:
            skipped_no_anchor += 1
            continue

        change = ((latest - anchor) / anchor) * 100.0

        if change < min_move:
            skipped_below_move += 1
            continue

        rough.append({
            "symbol": symbol,
            "company_name": asset.name or symbol,
            "exchange": asset.exchange,
            "latest_price": latest,
            "price_source_detail": source,
            "latest_time": latest_ts,
            "latest_size": latest_size,
            "anchor_price": anchor,
            "anchor_label": anchor_label,
            "change_pct": change,
            "bid": bid,
            "ask": ask,
        })

    # Keep a wide pool before explicit bar-volume and close-anchor validation.
    rough.sort(key=lambda r: -safe_float(r.get("change_pct"), 0.0))
    validation_pool = rough[: max(MAX_OUTPUT_ROWS * 10, 250)]

    validation_symbols = [r["symbol"] for r in validation_pool]

    close_anchors = fetch_regular_close_anchors(validation_symbols, session, current) if session == "afterhours" else {}
    bar_stats = fetch_extended_bar_stats(validation_symbols, session, current)

    rows: List[Dict[str, Any]] = []
    skipped_liquidity = 0
    skipped_too_few_bars = 0
    skipped_absolute_volume = 0
    skipped_wide_spread = 0

    for r in validation_pool:
        symbol = str(r["symbol"])
        stats = bar_stats.get(symbol, {})
        latest, price_source_detail, latest_ts, bid, ask = resolve_current_extended_price(
            symbol,
            r,
            stats,
            session,
            current,
        )

        anchor = close_anchors.get(symbol, 0.0) if session == "afterhours" else safe_float(r["anchor_price"], 0.0)
        anchor_label = "regular_16_close" if session == "afterhours" and anchor > 0 else str(r["anchor_label"])

        if anchor <= 0:
            anchor = safe_float(r["anchor_price"], 0.0)

        if latest <= 0 or anchor <= 0:
            skipped_no_price += 1
            continue

        if latest < MIN_PRICE or latest > MAX_PRICE:
            skipped_price_range += 1
            continue

        change = ((latest - anchor) / anchor) * 100.0
        if change < min_move:
            skipped_below_move += 1
            continue

        ext_volume = safe_float(stats.get("extended_volume"), 0.0)
        ext_notional = safe_float(stats.get("extended_notional"), 0.0)
        ext_bar_count = int(safe_float(stats.get("extended_bar_count"), 0.0))
        recent_10_volume = safe_float(stats.get("recent_10_min_volume"), 0.0)
        recent_10_notional = safe_float(stats.get("recent_10_min_notional"), 0.0)
        recent_20_volume = safe_float(stats.get("recent_20_min_volume"), 0.0)
        recent_20_notional = safe_float(stats.get("recent_20_min_notional"), 0.0)

        # If bar volume is missing but latest trade has size, preserve a minimal
        # datapoint for diagnostics, but do not allow it to bypass the minimum
        # extended-bar-count quality gate.
        latest_size = safe_int(r.get("latest_size"), 0)
        if ext_volume <= 0 and latest_size > 0:
            ext_volume = float(latest_size)
            ext_notional = float(latest_size) * latest

        liquidity_ok, liquidity_reason = passes_liquidity(
            change,
            ext_volume,
            ext_notional,
            ext_bar_count,
            recent_10_volume,
            recent_20_volume,
            recent_20_notional,
        )
        if not liquidity_ok:
            if liquidity_reason == "too_few_extended_bars":
                skipped_too_few_bars += 1
            elif liquidity_reason == "absolute_volume_too_low":
                skipped_absolute_volume += 1
            else:
                skipped_liquidity += 1
            continue

        spr = spread_pct(bid, ask, latest)
        max_allowed_spread = max_spread_pct_for_price(latest)

        if spr > 0 and spr > max_allowed_spread:
            skipped_wide_spread += 1
            continue

        score = score_mover(change, ext_volume, ext_notional, spr)

        session_label = "Pre-Market" if session == "premarket" else "After-Hours"
        bucket = "PREMARKET_MOVER" if session == "premarket" else "AFTER_HOURS_MOVER"

        tags = [
            f"{session_label} +{change:.1f}%",
            f"Ext Vol {int(ext_volume):,}",
            f"Ext ${ext_notional/1_000_000:.2f}M",
            "Monitor Only",
        ]
        if spr > 0:
            tags.append(f"Spread {spr:.1f}%")

        latest_iso = latest_ts.isoformat(timespec="seconds") if isinstance(latest_ts, datetime) else ""

        row = {
            "symbol": symbol,
            "company_name": r.get("company_name", symbol),
            "exchange": r.get("exchange", ""),
            "price": round(latest, 4),
            "change_pct": round(change, 2),
            "score": score,
            "tier": tier_for_score(score),
            "setup_bucket": bucket,
            "risk_category": "MONITOR_ONLY",
            "tags": " · ".join(tags),
            "monitor_session": session.upper(),
            "monitor_only": "true",
            "execution_allowed": "false",
            "price_source": f"Alpaca {DATA_FEED.upper()} Extended",
            "price_source_detail": price_source_detail,
            "price_updated_at": latest_iso or generated,
            "latest_age_minutes": round(age_minutes(latest_ts, current) or 0.0, 2),
            "extended_anchor_label": anchor_label,
            "extended_anchor_price": round(anchor, 4),
            "extended_latest_price": round(latest, 4),
            "extended_change_pct": round(change, 2),
            "extended_volume": int(ext_volume),
            "extended_notional": round(ext_notional, 2),
            "extended_high": round(safe_float(stats.get("extended_high"), 0.0), 4),
            "extended_low": round(safe_float(stats.get("extended_low"), 0.0), 4),
            "extended_bar_count": ext_bar_count,
            "recent_10_min_volume": int(recent_10_volume),
            "recent_10_min_notional": round(recent_10_notional, 2),
            "recent_20_min_volume": int(recent_20_volume),
            "recent_20_min_notional": round(recent_20_notional, 2),
            "bid": round(bid, 4) if bid else "",
            "ask": round(ask, 4) if ask else "",
            "spread_pct": round(spr, 2) if spr else "",
            "max_allowed_spread_pct": round(max_allowed_spread, 2),
            "dollar_vol_M": round(ext_notional / 1_000_000, 3),
            "atr_pct": 0.0,
            "vwap_dist_pct": 0.0,
            "from_hod_pct": 0.0,
            "above_vwap": "",
            "sector": "Unknown",
            "sector_etf": "SPY",
            "sector_status": "UNKNOWN",
            "sector_change_pct": 0.0,
            "stock_vs_sector_pct": 0.0,
            "snapshot_generated_at_et": generated,
        }
        rows.append(row)

    rows.sort(key=lambda r: (-safe_float(r.get("score"), 0.0), -safe_float(r.get("change_pct"), 0.0), -safe_float(r.get("extended_notional"), 0.0), str(r.get("symbol"))))
    rows = rows[:MAX_OUTPUT_ROWS]

    metadata = {
        "version": VERSION,
        "session": session.upper(),
        "generated_at_et": generated,
        "monitor_only": True,
        "execution_allowed": False,
        "data_feed": DATA_FEED,
        "asset_universe_count": len(assets),
        "snapshot_count": len(snapshots),
        "rough_candidate_count": len(rough),
        "rows": len(rows),
        "max_rows": MAX_OUTPUT_ROWS,
        "price_min": MIN_PRICE,
        "price_max": MAX_PRICE,
        "min_move_pct": min_move,
        "min_extended_volume": MIN_EXTENDED_VOLUME,
        "min_extended_notional": MIN_EXTENDED_NOTIONAL,
        "absolute_min_extended_volume": ABSOLUTE_MIN_EXTENDED_VOLUME,
        "min_extended_bars": MIN_EXTENDED_BARS,
        "premarket_max_stale_minutes": PREMARKET_MAX_STALE_MINUTES,
        "after_hours_max_stale_minutes": AFTER_HOURS_MAX_STALE_MINUTES,
        "recent_10_min_volume": RECENT_10_MIN_VOLUME,
        "recent_20_min_volume": RECENT_20_MIN_VOLUME,
        "recent_20_min_notional": RECENT_20_MIN_NOTIONAL,
        "max_spread_under_5": MAX_SPREAD_UNDER_5,
        "max_spread_under_10": MAX_SPREAD_UNDER_10,
        "max_spread_10_plus": MAX_SPREAD_10_PLUS,
        "exclude_non_common_stocks": EXCLUDE_NON_COMMON_STOCKS,
        "skipped_no_snapshot": skipped_no_snapshot,
        "skipped_no_price": skipped_no_price,
        "skipped_price_range": skipped_price_range,
        "skipped_no_anchor": skipped_no_anchor,
        "skipped_below_move": skipped_below_move,
        "skipped_liquidity": skipped_liquidity,
        "skipped_too_few_extended_bars": skipped_too_few_bars,
        "skipped_absolute_volume": skipped_absolute_volume,
        "skipped_wide_spread": skipped_wide_spread,
        "ranking_method": (
            "fresh current extended price vs previous regular close; high-volume breakout/continuation"
            if session == "premarket"
            else "fresh current extended price vs regular 16:00 close; high-volume breakout/continuation"
        ),
    }

    return rows, metadata


def output_paths(session: str) -> Tuple[Path, Path]:
    if session.lower() == "premarket":
        return PROJECT_DIR / "premarket_movers.csv", PROJECT_DIR / "premarket_movers.json"
    return PROJECT_DIR / "after_hours_movers.csv", PROJECT_DIR / "after_hours_movers.json"


def run(session: str) -> int:
    rows, metadata = build_movers(session)
    csv_path, json_path = output_paths(session)

    write_csv(csv_path, rows)
    write_json(json_path, {"metadata": metadata, "symbols": rows, "movers": rows})

    print("=" * 70)
    print(f"EXTENDED HOURS MOVERS | session={session.upper()}")
    print(f"Generated: {metadata['generated_at_et']}")
    print(f"Universe: {metadata['asset_universe_count']} | Snapshots: {metadata['snapshot_count']} | Rough: {metadata['rough_candidate_count']}")
    print(f"Saved: {csv_path.name} ({len(rows)} rows)")
    print(f"Saved: {json_path.name}")
    print(
        f"Skipped below move: {metadata['skipped_below_move']} | "
        f"too few bars: {metadata['skipped_too_few_extended_bars']} | "
        f"abs vol low: {metadata['skipped_absolute_volume']} | "
        f"liquidity low: {metadata['skipped_liquidity']} | "
        f"wide spread: {metadata['skipped_wide_spread']}"
    )
    print("=" * 70)

    if rows:
        print("Top movers:")
        for row in rows[:12]:
            print(
                f"  {row['symbol']:>6}  {row['change_pct']:>7.2f}%  "
                f"${row['price']:<8}  Vol {row['extended_volume']:,}  "
                f"${row['extended_notional']/1_000_000:.2f}M"
            )
    else:
        print("No extended-hours movers passed the relaxed monitor-only thresholds.")

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor-only pre-market / after-hours mover scanner")
    parser.add_argument("--session", choices=["premarket", "afterhours"], required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.chdir(PROJECT_DIR)
    return run(args.session)


if __name__ == "__main__":
    raise SystemExit(main())
