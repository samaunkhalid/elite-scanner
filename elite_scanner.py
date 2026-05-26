"""
ELITE MULTI-SOURCE STOCK SCANNER v2.2 GAP FIELDS
======================================
ChatGPT Calibration Fixes Applied:

  ✅ Normalized scoring to 100 (was 105)
  ✅ Separated Catalyst from Momentum
  ✅ Tightened Execution layer (targets 50-60% hit rate)
  ✅ Demoted Squeeze to 8pts max (was 15)
  ✅ Added Extension Risk penalties (>25% = high-risk)
  ✅ Added Earnings Reaction tags
  ✅ Added IWM small-cap regime penalty

7-Layer Conviction Scoring (NORMALIZED TO 100):
  1. CATALYST     /15 — Real news/events only
  2. MOMENTUM     /20 — Big moves, RVOL, gaps (NEW - separated)
  3. EXECUTION    /20 — Strict liquidity requirements
  4. SQUEEZE      /8  — Rare bonus only (DEMOTED)
  5. STRENGTH     /15 — RS vs SPY/sector
  6. TECHNICAL    /12 — EMA stack, breakouts
  7. PARTICIPATION/10 — Accumulation + insider (renamed)

Total: 100 max

Tier System:
  S (80+): Highest conviction
  1 (65+): Strong setups
  2 (50+): Watching
  3 (35+): Monitor

Output:
  - Active Watchlist: Top 10 only
  - Raw scored universe: Full CSV for diagnostics
"""

from yahooquery import Ticker
import pandas as pd
import requests
import json
import os
import math
from pathlib import Path
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None



# ==============================================================
# EARLY RECLAIM RUNNER LANE — Alpaca SIP intraday-first discovery
# ==============================================================
# Purpose:
#   Catch GO/SFM-style early regular-session runners before they become
#   late near-HOD continuation setups.
#
# Locked design:
#   - Yahoo remains a seed/context source.
#   - Alpaca SIP intraday behavior is allowed to force-include qualifying
#     early reclaim runners even when Yahoo/base score is mediocre.
#   - This lane is regular-market only and does not affect premarket /
#     after-hours monitor-only scanners.
#
# User preference:
#   Early-runner hard price filter is $2–$80.
EARLY_RECLAIM_ENABLED = os.getenv("EARLY_RECLAIM_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
EARLY_RECLAIM_MIN_PRICE = float(os.getenv("EARLY_RECLAIM_MIN_PRICE", "2"))
EARLY_RECLAIM_MAX_PRICE = float(os.getenv("EARLY_RECLAIM_MAX_PRICE", "80"))
EARLY_RECLAIM_MAX_SYMBOLS = int(os.getenv("EARLY_RECLAIM_MAX_SYMBOLS", "900"))
EARLY_RECLAIM_FETCH_CHUNK = int(os.getenv("EARLY_RECLAIM_FETCH_CHUNK", "50"))
EARLY_RECLAIM_MIN_5M_VOLUME = float(os.getenv("EARLY_RECLAIM_MIN_5M_VOLUME", "2000"))
EARLY_RECLAIM_MIN_5M_NOTIONAL = float(os.getenv("EARLY_RECLAIM_MIN_5M_NOTIONAL", "15000"))
EARLY_RECLAIM_MIN_15M_VOLUME = float(os.getenv("EARLY_RECLAIM_MIN_15M_VOLUME", "5000"))
EARLY_RECLAIM_MIN_15M_NOTIONAL = float(os.getenv("EARLY_RECLAIM_MIN_15M_NOTIONAL", "35000"))
EARLY_RECLAIM_MAX_VWAP_DIST_PCT = float(os.getenv("EARLY_RECLAIM_MAX_VWAP_DIST_PCT", "3.5"))
EARLY_RECLAIM_MAX_BAR_AGE_MINUTES = float(os.getenv("EARLY_RECLAIM_MAX_BAR_AGE_MINUTES", "8"))
EARLY_RECLAIM_MIN_SCORE = float(os.getenv("EARLY_RECLAIM_MIN_SCORE", "64"))
EARLY_RECLAIM_FORCE_SCORE_FLOOR = float(os.getenv("EARLY_RECLAIM_FORCE_SCORE_FLOOR", "52"))
EARLY_RECLAIM_OUTPUT_LIMIT = int(os.getenv("EARLY_RECLAIM_OUTPUT_LIMIT", "30"))
EARLY_RECLAIM_HIGH_QUALITY_SCORE = float(os.getenv("EARLY_RECLAIM_HIGH_QUALITY_SCORE", "84"))
EARLY_RECLAIM_FIRST_ATTEMPT_BONUS = float(os.getenv("EARLY_RECLAIM_FIRST_ATTEMPT_BONUS", "7"))
EARLY_RECLAIM_SECOND_ATTEMPT_PENALTY = float(os.getenv("EARLY_RECLAIM_SECOND_ATTEMPT_PENALTY", "3"))
EARLY_RECLAIM_FAILED_ATTEMPT_PENALTY = float(os.getenv("EARLY_RECLAIM_FAILED_ATTEMPT_PENALTY", "7"))
EARLY_RECLAIM_REJECTION_FAIL_COUNT = int(os.getenv("EARLY_RECLAIM_REJECTION_FAIL_COUNT", "3"))

OPTIONAL_STATIC_UNIVERSE_FILES = [
    "static_liquid_universe.csv",
    "liquid_universe.csv",
    "regular_market_universe.csv",
]
OPTIONAL_SEED_FILES = [
    "premarket_movers.csv",
    "elite_watchlist_raw.csv",
    "potential_movers.csv",
    "active_momentum.csv",
]


def iso_now_et():
    """Return current New York time as ISO string for scanner metadata."""
    if ZoneInfo:
        return datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")
    return datetime.now().isoformat(timespec="seconds")


def write_scanner_meta(extra=None):
    """
    Write the true broad-scanner generation timestamp.

    This prevents dashboard rebuild time or Signal Desk refresh time from being
    confused with the broad scanner data timestamp.
    """
    meta = {
        "scanner_generated_at_et": iso_now_et(),
        "price_source_preference": "Alpaca SIP last intraday bar when available; Yahoo fallback",
        "note": "This is the broad scanner data timestamp. Signal Desk refresh may be newer.",
    }

    if isinstance(extra, dict):
        meta.update(extra)

    with open("scanner_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


# ==============================================================
# UNIVERSE
# ==============================================================

def get_dynamic_universe():
    """Fetch top gainers, losers, most active."""
    universe = set()
    screeners = ["day_gainers", "day_losers", "most_actives",
                 "small_cap_gainers", "growth_technology_stocks"]

    for screener in screeners:
        try:
            url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?count=100&scrIds={screener}"
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                quotes = data.get("finance", {}).get("result", [{}])[0].get("quotes", [])
                for q in quotes:
                    sym = q.get("symbol")
                    if sym and not any(c in sym for c in ["^", "=", "."]):
                        universe.add(sym)
        except Exception as e:
            print(f"  Screener {screener} failed: {e}")

    momentum_core = [
        "SOUN", "AI", "BBAI", "IONQ", "RGTI", "ARQQ", "PLTR",
        "RIOT", "MARA", "CLSK", "HUT", "BITF", "CIFR", "CORZ", "BTBT", "IREN", "MSTR", "COIN",
        "WOLF", "LSCC", "SMCI", "SMTC",
        "RIVN", "NIO", "LCID", "QS", "CHPT", "PLUG", "FCEL", "BE", "BLNK", "EVGO",
        "RKLB", "ASTS", "LUNR", "JOBY", "KTOS", "ACHR",
        "HIMS", "CRSP", "BNGO", "VKTX", "MDGL", "VRDN", "CYTK", "IOVA", "SAVA",
        "GME", "AMC", "BBBY", "BB", "NOK",
        "HOOD", "SOFI", "AFRM", "UPST", "NU",
        "RDDT", "PINS", "SNAP", "RBLX", "ROKU", "DKNG",
        "NET", "CRWD", "ZS", "PANW", "OKTA",
    ]
    universe.update(momentum_core)
    print(f"  Dynamic universe: {len(universe)} stocks")
    return list(universe)



def normalize_symbol_for_scanner(symbol):
    """Normalize symbols and remove obvious non-stock/index forms."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return ""
    if any(c in sym for c in ["^", "=", "/"]):
        return ""
    # Keep class shares such as BRK.B out of this low-priced early-runner lane;
    # the regular scanner can still see them if needed.
    if "." in sym:
        return ""
    return sym


def read_symbol_seed_file(path):
    """Read symbols from a CSV file if it exists. Tolerates many column names."""
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return []
        df = pd.read_csv(p)
        if df.empty:
            return []
        for col in ["symbol", "Symbol", "ticker", "Ticker"]:
            if col in df.columns:
                return [normalize_symbol_for_scanner(x) for x in df[col].dropna().tolist()]
        # Fallback: first column.
        return [normalize_symbol_for_scanner(x) for x in df.iloc[:, 0].dropna().tolist()]
    except Exception:
        return []


def build_early_reclaim_candidate_pool(base_universe, existing_results=None):
    """
    Build a hybrid candidate pool for the early reclaim lane.

    Important:
      - Does not rely only on Yahoo score/top-100.
      - Uses optional static liquid universe files when present.
      - Keeps current scanner universe and current/previous watchlists as seeds.
      - Caps total symbols to protect Alpaca.
    """
    ordered = []
    seen = set()

    def add_many(symbols, source_label):
        for sym in symbols or []:
            s = normalize_symbol_for_scanner(sym)
            if not s or s in seen:
                continue
            seen.add(s)
            ordered.append((s, source_label))

    add_many(base_universe, "dynamic_universe")

    if existing_results:
        add_many([r.get("symbol") for r in existing_results], "scored_results")

    for fname in OPTIONAL_STATIC_UNIVERSE_FILES:
        add_many(read_symbol_seed_file(fname), fname)

    for fname in OPTIONAL_SEED_FILES:
        add_many(read_symbol_seed_file(fname), fname)

    # Optional manual symbols for quick tests or user-curated names.
    extra = os.getenv("EARLY_RECLAIM_EXTRA_SYMBOLS", "").strip()
    if extra:
        add_many([x.strip() for x in extra.split(",")], "env_extra")

    # Stable ordering: dynamic/current universe first, static/old files later.
    symbols = [s for s, _ in ordered]
    return symbols[: max(50, EARLY_RECLAIM_MAX_SYMBOLS)]


def _alpaca_headers():
    key = (
        os.getenv("ALPACA_API_KEY")
        or os.getenv("APCA_API_KEY_ID")
        or ""
    ).strip()
    secret = (
        os.getenv("ALPACA_SECRET_KEY")
        or os.getenv("APCA_API_SECRET_KEY")
        or ""
    ).strip()
    if not key or not secret:
        return None
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def _to_utc_iso(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_alpaca_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _safe_float_local(value, default=0.0):
    try:
        if value is None:
            return default
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _session_vwap_from_bars(df):
    if df.empty:
        return 0.0
    vol = pd.to_numeric(df.get("v", pd.Series([0] * len(df))), errors="coerce").fillna(0)
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
    return _safe_float_local(close.iloc[-1] if len(close) else 0, 0)



# ==============================================================
# GAP / OPENING RANGE CONTEXT — v2.2
# ==============================================================
#
# Purpose:
#   Provide explicit fields required by signal_engine.py STRONG_GAP_BREAKOUT.
#   These fields are context only; they do not create trade signals by themselves.
#
# Fields emitted:
#   previous_close, session_open, gap_pct, gap_age_minutes,
#   premarket_high, opening_range_high, opening_range_low,
#   opening_range_source, gap_direction, strong_gap_up
#
# Safety:
#   - Yahoo quote fields are used only for broad gap context.
#   - Opening-range values are trusted only when sourced from Alpaca 1Min bars.
#   - Signal engine still decides WATCH / READY / ACTIVE.

def _now_et_for_scanner():
    try:
        if ZoneInfo:
            return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        pass
    return datetime.now()


def _minutes_since_regular_open_et(now_et=None):
    try:
        now_et = now_et or _now_et_for_scanner()
        session_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        return max(0.0, (now_et - session_start).total_seconds() / 60.0)
    except Exception:
        return 0.0


def calculate_gap_context_from_yahoo_quote(q, price=0.0):
    """
    Build gap context from Yahoo/yahooquery quote fields.

    This is intentionally conservative: Yahoo does not provide reliable opening
    range candles here, so opening_range_high/low remain 0 unless we later have
    Alpaca 1Min bars. Signal engine can use gap_pct/session_open from this and
    real OR levels from live bars/Alpaca when available.
    """
    q = q if isinstance(q, dict) else {}
    price = _safe_float_local(price, 0.0)

    previous_close = _safe_float_local(
        q.get("regularMarketPreviousClose")
        or q.get("previousClose")
        or q.get("postMarketPreviousClose"),
        0.0,
    )
    session_open = _safe_float_local(
        q.get("regularMarketOpen")
        or q.get("open")
        or q.get("regularMarketPrice")
        or price,
        price,
    )
    premarket_price = _safe_float_local(q.get("preMarketPrice"), 0.0)
    # Many Yahoo quote payloads do not expose preMarketHigh. Keep this
    # conservative and avoid fabricating a high from the full regular session.
    premarket_high = _safe_float_local(q.get("preMarketHigh") or q.get("preMarketDayHigh"), 0.0)
    if premarket_high <= 0 and premarket_price > 0:
        premarket_high = premarket_price

    gap_pct = 0.0
    if previous_close > 0 and session_open > 0:
        gap_pct = (session_open - previous_close) / previous_close * 100.0

    if gap_pct > 0.25:
        gap_direction = "UP"
    elif gap_pct < -0.25:
        gap_direction = "DOWN"
    else:
        gap_direction = "FLAT"

    return {
        "previous_close": round(previous_close, 4),
        "session_open": round(session_open, 4),
        "gap_pct": round(gap_pct, 2),
        "gap_age_minutes": round(_minutes_since_regular_open_et(), 1),
        "premarket_high": round(premarket_high, 4),
        "opening_range_high": 0.0,
        "opening_range_low": 0.0,
        "opening_range_minutes": 0,
        "opening_range_source": "MISSING",
        "gap_direction": gap_direction,
        "strong_gap_up": bool(gap_pct >= 2.0),
    }


def calculate_gap_context_from_intraday_bars(df, previous_close=0.0, premarket_high=0.0):
    """
    Build regular-session opening range context from Alpaca 1Min bars.

    Bars are expected to begin at/after 09:30 ET. This gives reliable:
      - session_open
      - opening_range_high / low for first 15 minutes
      - gap_age_minutes from latest bar timestamp

    previous_close/premarket_high are optional and can be supplied from Yahoo.
    """
    default = {
        "previous_close": round(_safe_float_local(previous_close, 0.0), 4),
        "session_open": 0.0,
        "gap_pct": 0.0,
        "gap_age_minutes": 0.0,
        "premarket_high": round(_safe_float_local(premarket_high, 0.0), 4),
        "opening_range_high": 0.0,
        "opening_range_low": 0.0,
        "opening_range_minutes": 0,
        "opening_range_source": "MISSING",
        "gap_direction": "FLAT",
        "strong_gap_up": False,
    }
    try:
        if df is None or df.empty or "ts" not in df.columns:
            return default

        work = df.dropna(subset=["ts"]).sort_values("ts").copy()
        if work.empty:
            return default

        for col in ["o", "h", "l", "c", "v"]:
            if col in work.columns:
                work[col] = pd.to_numeric(work[col], errors="coerce")

        first = work.iloc[0]
        latest = work.iloc[-1]
        session_open = _safe_float_local(first.get("o"), _safe_float_local(first.get("c"), 0.0))
        if session_open <= 0:
            return default

        latest_ts = latest["ts"]
        try:
            if getattr(latest_ts, "tzinfo", None) is None:
                latest_ts = latest_ts.tz_localize("UTC")
            latest_et = latest_ts.tz_convert("America/New_York")
            session_start = latest_et.replace(hour=9, minute=30, second=0, microsecond=0)
            gap_age = max(0.0, (latest_et - session_start).total_seconds() / 60.0)
        except Exception:
            gap_age = _minutes_since_regular_open_et()

        # First 15 regular-session minutes. If fewer than 15 bars exist, use what
        # exists and label minutes accordingly.
        opening = work.head(15)
        opening_high = _safe_float_local(opening["h"].max() if "h" in opening.columns else 0, 0.0)
        opening_low = _safe_float_local(opening["l"].min() if "l" in opening.columns else 0, 0.0)

        prev = _safe_float_local(previous_close, 0.0)
        gap_pct = ((session_open - prev) / prev * 100.0) if prev > 0 else 0.0
        if gap_pct > 0.25:
            gap_direction = "UP"
        elif gap_pct < -0.25:
            gap_direction = "DOWN"
        else:
            gap_direction = "FLAT"

        return {
            "previous_close": round(prev, 4),
            "session_open": round(session_open, 4),
            "gap_pct": round(gap_pct, 2),
            "gap_age_minutes": round(gap_age, 1),
            "premarket_high": round(_safe_float_local(premarket_high, 0.0), 4),
            "opening_range_high": round(opening_high, 4),
            "opening_range_low": round(opening_low, 4),
            "opening_range_minutes": int(min(len(opening), 15)),
            "opening_range_source": "ALPACA_1MIN",
            "gap_direction": gap_direction,
            "strong_gap_up": bool(gap_pct >= 2.0),
        }
    except Exception:
        return default


def _ema(series, span):
    return pd.to_numeric(series, errors="coerce").ewm(span=span, adjust=False).mean()


def _macd_hist(close):
    close = pd.to_numeric(close, errors="coerce")
    macd = _ema(close, 12) - _ema(close, 26)
    signal = _ema(macd, 9)
    return macd - signal


def fetch_alpaca_1min_bars_for_early_reclaim(symbols):
    """
    Fetch 1-minute regular-session bars for the candidate pool.

    This is separate from the normal top-100 Alpaca enrichment because the
    early lane must not let Yahoo/base score eliminate live intraday reclaimers.
    """
    headers = _alpaca_headers()
    if not headers:
        print("  ⚠️ Missing Alpaca credentials; skipping early reclaim lane")
        return {}

    if ZoneInfo:
        now_et = datetime.now(ZoneInfo("America/New_York"))
    else:
        now_et = datetime.now()

    # Early reclaim lane is regular-market only. Use 09:30 ET as session start.
    session_start = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if now_et < session_start:
        return {}

    feed = os.getenv("ALPACA_DATA_FEED", "sip").strip().lower() or "sip"
    base_url = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets").rstrip("/")
    out = {}

    clean = []
    seen = set()
    for s in symbols or []:
        sym = normalize_symbol_for_scanner(s)
        if sym and sym not in seen:
            seen.add(sym)
            clean.append(sym)

    for i in range(0, len(clean), EARLY_RECLAIM_FETCH_CHUNK):
        chunk = clean[i:i + EARLY_RECLAIM_FETCH_CHUNK]
        try:
            params = {
                "symbols": ",".join(chunk),
                "timeframe": "1Min",
                "start": _to_utc_iso(session_start),
                "end": _to_utc_iso(now_et),
                "limit": 10000,
                "adjustment": "raw",
                "sort": "asc",
                "feed": feed,
            }
            r = requests.get(
                f"{base_url}/v2/stocks/bars",
                headers=headers,
                params=params,
                timeout=20,
            )
            if r.status_code >= 400:
                print(f"  ⚠️ Early reclaim Alpaca chunk failed HTTP {r.status_code}: {','.join(chunk[:5])}...")
                continue
            payload = r.json() if r.content else {}
            bars = payload.get("bars", {}) or {}
            for sym, rows in bars.items():
                if rows:
                    out[str(sym).upper()] = rows
        except Exception as exc:
            print(f"  ⚠️ Early reclaim Alpaca chunk exception ({','.join(chunk[:5])}...): {exc}")

    return out


def analyze_vwap_reclaim_attempt_quality(resampled_5m, session_vwap):
    """
    Classify current VWAP reclaim quality from today's 5-minute bars.

    Purpose:
      - A first clean VWAP reclaim is materially different from a third/fourth
        reclaim after repeated VWAP rejections.
      - This function tracks attempts and failed attempts so the early reclaim
        lane does not treat repeated VWAP resistance as a clean runner.

    Definitions:
      - Attempt: a 5-minute close crosses from below VWAP to at/above VWAP.
      - Failure: after an attempt, one of the next two completed 5-minute bars
        closes back below VWAP. This avoids punishing a normal intrabar retest.
      - Current attempt: the most recent attempt while the latest close is still
        above VWAP.
    """
    default = {
        "vwap_reclaim_attempt_count": 0,
        "vwap_reclaim_failed_count": 0,
        "vwap_reclaim_current_attempt": 0,
        "vwap_reclaim_quality_label": "No VWAP Reclaim",
        "vwap_reclaim_quality_color": "neutral",
        "vwap_reclaim_quality_adjustment": 0.0,
        "vwap_reclaim_quality_warning": "",
    }

    try:
        if resampled_5m is None or len(resampled_5m) < 3 or session_vwap <= 0:
            return default

        closes = pd.to_numeric(resampled_5m["c"], errors="coerce").dropna()
        if len(closes) < 3:
            return default

        above = closes >= session_vwap
        attempt_indices = []
        for i in range(1, len(closes)):
            if (not bool(above.iloc[i - 1])) and bool(above.iloc[i]):
                attempt_indices.append(i)

        # If price is currently above VWAP and it was below earlier, but the
        # first cross occurred before our available resample boundary, still
        # treat it as one current reclaim attempt.
        if not attempt_indices and bool(above.iloc[-1]) and bool((closes.iloc[:-1] < session_vwap).any()):
            attempt_indices.append(len(closes) - 1)

        attempt_count = len(attempt_indices)
        if attempt_count == 0:
            return default

        failed_count = 0
        latest_idx = len(closes) - 1
        for idx in attempt_indices:
            # Do not score the newest/incomplete attempt as failed yet.
            if idx >= latest_idx:
                continue
            lookahead = closes.iloc[idx + 1:min(idx + 3, len(closes))]
            if len(lookahead) and bool((lookahead < session_vwap).any()):
                failed_count += 1

        current_attempt = attempt_count if bool(above.iloc[-1]) else 0

        if failed_count >= EARLY_RECLAIM_REJECTION_FAIL_COUNT:
            label = "VWAP Rejection Pattern"
            color = "red"
            adj = -15.0
            warning = f"{failed_count} failed VWAP reclaims today"
        elif failed_count >= 2:
            label = "Repeated VWAP Reclaims"
            color = "red"
            adj = -10.0
            warning = f"{failed_count} failed VWAP reclaims today"
        elif current_attempt <= 1 and failed_count == 0:
            label = "1st VWAP Reclaim"
            color = "green"
            adj = EARLY_RECLAIM_FIRST_ATTEMPT_BONUS
            warning = ""
        elif current_attempt == 2 or failed_count == 1:
            label = "2nd VWAP Attempt"
            color = "yellow"
            adj = -EARLY_RECLAIM_SECOND_ATTEMPT_PENALTY
            warning = f"{failed_count} failed VWAP reclaim" if failed_count else "Second reclaim attempt"
        else:
            label = f"VWAP Attempt {current_attempt}"
            color = "yellow"
            adj = -EARLY_RECLAIM_SECOND_ATTEMPT_PENALTY
            warning = ""

        return {
            "vwap_reclaim_attempt_count": int(attempt_count),
            "vwap_reclaim_failed_count": int(failed_count),
            "vwap_reclaim_current_attempt": int(current_attempt),
            "vwap_reclaim_quality_label": label,
            "vwap_reclaim_quality_color": color,
            "vwap_reclaim_quality_adjustment": float(adj),
            "vwap_reclaim_quality_warning": warning,
        }
    except Exception:
        return default


def analyze_early_reclaim_symbol(symbol, rows):
    """
    Return a dict if symbol qualifies as VWAP/EMA early reclaim runner.
    Otherwise return None.
    """
    if not rows:
        return None

    df = pd.DataFrame(rows)
    if df.empty or len(df) < 8:
        return None

    for col in ["o", "h", "l", "c", "v", "vw"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["c"]).copy()
    if df.empty or len(df) < 8:
        return None

    # Convert timestamp for freshness and 5-minute resampling.
    if "t" not in df.columns:
        return None
    df["ts"] = pd.to_datetime(df["t"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"]).sort_values("ts")
    if df.empty:
        return None

    latest = df.iloc[-1]
    price = _safe_float_local(latest.get("c"), 0)
    if price < EARLY_RECLAIM_MIN_PRICE or price > EARLY_RECLAIM_MAX_PRICE:
        return None

    now_utc = datetime.now(timezone.utc)
    latest_ts = latest["ts"].to_pydatetime()
    age_minutes = max(0.0, (now_utc - latest_ts).total_seconds() / 60.0)
    if age_minutes > EARLY_RECLAIM_MAX_BAR_AGE_MINUTES:
        return None

    session_vwap = _session_vwap_from_bars(df)
    if session_vwap <= 0:
        return None

    df["ema9_1m"] = _ema(df["c"], 9)
    ema9_1m = _safe_float_local(df["ema9_1m"].iloc[-1], 0)
    if ema9_1m <= 0:
        return None

    # 5-minute structure confirmation.
    res = df.set_index("ts").resample("5min").agg({
        "o": "first",
        "h": "max",
        "l": "min",
        "c": "last",
        "v": "sum",
    }).dropna(subset=["c"])
    if len(res) < 3:
        return None

    res["ema9_5m"] = _ema(res["c"], 9)
    last5 = res.iloc[-1]
    prev5 = res.iloc[-2] if len(res) >= 2 else last5

    latest_5m_vol = _safe_float_local(last5.get("v"), 0)
    latest_5m_notional = latest_5m_vol * price
    last15 = res.tail(3)
    last15_vol = _safe_float_local(last15["v"].sum(), 0)
    last15_notional = float((last15["v"] * last15["c"]).sum())
    prev_5m_vol = _safe_float_local(prev5.get("v"), 0)
    prev_5m_notional = prev_5m_vol * _safe_float_local(prev5.get("c"), price)

    # Light volume/notional gates designed to catch GO-type early runners.
    if not (latest_5m_vol >= EARLY_RECLAIM_MIN_5M_VOLUME or latest_5m_notional >= EARLY_RECLAIM_MIN_5M_NOTIONAL):
        return None
    if not (last15_vol >= EARLY_RECLAIM_MIN_15M_VOLUME or last15_notional >= EARLY_RECLAIM_MIN_15M_NOTIONAL):
        return None

    # Require activity to be increasing or at least not dead.
    accelerating = (
        latest_5m_vol > prev_5m_vol
        or latest_5m_notional > prev_5m_notional
        or last15_vol >= max(EARLY_RECLAIM_MIN_15M_VOLUME * 1.5, 1)
    )
    if not accelerating:
        return None

    vwap_dist_pct = ((price - session_vwap) / session_vwap * 100.0) if session_vwap > 0 else 999
    if price < session_vwap:
        return None
    if vwap_dist_pct > EARLY_RECLAIM_MAX_VWAP_DIST_PCT:
        return None

    if price < ema9_1m:
        return None

    # 1-minute early reclaim evidence:
    recent_1m = df.tail(8).copy()
    recent_below_vwap = bool((recent_1m["c"].iloc[:-1] < session_vwap).any())
    recent_below_ema = bool((recent_1m["c"].iloc[:-1] < recent_1m["ema9_1m"].iloc[:-1]).any())
    one_min_reclaim = (recent_below_vwap and price >= session_vwap) or (recent_below_ema and price >= ema9_1m)

    # 5-minute confirmation:
    ema9_5m = _safe_float_local(last5.get("ema9_5m"), 0)
    prev_5m_close = _safe_float_local(prev5.get("c"), 0)
    prev_5m_ema = _safe_float_local(prev5.get("ema9_5m"), 0)
    five_min_above = price >= session_vwap and (ema9_5m <= 0 or price >= ema9_5m)
    five_min_reclaim = (
        five_min_above
        and (
            prev_5m_close < session_vwap
            or (prev_5m_ema > 0 and prev_5m_close < prev_5m_ema)
            or _safe_float_local(last5.get("l"), price) <= max(session_vwap, ema9_5m if ema9_5m > 0 else session_vwap) * 1.003
        )
    )

    if not (one_min_reclaim and five_min_above):
        return None

    hist = _macd_hist(df["c"])
    macd_rising = False
    if len(hist.dropna()) >= 3:
        h = hist.dropna().tail(3).tolist()
        macd_rising = h[-1] > h[-2] or (h[-1] > h[-3] and h[-1] >= -0.01)

    if not macd_rising:
        return None

    reclaim_quality = analyze_vwap_reclaim_attempt_quality(res, session_vwap)
    reclaim_quality_adj = _safe_float_local(reclaim_quality.get("vwap_reclaim_quality_adjustment"), 0)

    # Score prioritizes current, early reclaim quality — not near-HOD.
    early_score = 45.0 + reclaim_quality_adj
    reasons = []

    quality_label = str(reclaim_quality.get("vwap_reclaim_quality_label", "") or "")
    quality_warning = str(reclaim_quality.get("vwap_reclaim_quality_warning", "") or "")
    if quality_label and quality_label != "No VWAP Reclaim":
        reasons.append(quality_label)
    if quality_warning:
        reasons.append(quality_warning)

    if one_min_reclaim:
        early_score += 8
        reasons.append("1Min VWAP/EMA reclaim")
    if five_min_reclaim:
        early_score += 10
        reasons.append("5Min reclaim/hold")
    elif five_min_above:
        early_score += 6
        reasons.append("5Min above VWAP/EMA")
    if macd_rising:
        early_score += 7
        reasons.append("MACD curling up")
    if accelerating:
        early_score += 7
        reasons.append("Recent volume accelerating")
    if 0 <= vwap_dist_pct <= 1.8:
        early_score += 7
        reasons.append("Not extended from VWAP")
    elif vwap_dist_pct <= EARLY_RECLAIM_MAX_VWAP_DIST_PCT:
        early_score += 3
        reasons.append("Moderate VWAP extension")
    if latest_5m_vol >= 5000 or latest_5m_notional >= 35000:
        early_score += 4
        reasons.append("Usable 5Min participation")

    early_score = max(0, min(100, early_score))

    if early_score < EARLY_RECLAIM_MIN_SCORE:
        return None

    hod = _safe_float_local(df["h"].max(), price)
    lod = _safe_float_local(df["l"].min(), price)
    from_hod_pct = ((price - hod) / hod * 100.0) if hod > 0 else 0.0
    session_open = _safe_float_local(df.iloc[0].get("o"), price)
    intraday_change_pct = ((price - session_open) / session_open * 100.0) if session_open > 0 else 0.0
    gap_context = calculate_gap_context_from_intraday_bars(df)

    return {
        "symbol": symbol,
        "price": round(price, 4),
        "change_pct": round(intraday_change_pct, 2),
        "early_reclaim_score": round(early_score, 1),
        "early_reclaim_runner": True,
        "early_reclaim_reason": " · ".join(reasons[:8]),
        "vwap_reclaim_attempt_count": reclaim_quality.get("vwap_reclaim_attempt_count", 0),
        "vwap_reclaim_failed_count": reclaim_quality.get("vwap_reclaim_failed_count", 0),
        "vwap_reclaim_current_attempt": reclaim_quality.get("vwap_reclaim_current_attempt", 0),
        "vwap_reclaim_quality_label": reclaim_quality.get("vwap_reclaim_quality_label", ""),
        "vwap_reclaim_quality_color": reclaim_quality.get("vwap_reclaim_quality_color", "neutral"),
        "vwap_reclaim_quality_adjustment": reclaim_quality.get("vwap_reclaim_quality_adjustment", 0),
        "vwap_reclaim_quality_warning": reclaim_quality.get("vwap_reclaim_quality_warning", ""),
        "early_reclaim_high_quality": bool(early_score >= EARLY_RECLAIM_HIGH_QUALITY_SCORE and reclaim_quality.get("vwap_reclaim_quality_color") != "red"),
        "early_reclaim_latest_5m_volume": int(latest_5m_vol),
        "early_reclaim_latest_5m_notional": round(latest_5m_notional, 0),
        "early_reclaim_last15_volume": int(last15_vol),
        "early_reclaim_last15_notional": round(last15_notional, 0),
        "early_reclaim_bar_age_minutes": round(age_minutes, 1),
        "early_reclaim_vwap": round(session_vwap, 4),
        "early_reclaim_ema9_1m": round(ema9_1m, 4),
        "early_reclaim_ema9_5m": round(ema9_5m, 4),
        "vwap": round(session_vwap, 4),
        "vwap_dist_pct": round(vwap_dist_pct, 2),
        "above_vwap": True,
        "hod": round(hod, 4),
        "lod": round(lod, 4),
        "from_hod_pct": round(from_hod_pct, 2),
        "near_hod": bool(from_hod_pct >= -2.0),
        "intraday_volume": int(_safe_float_local(df["v"].sum(), 0)),
        "price_source": "Alpaca SIP Early Reclaim",
        "price_updated_at": latest.get("t", ""),
        "data_source": "Alpaca SIP Early Reclaim",
        "intraday_setup_type": "VWAP_EMA_RECLAIM_RUNNER",
        "previous_close": gap_context.get("previous_close", 0),
        "session_open": gap_context.get("session_open", 0),
        "gap_pct": gap_context.get("gap_pct", 0),
        "gap_age_minutes": gap_context.get("gap_age_minutes", 0),
        "premarket_high": gap_context.get("premarket_high", 0),
        "opening_range_high": gap_context.get("opening_range_high", 0),
        "opening_range_low": gap_context.get("opening_range_low", 0),
        "opening_range_minutes": gap_context.get("opening_range_minutes", 0),
        "opening_range_source": gap_context.get("opening_range_source", "MISSING"),
        "gap_direction": gap_context.get("gap_direction", "FLAT"),
        "strong_gap_up": gap_context.get("strong_gap_up", False),
    }


def build_early_reclaim_rows(base_universe, existing_results):
    """
    Return early reclaim rows keyed by symbol.

    Existing normal scanner rows can be upgraded. New qualifying symbols can be
    force-included so Yahoo/base scoring is not the first elimination gate.
    """
    if not EARLY_RECLAIM_ENABLED:
        return {}

    pool = build_early_reclaim_candidate_pool(base_universe, existing_results)
    if not pool:
        return {}

    print(f"\n[Stage 6A] Alpaca SIP early reclaim lane...")
    print(f"  Candidate pool: {len(pool)} symbols | price ${EARLY_RECLAIM_MIN_PRICE:g}-${EARLY_RECLAIM_MAX_PRICE:g}")

    bars_by_symbol = fetch_alpaca_1min_bars_for_early_reclaim(pool)
    if not bars_by_symbol:
        print("  No Alpaca bars returned for early reclaim lane")
        return {}

    early = {}
    checked = 0
    for symbol, rows in bars_by_symbol.items():
        checked += 1
        try:
            hit = analyze_early_reclaim_symbol(symbol, rows)
            if hit:
                early[symbol] = hit
        except Exception:
            continue

    ranked = dict(sorted(early.items(), key=lambda kv: kv[1].get("early_reclaim_score", 0), reverse=True)[:EARLY_RECLAIM_OUTPUT_LIMIT])
    print(f"  Early reclaim checked: {checked} | qualified: {len(ranked)}")
    if ranked:
        preview = ", ".join([f"{s}({v.get('early_reclaim_score')})" for s, v in list(ranked.items())[:10]])
        print(f"  Early reclaim preview: {preview}")
    return ranked


def apply_early_reclaim_to_results(results, early_rows):
    """
    Merge early reclaim hits into normal scanner results.

    Existing rows are upgraded. New rows are force-included as Potential Movers
    with enough structure for signal_engine.py to evaluate them.
    """
    if not early_rows:
        return results, 0, 0

    by_symbol = {str(r.get("symbol", "")).upper(): r for r in results}
    upgraded = 0
    added = 0

    for symbol, er in early_rows.items():
        if symbol in by_symbol:
            stock = by_symbol[symbol]
            stock["early_reclaim_runner"] = True
            stock["intraday_setup_type"] = "VWAP_EMA_RECLAIM_RUNNER"
            stock["early_reclaim_score"] = er.get("early_reclaim_score", 0)
            stock["early_reclaim_reason"] = er.get("early_reclaim_reason", "")
            for _field in [
                "vwap_reclaim_attempt_count",
                "vwap_reclaim_failed_count",
                "vwap_reclaim_current_attempt",
                "vwap_reclaim_quality_label",
                "vwap_reclaim_quality_color",
                "vwap_reclaim_quality_adjustment",
                "vwap_reclaim_quality_warning",
                "early_reclaim_high_quality",
            ]:
                stock[_field] = er.get(_field, stock.get(_field, ""))
            stock["price"] = er.get("price", stock.get("price", 0))
            stock["intraday_last_price"] = er.get("price", stock.get("price", 0))
            stock["price_source"] = er.get("price_source", stock.get("price_source", "Alpaca SIP"))
            stock["price_updated_at"] = er.get("price_updated_at", stock.get("price_updated_at", ""))
            stock["vwap"] = er.get("vwap", stock.get("vwap", 0))
            stock["vwap_dist_pct"] = er.get("vwap_dist_pct", stock.get("vwap_dist_pct", 0))
            stock["above_vwap"] = True
            stock["hod"] = er.get("hod", stock.get("hod", 0))
            stock["lod"] = er.get("lod", stock.get("lod", 0))
            stock["from_hod_pct"] = er.get("from_hod_pct", stock.get("from_hod_pct", 0))
            stock["near_hod"] = er.get("near_hod", stock.get("near_hod", False))
            stock["intraday_volume"] = er.get("intraday_volume", stock.get("intraday_volume", 0))
            # Preserve Yahoo previous-close/gap context when present; use Alpaca
            # 1Min bars to add reliable opening-range levels.
            for _field in [
                "opening_range_high",
                "opening_range_low",
                "opening_range_minutes",
                "opening_range_source",
            ]:
                stock[_field] = er.get(_field, stock.get(_field, 0))
            for _field in [
                "previous_close",
                "session_open",
                "gap_pct",
                "gap_age_minutes",
                "premarket_high",
                "gap_direction",
                "strong_gap_up",
            ]:
                _v = er.get(_field, None)
                if _v not in [None, "", 0, 0.0, "0", "0.0"]:
                    stock[_field] = _v
                else:
                    stock[_field] = stock.get(_field, _v if _v is not None else 0)
            stock["data_source"] = "Yahoo + Alpaca SIP Early Reclaim"
            stock["score"] = min(100, max(float(stock.get("score", 0) or 0), EARLY_RECLAIM_FORCE_SCORE_FLOOR) + 4)
            existing_tags = str(stock.get("tags", "") or "")
            prefix = f"VWAP/EMA reclaim runner · {er.get('early_reclaim_reason', '')}"
            stock["tags"] = " · ".join([x for x in [prefix, existing_tags] if x])[:500]
            upgraded += 1
        else:
            row = {
                "tier": "2" if er.get("early_reclaim_score", 0) >= 70 else "3",
                "symbol": symbol,
                "company_name": symbol,
                "sector": "Unknown",
                "sector_etf": "SPY",
                "sector_change_pct": 0,
                "stock_vs_sector_pct": 0,
                "sector_vs_spy_pct": 0,
                "sector_status": "UNKNOWN",
                "sector_score": 0,
                "price": er.get("price", 0),
                "change_pct": er.get("change_pct", 0),
                "previous_close": er.get("previous_close", 0),
                "session_open": er.get("session_open", 0),
                "gap_pct": er.get("gap_pct", 0),
                "gap_age_minutes": er.get("gap_age_minutes", 0),
                "premarket_high": er.get("premarket_high", 0),
                "opening_range_high": er.get("opening_range_high", 0),
                "opening_range_low": er.get("opening_range_low", 0),
                "opening_range_minutes": er.get("opening_range_minutes", 0),
                "opening_range_source": er.get("opening_range_source", "MISSING"),
                "gap_direction": er.get("gap_direction", "FLAT"),
                "strong_gap_up": er.get("strong_gap_up", False),
                "score": max(EARLY_RECLAIM_FORCE_SCORE_FLOOR, er.get("early_reclaim_score", 0)),
                "base_score": 0,
                "ext_penalty": 0,
                "regime_penalty": 0,
                "risk_category": "NORMAL",
                "is_earnings_reaction": False,
                "catalyst": 0,
                "momentum": 0,
                "execution": 0,
                "squeeze": 0,
                "strength": 0,
                "technical": 0,
                "participation": 0,
                "social": 0,
                "short_pct": 0,
                "float_M": 0,
                "days_to_cover": 0,
                "atr_pct": 0,
                "dollar_vol_M": round(er.get("intraday_volume", 0) * er.get("price", 0) / 1e6, 2),
                "market_cap_B": 0,
                "days_to_earnings": "—",
                "tags": f"VWAP/EMA reclaim runner · {er.get('early_reclaim_reason', '')}",
                "early_reclaim_runner": True,
                "intraday_setup_type": "VWAP_EMA_RECLAIM_RUNNER",
                "early_reclaim_score": er.get("early_reclaim_score", 0),
                "early_reclaim_reason": er.get("early_reclaim_reason", ""),
                "vwap_reclaim_attempt_count": er.get("vwap_reclaim_attempt_count", 0),
                "vwap_reclaim_failed_count": er.get("vwap_reclaim_failed_count", 0),
                "vwap_reclaim_current_attempt": er.get("vwap_reclaim_current_attempt", 0),
                "vwap_reclaim_quality_label": er.get("vwap_reclaim_quality_label", ""),
                "vwap_reclaim_quality_color": er.get("vwap_reclaim_quality_color", "neutral"),
                "vwap_reclaim_quality_adjustment": er.get("vwap_reclaim_quality_adjustment", 0),
                "vwap_reclaim_quality_warning": er.get("vwap_reclaim_quality_warning", ""),
                "early_reclaim_high_quality": er.get("early_reclaim_high_quality", False),
                "intraday_last_price": er.get("price", 0),
                "price_source": er.get("price_source", "Alpaca SIP Early Reclaim"),
                "price_updated_at": er.get("price_updated_at", ""),
                "vwap": er.get("vwap", 0),
                "vwap_dist_pct": er.get("vwap_dist_pct", 0),
                "above_vwap": True,
                "hod": er.get("hod", 0),
                "lod": er.get("lod", 0),
                "from_hod_pct": er.get("from_hod_pct", 0),
                "near_hod": er.get("near_hod", False),
                "intraday_volume": er.get("intraday_volume", 0),
                "data_source": "Alpaca SIP Early Reclaim",
            }
            results.append(row)
            by_symbol[symbol] = row
            added += 1

    return results, upgraded, added


# ==============================================================
# STAGE 0: MARKET REGIME (ENHANCED)
# ==============================================================

def detect_market_regime():
    """Detect market regime with IWM small-cap tracking."""
    print(f"\n[Stage 0] Detecting market regime...")
    
    indicators = ["SPY", "QQQ", "IWM", "^VIX"]
    regime = {
        "spy_change": 0, "qqq_change": 0, "iwm_change": 0,
        "vix_level": 20, "vix_change": 0,
        "regime": "NORMAL", "bias": "NEUTRAL", "label": "Mixed market",
        "smallcap_caution": False,
    }
    
    try:
        t = Ticker(indicators, asynchronous=True)
        prices = t.price
        
        if isinstance(prices, dict):
            spy = prices.get("SPY", {})
            qqq = prices.get("QQQ", {})
            iwm = prices.get("IWM", {})
            vix = prices.get("^VIX", {})
            
            if isinstance(spy, dict):
                regime["spy_change"] = (spy.get("regularMarketChangePercent", 0) or 0) * 100
            if isinstance(qqq, dict):
                regime["qqq_change"] = (qqq.get("regularMarketChangePercent", 0) or 0) * 100
            if isinstance(iwm, dict):
                regime["iwm_change"] = (iwm.get("regularMarketChangePercent", 0) or 0) * 100
            if isinstance(vix, dict):
                regime["vix_level"] = vix.get("regularMarketPrice", 20) or 20
                regime["vix_change"] = (vix.get("regularMarketChangePercent", 0) or 0) * 100
        
        spy_chg = regime["spy_change"]
        iwm_chg = regime["iwm_change"]
        vix_lv = regime["vix_level"]
        
        # Small-cap caution flag (FIX #7)
        if iwm_chg < -1.0:
            regime["smallcap_caution"] = True
        
        # Classify regime
        if vix_lv > 25:
            regime["regime"] = "HIGH_VOLATILITY"
            regime["label"] = f"⚠️ High volatility (VIX {vix_lv:.0f})"
            regime["bias"] = "CAUTION"
        elif spy_chg > 1.5 and regime["qqq_change"] > 1.5:
            regime["regime"] = "RISK_ON"
            regime["label"] = f"🟢 Strong risk-on ({spy_chg:+.1f}%)"
            regime["bias"] = "LONG_FAVORED"
        elif spy_chg > 0.5:
            regime["regime"] = "BULLISH"
            regime["label"] = f"🟢 Bullish ({spy_chg:+.1f}%)"
            regime["bias"] = "LONG_FAVORED"
        elif spy_chg < -1.5:
            regime["regime"] = "RISK_OFF"
            regime["label"] = f"🔴 Risk-off ({spy_chg:+.1f}%)"
            regime["bias"] = "SHORT_FAVORED"
        elif spy_chg < -0.5:
            regime["regime"] = "BEARISH"
            regime["label"] = f"🔴 Bearish ({spy_chg:+.1f}%)"
            regime["bias"] = "SHORT_FAVORED"
        elif abs(spy_chg) < 0.3 and vix_lv < 15:
            regime["regime"] = "CHOPPY"
            regime["label"] = f"😴 Choppy/quiet ({spy_chg:+.1f}%)"
            regime["bias"] = "REDUCE_SIZE"
        else:
            regime["regime"] = "NORMAL"
            regime["label"] = f"⚪ Normal ({spy_chg:+.1f}%)"
            regime["bias"] = "NEUTRAL"
        
        if regime["smallcap_caution"]:
            regime["label"] += " | ⚠️ Small-cap weak"
        
        print(f"  SPY: {spy_chg:+.2f}% | QQQ: {regime['qqq_change']:+.2f}% | "
              f"IWM: {iwm_chg:+.2f}% | VIX: {vix_lv:.1f}")
        print(f"  Regime: {regime['regime']} | Bias: {regime['bias']}")
        print(f"  Label:  {regime['label']}")
    except Exception as e:
        print(f"  Regime detection failed: {e}")
    
    return regime


# ==============================================================
# HELPERS
# ==============================================================

def get_avg_volume(quote, history_df=None):
    """Try multiple volume sources."""
    avg_vol = (
        quote.get("averageDailyVolume3Month")
        or quote.get("averageDailyVolume10Day")
        or quote.get("averageVolume")
        or quote.get("averageVolume10days")
        or 0
    )
    avg_vol = avg_vol or 0
    
    if avg_vol == 0 and history_df is not None and len(history_df) >= 20:
        try:
            avg_vol = float(history_df["volume"].tail(20).mean())
        except:
            pass
    
    return avg_vol


def get_atr_pct(history_df, periods=14):
    """Calculate ATR as % of price."""
    if history_df is None or len(history_df) < periods + 1:
        return 0
    try:
        df = history_df.tail(periods + 1).copy()
        df["prev_close"] = df["close"].shift(1)
        df["tr1"] = df["high"] - df["low"]
        df["tr2"] = abs(df["high"] - df["prev_close"])
        df["tr3"] = abs(df["low"] - df["prev_close"])
        df["tr"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
        atr = df["tr"].tail(periods).mean()
        price = df["close"].iloc[-1]
        if price > 0:
            return (atr / price) * 100
    except:
        pass
    return 0


def check_earnings_status(symbol, calendar_all):
    """
    Check earnings status. Returns (within_2d, within_48h_past, days_to_earnings).
    FIX #3: Tag recent earnings reactions.
    """
    try:
        if isinstance(calendar_all, dict):
            sym_cal = calendar_all.get(symbol, {})
            if isinstance(sym_cal, dict):
                earnings = sym_cal.get("earnings", {})
                if isinstance(earnings, dict):
                    earnings_date = earnings.get("earningsDate")
                    if earnings_date:
                        try:
                            if isinstance(earnings_date, list) and earnings_date:
                                edate = pd.to_datetime(earnings_date[0])
                            else:
                                edate = pd.to_datetime(earnings_date)
                            now = pd.Timestamp.now(tz=edate.tz) if edate.tz else pd.Timestamp.now()
                            days_to = (edate - now).days
                            
                            within_2d = (days_to >= 0 and days_to <= 2)
                            within_48h_past = (days_to < 0 and days_to >= -2)
                            
                            return within_2d, within_48h_past, days_to
                        except:
                            pass
    except:
        pass
    return False, False, None


# ==============================================================
# SECTOR / INDUSTRY RELATIVE STRENGTH
# ==============================================================

SECTOR_ETFS = {
    "Semiconductors": "SMH",
    "Technology": "XLK",
    "Software": "IGV",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Biotechnology": "XBI",
    "Industrials": "XLI",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Communication Services": "XLC",
    "Energy": "XLE",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
    "Crypto": "IBIT",
    "Aerospace & Defense": "ITA",
    "Solar": "TAN",
    "Automobiles": "CARZ",
    "Unknown": "SPY",
}

SYMBOL_SECTOR_OVERRIDES = {
    # Semiconductors
    "GFS": "Semiconductors",
    "AMKR": "Semiconductors",
    "STM": "Semiconductors",
    "AMD": "Semiconductors",
    "INTC": "Semiconductors",
    "QCOM": "Semiconductors",
    "NVDA": "Semiconductors",
    "MU": "Semiconductors",
    "SMCI": "Semiconductors",
    "AVGO": "Semiconductors",
    "MRVL": "Semiconductors",
    "LSCC": "Semiconductors",
    "WOLF": "Semiconductors",

    # Crypto / digital assets
    "RIOT": "Crypto",
    "MARA": "Crypto",
    "CLSK": "Crypto",
    "CORZ": "Crypto",
    "IREN": "Crypto",
    "HUT": "Crypto",
    "BITF": "Crypto",
    "CIFR": "Crypto",
    "COIN": "Crypto",
    "MSTR": "Crypto",

    # Space / defense / aviation
    "RKLB": "Aerospace & Defense",
    "LUNR": "Aerospace & Defense",
    "ASTS": "Aerospace & Defense",
    "KTOS": "Aerospace & Defense",

    # EV / mobility
    "TSLA": "Automobiles",
    "RIVN": "Automobiles",
    "LCID": "Automobiles",
    "NIO": "Automobiles",
    "XPEV": "Automobiles",
    "LI": "Automobiles",
    "JOBY": "Automobiles",
    "ACHR": "Automobiles",

    # Solar
    "RUN": "Solar",
    "CSIQ": "Solar",
    "ENPH": "Solar",
    "SEDG": "Solar",
}


def normalize_sector(raw_sector, symbol=None):
    """Normalize Yahoo sector names into ETF-mapped sectors."""
    symbol = str(symbol or "").upper()

    if symbol in SYMBOL_SECTOR_OVERRIDES:
        return SYMBOL_SECTOR_OVERRIDES[symbol]

    s = str(raw_sector or "").strip()

    if not s:
        return "Unknown"

    if s in SECTOR_ETFS:
        return s

    mapping = {
        "Technology": "Technology",
        "Financial Services": "Financial Services",
        "Healthcare": "Healthcare",
        "Industrials": "Industrials",
        "Consumer Cyclical": "Consumer Cyclical",
        "Consumer Defensive": "Consumer Defensive",
        "Communication Services": "Communication Services",
        "Energy": "Energy",
        "Basic Materials": "Basic Materials",
        "Real Estate": "Real Estate",
        "Utilities": "Utilities",
    }

    return mapping.get(s, "Unknown")


def fetch_sector_context(regime):
    """
    Fetch sector ETF same-day % change.
    Lightweight sector-leadership context for stock-level scoring.
    """
    etfs = sorted(set(SECTOR_ETFS.values()) | {"SPY", "QQQ", "IWM", "SMH", "ARKK"})
    context = {}

    try:
        t = Ticker(etfs, asynchronous=True)
        prices = t.price

        for etf in etfs:
            q = prices.get(etf, {}) if isinstance(prices, dict) else {}
            if not isinstance(q, dict):
                continue

            chg = (q.get("regularMarketChangePercent", 0) or 0) * 100
            px = q.get("regularMarketPrice", 0) or 0

            context[etf] = {
                "change_pct": chg,
                "price": px,
            }

    except Exception as e:
        print(f"  Sector ETF context failed: {e}")

    return context


def build_symbol_sector_map(universe, profile_all):
    """
    Build symbol -> sector map from Yahoo summary_profile.
    Fallback to overrides / Unknown.
    """
    symbol_sector = {}

    for symbol in universe:
        raw_sector = ""

        try:
            if isinstance(profile_all, dict):
                profile = profile_all.get(symbol, {})
                if isinstance(profile, dict):
                    raw_sector = profile.get("sector", "") or ""
        except Exception:
            raw_sector = ""

        symbol_sector[symbol] = normalize_sector(raw_sector, symbol)

    return symbol_sector


def score_sector_alignment(symbol, change_pct, sector, sector_context, regime):
    """
    Sector alignment overlay.
    Returns score adjustment, reasons, data.

    Professional intent:
    - Reward stocks aligned with leading sectors.
    - Modestly reward stocks outperforming their sector.
    - Penalize stocks fighting weak/rotating-out sectors.
    """
    score = 0
    reasons = []

    sector = sector or "Unknown"
    etf = SECTOR_ETFS.get(sector, "SPY")

    sector_chg = sector_context.get(etf, {}).get("change_pct", 0)
    spy_chg = regime.get("spy_change", 0)

    stock_vs_sector = change_pct - sector_chg
    sector_vs_spy = sector_chg - spy_chg

    if sector == "Unknown":
        sector_status = "UNKNOWN"
    elif sector_vs_spy >= 0.75 and sector_chg > 0:
        sector_status = "LEADING"
    elif sector_vs_spy >= 0.25:
        sector_status = "IMPROVING"
    elif sector_vs_spy <= -0.75:
        sector_status = "WEAK"
    else:
        sector_status = "NEUTRAL"

    if sector_status == "LEADING" and stock_vs_sector >= 1.0:
        score += 4
        reasons.append("Sector leading")
        reasons.append(f"Vs sector +{stock_vs_sector:.1f}%")
    elif sector_status in ["LEADING", "IMPROVING"] and stock_vs_sector >= 0:
        score += 2
        reasons.append("Sector supportive")
    elif sector_status == "WEAK" and stock_vs_sector < 0:
        score -= 4
        reasons.append("Sector weak")
    elif sector_status == "WEAK":
        score -= 2
        reasons.append("Sector headwind")

    data = {
        "sector": sector,
        "sector_etf": etf,
        "sector_change_pct": round(sector_chg, 2),
        "stock_vs_sector_pct": round(stock_vs_sector, 2),
        "sector_vs_spy_pct": round(sector_vs_spy, 2),
        "sector_status": sector_status,
    }

    return score, reasons, data



# ==============================================================
# HARD REJECT
# ==============================================================

def hard_reject(symbol, price, market_cap, avg_vol, exchange, atr_pct, earnings_within_2d):
    """Returns (rejected, reason)."""
    if price < 5.0:
        return True, "price_too_low"
    if price > 100.0:
        return True, "price_too_high"
    if market_cap < 100_000_000:
        return True, "mkt_cap_too_small"
    if avg_vol < 200_000:
        return True, "avg_vol_too_low"
    
    dollar_vol = avg_vol * price
    if dollar_vol < 5_000_000:
        return True, "dollar_vol_too_low"
    
    if exchange and exchange not in ["NMS", "NYQ", "ASE", "NGM", "PCX", "BTS", "NCM"]:
        return True, "bad_exchange"
    
    if atr_pct > 0:
        if atr_pct < 1.0:
            return True, "too_low_volatility"
        if atr_pct > 15:
            return True, "too_volatile"
    
    if earnings_within_2d:
        return True, "earnings_imminent"
    
    return False, ""


# ==============================================================
# LAYER 1: CATALYST (FIX #2 - Real events only)
# ==============================================================

def score_catalyst(symbol, days_to_earnings):
    """
    Catalyst score (0-15 points) - REAL events only.
    FIX #2: Separated from momentum scoring.
    """
    score = 0
    reasons = []
    
    # Earnings sweet spot (not too close, not too far)
    if days_to_earnings is not None:
        if 3 <= days_to_earnings <= 7:
            score += 8
            reasons.append(f"Earnings in {days_to_earnings}d")
        elif 8 <= days_to_earnings <= 15:
            score += 5
            reasons.append(f"Earnings in {days_to_earnings}d")
        elif 16 <= days_to_earnings <= 30:
            score += 2
    
    # Note: News/FDA/analyst would go here if we had those feeds
    # For now, earnings is the only real catalyst we can track
    
    return min(score, 15), reasons


# ==============================================================
# LAYER 2: MOMENTUM (FIX #2 - NEW, separated from catalyst)
# ==============================================================

def score_momentum(symbol, change_pct, history_df, today_vol, avg_vol):
    """
    Momentum score (0-20 points).
    FIX #2: Separated from catalyst - this is RESULT, not REASON.
    """
    score = 0
    reasons = []
    
    # Big move today
    abs_move = abs(change_pct)
    if abs_move >= 20:
        score += 10
        reasons.append(f"Major move {change_pct:+.1f}%")
    elif abs_move >= 10:
        score += 6
        reasons.append(f"Big move {change_pct:+.1f}%")
    elif abs_move >= 5:
        score += 3
    
    # Volume surge (3-day vs 20-day)
    if history_df is not None and len(history_df) >= 23:
        try:
            recent_vol = history_df["volume"].tail(3).mean()
            base_vol = history_df["volume"].iloc[-23:-3].mean()
            if base_vol > 0:
                vol_surge = recent_vol / base_vol
                if vol_surge > 4:
                    score += 7
                    reasons.append(f"Vol surge {vol_surge:.1f}x")
                elif vol_surge > 2.5:
                    score += 4
                elif vol_surge > 1.5:
                    score += 2
        except:
            pass
    
    # RVOL
    if avg_vol > 0 and today_vol > 0:
        rvol = today_vol / avg_vol
        if rvol > 3:
            score += 3
            reasons.append(f"RVOL {rvol:.1f}x")
        elif rvol > 2:
            score += 2
    
    return min(score, 20), reasons


# ==============================================================
# LAYER 3: EXECUTION
# ==============================================================

def score_execution(symbol, price, avg_vol, atr_pct, today_vol):
    """
    Execution Quality score (0-20 points).
    Uses price, liquidity, ATR, and RVOL.
    """
    score = 0
    reasons = []

    dollar_vol = avg_vol * price
    today_dollar_vol = today_vol * price

    # Dollar volume
    if dollar_vol > 100_000_000 and today_dollar_vol > 10_000_000:
        score += 8
        reasons.append(f"${dollar_vol/1e6:.0f}M liq")
    elif dollar_vol > 50_000_000 and today_dollar_vol > 5_000_000:
        score += 5
        reasons.append(f"${dollar_vol/1e6:.0f}M liq")
    elif dollar_vol > 25_000_000 and today_dollar_vol > 3_000_000:
        score += 3
    elif dollar_vol < 15_000_000 or today_dollar_vol < 1_000_000:
        score -= 5

    # ATR sweet spot
    if 2.0 <= atr_pct <= 7.0:
        score += 6
        reasons.append(f"ATR {atr_pct:.1f}% (clean)")
    elif 1.5 <= atr_pct < 2.0 or 7.0 < atr_pct <= 9.0:
        score += 3
    elif atr_pct > 10:
        score -= 3

    # Price sweet spot: $5–$100 scanner
    if 10 <= price <= 100:
        score += 4
        reasons.append("Clean price")
    elif 5 <= price < 10:
        score += 2
        reasons.append("Lower-price tradable")
    else:
        score -= 5

    # RVOL
    if avg_vol > 0:
        rvol = today_vol / avg_vol
        if rvol > 2.5:
            score += 2
        elif rvol < 1.0:
            score -= 2

    return max(0, min(score, 20)), reasons


# ==============================================================
# LAYER 4: SQUEEZE (FIX #5 - DEMOTED TO 8 MAX)
# ==============================================================

def score_squeeze(symbol, key_stats_all, avg_vol, today_vol, change_pct):
    """
    Squeeze score (0-8 points) - RARE bonus only.
    FIX #5: Reduced from 15 to 8, requires multiple conditions.
    """
    score = 0
    reasons = []
    short_pct = 0
    float_size = 0
    days_to_cover = 0

    try:
        if isinstance(key_stats_all, dict):
            sym_stats = key_stats_all.get(symbol, {})
            if isinstance(sym_stats, dict):
                short_pct = sym_stats.get("shortPercentOfFloat", 0) or 0
                if isinstance(short_pct, (int, float)):
                    short_pct = short_pct * 100
                float_size = sym_stats.get("floatShares", 0) or 0
                days_to_cover = sym_stats.get("shortRatio", 0) or 0

                # Only score if meaningful squeeze setup exists
                has_high_si = short_pct >= 15
                has_dtc = days_to_cover >= 3
                has_rvol = (today_vol / avg_vol > 2) if avg_vol > 0 else False
                float_M = float_size / 1e6 if float_size > 0 else 0
                good_float = 20 <= float_M <= 150
                
                # Require at least 2 conditions
                conditions_met = sum([has_high_si, has_dtc, has_rvol, good_float])
                
                if conditions_met >= 2:
                    if short_pct >= 30:
                        score += 4
                        reasons.append(f"SI {short_pct:.0f}%")
                    elif short_pct >= 20:
                        score += 3
                        reasons.append(f"SI {short_pct:.0f}%")
                    elif short_pct >= 15:
                        score += 2
                    
                    if days_to_cover >= 7:
                        score += 2
                        reasons.append(f"DTC {days_to_cover:.1f}d")
                    elif days_to_cover >= 5:
                        score += 1
                    
                    if good_float:
                        score += 2
                        reasons.append(f"Float {float_M:.0f}M")
    except:
        pass

    return min(score, 8), reasons, {"short_pct": short_pct, "float": float_size, "days_to_cover": days_to_cover}


# ==============================================================
# LAYER 5: STRENGTH (15 - unchanged)
# ==============================================================

def score_strength(symbol, history_df, spy_df, change_pct, regime):
    """RS score (0-15 points)."""
    score = 0
    reasons = []

    if history_df is None or len(history_df) < 60 or spy_df is None or len(spy_df) < 60:
        return score, reasons

    try:
        timeframe_score = 0
        for period, points in [(5, 3), (20, 4), (60, 3)]:
            if len(history_df) >= period and len(spy_df) >= period:
                stock_ret = (history_df["close"].iloc[-1] / history_df["close"].iloc[-period] - 1) * 100
                spy_ret = (spy_df["close"].iloc[-1] / spy_df["close"].iloc[-period] - 1) * 100
                if stock_ret > spy_ret:
                    timeframe_score += points

        score += timeframe_score
        if timeframe_score >= 8:
            reasons.append("RS strong (5/20/60d)")
        elif timeframe_score >= 5:
            reasons.append("RS positive")

        high_52w = history_df["close"].tail(252).max() if len(history_df) >= 252 else history_df["close"].max()
        current = history_df["close"].iloc[-1]
        if high_52w > 0:
            pct_from_high = ((high_52w - current) / high_52w) * 100
            if pct_from_high < 5:
                score += 3
                reasons.append("Near 52WH")
            elif pct_from_high > 50:
                low_52w = history_df["close"].tail(252).min() if len(history_df) >= 252 else history_df["close"].min()
                if low_52w > 0:
                    pct_from_low = ((current - low_52w) / low_52w) * 100
                    if pct_from_low > 50:
                        score += 2
                        reasons.append("V-recovery")

        if change_pct > 5:
            score += 2
        elif change_pct > 2:
            score += 1
        
        # Regime alignment
        bias = regime.get("bias", "NEUTRAL")
        if bias == "LONG_FAVORED" and change_pct > 0:
            score += 2
        elif bias == "SHORT_FAVORED" and change_pct < 0:
            score += 2
    except:
        pass

    return min(score, 15), reasons


# ==============================================================
# LAYER 6: TECHNICAL (12 - reduced from 15)
# ==============================================================

def score_technical(symbol, history_df):
    """Technical score (0-12 points)."""
    score = 0
    reasons = []

    if history_df is None or len(history_df) < 50:
        return score, reasons

    try:
        df = history_df.copy()
        df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
        df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["SMA50"] = df["close"].rolling(50).mean()

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        if latest["close"] > latest["EMA9"] > latest["EMA20"] > latest["SMA50"]:
            score += 4
            reasons.append("EMA stack ↑")
        elif latest["close"] > latest["EMA20"]:
            score += 2

        if prev["close"] > 0:
            gap_pct = ((latest["open"] - prev["close"]) / prev["close"]) * 100
            if abs(gap_pct) > 3:
                score += 3
                reasons.append(f"Gap {gap_pct:+.1f}%")
            elif abs(gap_pct) > 1.5:
                score += 1

        high_20 = df["close"].tail(20).max()
        if high_20 > 0:
            pct_from_high = ((high_20 - latest["close"]) / high_20) * 100
            if pct_from_high < 2:
                score += 2
                reasons.append("Near 20D high")
            elif pct_from_high < 5:
                score += 1

        if latest["close"] > latest["open"] and latest["close"] > prev["close"]:
            score += 2

        bar_range = latest["high"] - latest["low"]
        if bar_range > 0:
            close_position = (latest["close"] - latest["low"]) / bar_range
            if close_position > 0.75:
                score += 1
    except:
        pass

    return min(score, 12), reasons


# ==============================================================
# LAYER 7: PARTICIPATION (10 - renamed from smart_money)
# ==============================================================

def score_participation(symbol, key_stats_all, history_df):
    """Participation score (0-10 points) - renamed for clarity."""
    score = 0
    reasons = []

    try:
        if history_df is not None and len(history_df) >= 20:
            df = history_df.tail(20).copy()
            df["change"] = df["close"].pct_change()
            up_vol = df[df["change"] > 0]["volume"].mean()
            dn_vol = df[df["change"] < 0]["volume"].mean()
            if up_vol > 0 and dn_vol > 0 and not pd.isna(up_vol) and not pd.isna(dn_vol):
                acc_ratio = up_vol / dn_vol
                if acc_ratio > 1.5:
                    score += 5
                    reasons.append(f"Accumulating {acc_ratio:.1f}x")
                elif acc_ratio > 1.2:
                    score += 3

        if isinstance(key_stats_all, dict):
            sym_stats = key_stats_all.get(symbol, {})
            if isinstance(sym_stats, dict):
                insider_pct = sym_stats.get("heldPercentInsiders", 0) or 0
                if isinstance(insider_pct, (int, float)):
                    insider_pct *= 100
                    if insider_pct >= 20:
                        score += 5
                        reasons.append(f"Insider {insider_pct:.0f}%")
                    elif insider_pct >= 10:
                        score += 3
                        reasons.append(f"Insider {insider_pct:.0f}%")
                    elif insider_pct >= 5:
                        score += 1
    except:
        pass

    return min(score, 10), reasons


# ==============================================================
# SOCIAL (unchanged at 10)
# ==============================================================

def fetch_social_data():
    try:
        url = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            social_map = {}
            for item in results:
                try:
                    ticker = item.get("ticker")
                    if not ticker:
                        continue
                    
                    def safe_int(val, default=0):
                        if val is None:
                            return default
                        try:
                            return int(val)
                        except:
                            return default

                    mentions = safe_int(item.get("mentions"), 0)
                    m_24h = safe_int(item.get("mentions_24h_ago"), 1) or 1
                    rank = safe_int(item.get("rank"), 999)

                    social_map[ticker] = {
                        "mentions": mentions,
                        "growth": mentions / m_24h if m_24h > 0 else 1,
                        "rank": rank,
                    }
                except:
                    continue
            return social_map
    except Exception as e:
        print(f"  Social data fetch failed: {e}")
    return {}


def score_social(symbol, social_map):
    score = 0
    reasons = []

    if symbol in social_map:
        data = social_map[symbol]
        rank = data["rank"]
        growth = data["growth"]

        if rank <= 10:
            score += 8
            reasons.append(f"WSB #{rank}")
        elif rank <= 25:
            score += 5
            reasons.append(f"WSB #{rank}")
        elif rank <= 50:
            score += 2

        if growth >= 3:
            score = min(score + 2, 10)
            reasons.append(f"{growth:.1f}x mentions")

    return score, reasons


# ==============================================================
# EXTENSION RISK (FIX #6 - NEW)
# ==============================================================

def assess_extension_risk(change_pct):
    """
    FIX #6: Penalize already-extended stocks.
    Returns (penalty_pts, risk_category).
    """
    abs_move = abs(change_pct)
    
    if abs_move > 100:
        return -20, "EXTREME_MOVE"
    elif abs_move > 50:
        return -15, "HIGH_RISK"
    elif abs_move > 25:
        return -10, "EXTENDED"
    else:
        return 0, "NORMAL"

def assign_tier(score):
    """Recalculate tier after Alpaca intraday score is added."""
    if score >= 80:
        return "S"
    elif score >= 65:
        return "1"
    elif score >= 50:
        return "2"
    elif score >= 35:
        return "3"
    return "—"


def boolish(value):
    """Robust bool conversion for scanner fields."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"true", "1", "yes", "y"}


def classify_setup_bucket(stock):
    """
    Long-only bucket classifier.

    User rule:
    - This dashboard is for manual LONG trading only.
    - Negative-change / short-biased tickers must NOT appear in Potential Movers
      or Active Momentum.
    - Downside movers can remain in raw diagnostics only, but not in the main
      decision buckets.

    Main decision buckets:
    - POTENTIAL_MOVER: positive, earlier long setup.
    - ACTIVE_MOMENTUM: positive, already-moving long momentum.
    """
    change_signed = float(stock.get("change_pct", 0) or 0)
    change_abs = abs(change_signed)

    risk = stock.get("risk_category", "NORMAL")
    catalyst_sentiment = stock.get("catalyst_sentiment", "NONE")

    above_vwap = boolish(stock.get("above_vwap", False))
    near_hod = boolish(stock.get("near_hod", False))
    vwap_dist = float(stock.get("vwap_dist_pct", 999) or 999)
    recent_range = float(stock.get("recent_range_pct", 999) or 999)

    # Hard long-only gate.
    # No negative/flat movers are allowed into Potential or Active Momentum.
    if change_signed <= 0:
        return "MONITOR"

    if risk == "NEWS_RISK" or catalyst_sentiment == "NEGATIVE":
        return "HIGH_RISK_EXTREME"

    # Highest risk first.
    if risk in ["HIGH_RISK", "EXTREME_MOVE"]:
        return "HIGH_RISK_EXTREME"

    if risk == "EXTENDED" or change_abs >= 25:
        return "EXTENDED_CHASE_RISK"

    # Early reclaim runner lane:
    # - All detected early reclaim names can be displayed in the dashboard lane.
    # - Only high-quality names are force-promoted into POTENTIAL_MOVER so
    #   signal_engine.py can create a new WATCH candidate.
    # - Lower-quality or repeated VWAP rejection names remain MONITOR unless they
    #   also qualify through the normal continuation rules below.
    if boolish(stock.get("early_reclaim_runner", False)):
        price = float(stock.get("price", 0) or 0)
        early_score = float(stock.get("early_reclaim_score", 0) or 0)
        failed_reclaims = int(float(stock.get("vwap_reclaim_failed_count", 0) or 0))
        quality_color = str(stock.get("vwap_reclaim_quality_color", "") or "").lower()
        if (
            EARLY_RECLAIM_MIN_PRICE <= price <= EARLY_RECLAIM_MAX_PRICE
            and above_vwap
            and vwap_dist <= EARLY_RECLAIM_MAX_VWAP_DIST_PCT
            and risk == "NORMAL"
            and early_score >= EARLY_RECLAIM_HIGH_QUALITY_SCORE
            and failed_reclaims < 2
            and quality_color != "red"
        ):
            stock["early_reclaim_bucket_promoted"] = True
            return "POTENTIAL_MOVER"
        stock["early_reclaim_bucket_promoted"] = False

    # Active momentum = already moving upward, but not extreme.
    if 12 <= change_signed < 25:
        return "ACTIVE_MOMENTUM"

    # Potential mover = cleaner, earlier upward setup.
    if (
        0 < change_signed <= 12
        and above_vwap
        and vwap_dist <= 6
        and (near_hod or recent_range <= 1.8)
    ):
        return "POTENTIAL_MOVER"

    return "MONITOR"
# ==============================================================
# MAIN
# ==============================================================

def main():
    print("\n" + "=" * 70)
    print("ELITE MULTI-SOURCE STOCK SCANNER v2.1 (Calibrated)")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    regime = detect_market_regime()

    print(f"\n[Stage 1] Building dynamic universe...")
    universe = get_dynamic_universe()

    print(f"\n[Stage 2] Fetching SPY benchmark...")
    spy_df = None
    try:
        spy = Ticker("SPY")
        spy_data = spy.history(period="1y", interval="1d")
        if isinstance(spy_data, pd.DataFrame) and not spy_data.empty:
            spy_df = spy_data.reset_index().sort_values("date")
    except Exception as e:
        print(f"  SPY fetch failed: {e}")

    print(f"\n[Stage 3] Fetching Reddit/WSB sentiment data...")
    social_map = fetch_social_data()
    print(f"  Got social data for {len(social_map)} tickers")

    print(f"\n[Stage 4] Fetching market data for {len(universe)} stocks...")
    print(f"  This may take 2-3 minutes...")

    tickers = Ticker(universe, asynchronous=True)
    quotes = tickers.price
    
    print(f"  Fetching historical data...")
    try:
        history = tickers.history(period="1y", interval="1d")
    except Exception as e:
        print(f"  History fetch failed: {e}")
        history = pd.DataFrame()

    print(f"  Fetching key stats...")
    try:
        key_stats_all = tickers.key_stats
    except Exception as e:
        print(f"  Key stats fetch failed: {e}")
        key_stats_all = {}

    print(f"  Fetching calendar events...")
    try:
        calendar_all = tickers.calendar_events
    except Exception as e:
        print(f"  Calendar fetch failed: {e}")
        calendar_all = {}

    print(f"  Fetching company profiles for sector mapping...")
    try:
        profile_all = tickers.summary_profile
    except Exception as e:
        print(f"  Company profile fetch failed: {e}")
        profile_all = {}

    print(f"  Fetching sector ETF context...")
    sector_context = fetch_sector_context(regime)
    symbol_sector_map = build_symbol_sector_map(universe, profile_all)

    print(f"\n[Stage 5] Scoring with 7 layers + extension risk...\n")
    
    layer_stats = {name: {"hits": 0, "total_pts": 0, "max_seen": 0}
                   for name in ["catalyst", "momentum", "execution", "squeeze",
                                "strength", "technical", "participation", "social"]}
    score_distribution = {b: 0 for b in ["0-9","10-19","20-29","30-39","40-49","50-59","60-69","70-79","80+"]}
    quality_reasons = {}
    extension_categories = {"NORMAL": 0, "EXTENDED": 0, "HIGH_RISK": 0, "EXTREME_MOVE": 0}
    quality_filtered = 0
    total_processed = 0
    earnings_blocked = 0
    earnings_reaction_count = 0

    results = []
    for symbol in universe:
        try:
            q = quotes.get(symbol, {})
            if not isinstance(q, dict):
                quality_filtered += 1
                continue

            price = q.get("regularMarketPrice", 0) or 0
            change_pct = (q.get("regularMarketChangePercent", 0) or 0) * 100
            today_vol = q.get("regularMarketVolume", 0) or 0
            market_cap = q.get("marketCap", 0) or 0
            exchange = q.get("exchange", "")
            gap_context = calculate_gap_context_from_yahoo_quote(q, price)

            hist_df = None
            try:
                if symbol in history.index.get_level_values(0):
                    hist_df = history.loc[symbol].copy().reset_index()
                    hist_df["date"] = pd.to_datetime(hist_df["date"])
                    hist_df = hist_df.sort_values("date").reset_index(drop=True)
            except:
                pass

            avg_vol = get_avg_volume(q, hist_df)
            atr_pct = get_atr_pct(hist_df)
            within_2d, within_48h_past, days_to_earnings = check_earnings_status(symbol, calendar_all)
            
            rejected, reason = hard_reject(symbol, price, market_cap, avg_vol, exchange, atr_pct, within_2d)
            if rejected:
                quality_reasons[reason] = quality_reasons.get(reason, 0) + 1
                quality_filtered += 1
                if reason == "earnings_imminent":
                    earnings_blocked += 1
                continue

            # FIX #3: Track earnings reactions
            is_earnings_reaction = within_48h_past
            if is_earnings_reaction:
                earnings_reaction_count += 1

            # Score all layers
            cat_score, cat_reasons = score_catalyst(symbol, days_to_earnings)
            mom_score, mom_reasons = score_momentum(symbol, change_pct, hist_df, today_vol, avg_vol)
            exec_score, exec_reasons = score_execution(symbol, price, avg_vol, atr_pct, today_vol)
            sq_score, sq_reasons, sq_data = score_squeeze(symbol, key_stats_all, avg_vol, today_vol, change_pct)
            rs_score, rs_reasons = score_strength(symbol, hist_df, spy_df, change_pct, regime)
            tech_score, tech_reasons = score_technical(symbol, hist_df)
            part_score, part_reasons = score_participation(symbol, key_stats_all, hist_df)
            soc_score, soc_reasons = score_social(symbol, social_map)

            # Sector / industry alignment overlay
            sector = symbol_sector_map.get(symbol, "Unknown")
            sector_score, sector_reasons, sector_data = score_sector_alignment(
                symbol=symbol,
                change_pct=change_pct,
                sector=sector,
                sector_context=sector_context,
                regime=regime,
            )

            # Track diagnostics
            for name, score in [("catalyst", cat_score), ("momentum", mom_score),
                                 ("execution", exec_score), ("squeeze", sq_score),
                                 ("strength", rs_score), ("technical", tech_score),
                                 ("participation", part_score), ("social", soc_score)]:
                if score > 0:
                    layer_stats[name]["hits"] += 1
                layer_stats[name]["total_pts"] += score
                if score > layer_stats[name]["max_seen"]:
                    layer_stats[name]["max_seen"] = score
            
            total_processed += 1
            
            # Base score (normalized to 100)
            base_total = cat_score + mom_score + exec_score + sq_score + rs_score + tech_score + part_score + soc_score
            
            # FIX #6: Extension risk penalty
            ext_penalty, risk_cat = assess_extension_risk(change_pct)
            extension_categories[risk_cat] += 1
            
            # FIX #7: Small-cap regime penalty
            float_M = sq_data.get("float", 0) / 1e6
            regime_penalty = 0
            if regime.get("smallcap_caution") and float_M < 500 and change_pct > 0:
                regime_penalty = -5
            
            final_score = base_total + ext_penalty + regime_penalty + sector_score
            final_score = max(0, min(100, final_score))

            # Distribution tracking
            if final_score < 10:
                score_distribution["0-9"] += 1
            elif final_score < 20:
                score_distribution["10-19"] += 1
            elif final_score < 30:
                score_distribution["20-29"] += 1
            elif final_score < 40:
                score_distribution["30-39"] += 1
            elif final_score < 50:
                score_distribution["40-49"] += 1
            elif final_score < 60:
                score_distribution["50-59"] += 1
            elif final_score < 70:
                score_distribution["60-69"] += 1
            elif final_score < 80:
                score_distribution["70-79"] += 1
            else:
                score_distribution["80+"] += 1

            if final_score < 25:
                continue

            # Tier (normalized to 100)
            if final_score >= 80:
                tier = "S"
            elif final_score >= 65:
                tier = "1"
            elif final_score >= 50:
                tier = "2"
            else:
                tier = "3"

            # Build tags
            all_reasons = (cat_reasons + mom_reasons + exec_reasons + sq_reasons +
                           rs_reasons + tech_reasons + part_reasons + soc_reasons +
                           sector_reasons)
            
            # Add special tags
            if is_earnings_reaction:
                all_reasons.insert(0, "📊 EARNINGS REACTION")
            if risk_cat != "NORMAL":
                all_reasons.insert(0, f"⚠️ {risk_cat.replace('_', ' ')}")
            
            tags = " · ".join(all_reasons[:7])

            results.append({
                "tier": tier,
                "symbol": symbol,
                "company_name": q.get("shortName") or q.get("longName") or symbol,
                "sector": sector_data.get("sector", "Unknown"),
                "sector_etf": sector_data.get("sector_etf", "SPY"),
                "sector_change_pct": sector_data.get("sector_change_pct", 0),
                "stock_vs_sector_pct": sector_data.get("stock_vs_sector_pct", 0),
                "sector_vs_spy_pct": sector_data.get("sector_vs_spy_pct", 0),
                "sector_status": sector_data.get("sector_status", "UNKNOWN"),
                "sector_score": sector_score,
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "previous_close": gap_context.get("previous_close", 0),
                "session_open": gap_context.get("session_open", 0),
                "gap_pct": gap_context.get("gap_pct", 0),
                "gap_age_minutes": gap_context.get("gap_age_minutes", 0),
                "premarket_high": gap_context.get("premarket_high", 0),
                "opening_range_high": gap_context.get("opening_range_high", 0),
                "opening_range_low": gap_context.get("opening_range_low", 0),
                "opening_range_minutes": gap_context.get("opening_range_minutes", 0),
                "opening_range_source": gap_context.get("opening_range_source", "MISSING"),
                "gap_direction": gap_context.get("gap_direction", "FLAT"),
                "strong_gap_up": gap_context.get("strong_gap_up", False),
                "score": final_score,
                "base_score": base_total,
                "ext_penalty": ext_penalty,
                "regime_penalty": regime_penalty,
                "risk_category": risk_cat,
                "is_earnings_reaction": is_earnings_reaction,
                "catalyst": cat_score,
                "momentum": mom_score,
                "execution": exec_score,
                "squeeze": sq_score,
                "strength": rs_score,
                "technical": tech_score,
                "participation": part_score,
                "social": soc_score,
                "short_pct": sq_data.get("short_pct", 0),
                "float_M": round(sq_data.get("float", 0) / 1e6, 1),
                "days_to_cover": round(sq_data.get("days_to_cover", 0), 1),
                "atr_pct": round(atr_pct, 2),
                "dollar_vol_M": round(avg_vol * price / 1e6, 1),
                "market_cap_B": round(market_cap / 1e9, 2),
                "days_to_earnings": days_to_earnings if days_to_earnings is not None else "—",
                "tags": tags,
            })

        except Exception:
            continue

    # Diagnostic Report
    print(f"\n{'=' * 70}")
    print(f"  DIAGNOSTIC REPORT")
    print(f"{'=' * 70}\n")
    print(f"  Market Regime:        {regime['label']}")
    print(f"  Universe size:        {len(universe)}")
    print(f"  Filtered by quality:  {quality_filtered}")
    print(f"    - Earnings blocked: {earnings_blocked}")
    print(f"    - Earnings reactions: {earnings_reaction_count}")
    print(f"  Successfully scored:  {total_processed}")
    
    print(f"\n  HARD REJECT BREAKDOWN:")
    for reason, count in sorted(quality_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason:<25} {count:>4}")
    
    print(f"\n  EXTENSION RISK BREAKDOWN:")
    for cat, count in extension_categories.items():
        pct = (count / total_processed * 100) if total_processed > 0 else 0
        print(f"    {cat:<15} {count:>4} ({pct:.1f}%)")
    
    print(f"\n  LAYER HIT RATES (Target: 40-70% for selective layers):")
    layer_max = {"catalyst": 15, "momentum": 20, "execution": 20, "squeeze": 8,
                 "strength": 15, "technical": 12, "participation": 10, "social": 10}
    print(f"  {'Layer':<14} {'Hits':>6} {'Hit%':>7} {'AvgPts':>8} {'Max':>5}/{'Possible':<8}")
    print(f"  {'-' * 60}")
    for layer_name, stats in layer_stats.items():
        hits = stats["hits"]
        hit_pct = (hits / total_processed * 100) if total_processed > 0 else 0
        avg_pts = (stats["total_pts"] / total_processed) if total_processed > 0 else 0
        max_seen = stats["max_seen"]
        max_p = layer_max.get(layer_name, 0)
        
        # Flag if too permissive or too dead
        flag = ""
        if hit_pct > 80:
            flag = " ⚠️ TOO LOOSE"
        elif hit_pct < 10:
            flag = " ⚠️ DEAD"
        
        print(f"  {layer_name:<14} {hits:>6} {hit_pct:>6.1f}% {avg_pts:>7.1f} {max_seen:>4}/{max_p:<7}{flag}")
    
    print(f"\n  SCORE DISTRIBUTION:")
    for bucket, count in score_distribution.items():
        bar = "█" * int(count / max(1, total_processed) * 40)
        print(f"  {bucket:<8}: {count:>4}  {bar}")
    
    print(f"{'=' * 70}\n")

    # Sort by base score first
    results = sorted(results, key=lambda x: x["score"], reverse=True)
    
    # =================================================================
    # ALPACA SIP ENRICHMENT (Real-time intraday data)
    # =================================================================
    print(f"\n[Stage 6] Enriching top candidates with Alpaca SIP real-time data...\n")
    
    # Debug: Check if API keys are available
    import os
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    
    if alpaca_key:
        print(f"  ✓ ALPACA_API_KEY found (length: {len(alpaca_key)})")
    else:
        print(f"  ✗ ALPACA_API_KEY not found in environment")
    
    if alpaca_secret:
        print(f"  ✓ ALPACA_SECRET_KEY found (length: {len(alpaca_secret)})")
    else:
        print(f"  ✗ ALPACA_SECRET_KEY not found in environment")
    
    try:
        from alpaca_feed import AlpacaFeed
        print(f"  ✓ alpaca_feed module imported successfully")
        
        # Only enrich top 100 to save API calls
        top_100_symbols = [r["symbol"] for r in results[:100]]
        print(f"  ✓ Preparing to enrich top {len(top_100_symbols)} symbols")
        
        alpaca = AlpacaFeed()
        print(f"  ✓ AlpacaFeed initialized")
        
        intraday_data = alpaca.get_intraday_data(top_100_symbols)
        
        # Enrich results with intraday metrics
        enriched_count = 0
        for stock in results:
            if stock["symbol"] in intraday_data:
                rt = intraday_data[stock["symbol"]]
                
                # Add intraday score (0-20 points)
                intraday_score, intraday_reasons = alpaca.score_intraday_position(rt)
                
                # Update final score
                stock["score"] = min(100, stock["score"] + intraday_score)
                stock["intraday_score"] = intraday_score
                
                # Add intraday fields.
                # IMPORTANT: displayed card price must use the same latest
                # intraday bar source as VWAP/HOD when Alpaca data exists.
                stock["price"] = rt["last_price"]
                stock["intraday_last_price"] = rt["last_price"]
                stock["price_source"] = "Alpaca SIP"
                stock["price_updated_at"] = rt.get("last_bar_time", "")

                stock["vwap"] = rt["vwap"]
                stock["vwap_dist_pct"] = rt["vwap_dist_pct"]
                stock["above_vwap"] = rt["above_vwap"]
                stock["hod"] = rt["hod"]
                stock["lod"] = rt["lod"]
                stock["from_hod_pct"] = rt["from_hod_pct"]
                stock["near_hod"] = rt["near_hod"]
                stock["intraday_volume"] = rt["intraday_volume"]
                stock["data_source"] = "Yahoo + Alpaca SIP"
                
                # Add intraday reasons to tags
                if intraday_reasons:
                    current_tags = stock["tags"].split(" · ")
                    combined = intraday_reasons + current_tags
                    stock["tags"] = " · ".join(combined[:7])
                
                enriched_count += 1
        
        print(f"  ✓ Enriched {enriched_count} stocks with SIP real-time data")
        print(f"  ⚠️ Note: SIP consolidated market data enabled")
        
        # Re-sort with intraday scores
        results = sorted(results, key=lambda x: x["score"], reverse=True)
        
        # -------------------------------------------------------------
        # Stage 6A: Early VWAP/EMA reclaim lane.
        # This runs after normal Alpaca enrichment is initialized, but it
        # uses a broader candidate pool and is not limited to the Yahoo/top-100
        # score path. Qualified symbols are force-included as Potential Movers.
        # -------------------------------------------------------------
        early_rows = build_early_reclaim_rows(universe, results)
        results, early_upgraded_count, early_added_count = apply_early_reclaim_to_results(results, early_rows)
        if early_rows:
            results = sorted(results, key=lambda x: x["score"], reverse=True)
            print(f"  ✓ Early reclaim lane merged: upgraded={early_upgraded_count}, added={early_added_count}")

    except ImportError:
        print(f"  ⚠️ Alpaca not available - skipping real-time enrichment")
        print(f"  Install: pip install alpaca-py")
    except Exception as e:
        print(f"  ⚠️ Alpaca enrichment failed: {e}")
        print(f"  Continuing with Yahoo data only...")
        # =================================================================
    # STAGE 7: ALPACA NEWS CATALYST ENRICHMENT
    # =================================================================
    print(f"\n[Stage 7] Enriching top candidates with Alpaca News catalysts...\n")

    try:
        from alpaca_news import AlpacaNews
        news = AlpacaNews()

        # News only on top 100 scored names to keep scanner fast.
        top_news_symbols = set([r["symbol"] for r in results[:100]])

        news_candidates = [
            stock for stock in results
            if stock.get("symbol") in top_news_symbols
        ]

        news.enrich_stocks_with_news(news_candidates, lookback_hours=24)

        # Apply conservative catalyst score overlay.
        for stock in results:
            catalyst_score = stock.get("catalyst_score", 0) or 0

            # Positive catalysts help, but only modestly.
            if catalyst_score > 0:
                stock["score"] = min(100, stock["score"] + int(catalyst_score * 0.5))

            # Negative catalysts/risk hurt strongly.
            elif catalyst_score < 0:
                stock["score"] = max(0, stock["score"] + catalyst_score)

                # Risk names should not remain clean potential movers.
                if catalyst_score <= -10:
                    stock["risk_category"] = "NEWS_RISK"

        print("  ✓ Catalyst overlay applied")

        # Re-sort after news catalyst overlay.
        results = sorted(results, key=lambda x: x["score"], reverse=True)

    except ImportError as e:
        print(f"  ⚠️ Alpaca News file not available: {e}")
        print(f"  Make sure alpaca_news.py exists in the repo root.")
        print("  Continuing without news catalysts...")

    except Exception as e:
        print(f"  ⚠️ Alpaca News enrichment failed: {e}")
        print("  Continuing without news catalysts...")
    # Create DataFrame after enrichment
    # Recalculate tier and setup bucket after Alpaca enrichment
    for stock in results:
        stock["tier"] = assign_tier(stock.get("score", 0))
        stock["setup_bucket"] = classify_setup_bucket(stock)

    df = pd.DataFrame(results)

    if df.empty:
        print("  No stocks passed scoring threshold.")
        pd.DataFrame().to_csv("elite_watchlist_raw.csv", index=False)
        pd.DataFrame().to_csv("elite_watchlist.csv", index=False)
        pd.DataFrame().to_csv("potential_movers.csv", index=False)
        pd.DataFrame().to_csv("active_momentum.csv", index=False)
        pd.DataFrame().to_csv("extended_movers.csv", index=False)
        pd.DataFrame().to_csv("high_risk_movers.csv", index=False)

        with open("elite_watchlist.json", "w") as f:
            f.write("[]")

        with open("market_regime.json", "w") as f:
            json.dump(regime, f, indent=2)

        write_scanner_meta({
            "raw_count": 0,
            "potential_count": 0,
            "active_count": 0,
            "extended_count": 0,
            "highrisk_count": 0,
            "alpaca_enriched_count": int(enriched_count) if "enriched_count" in locals() else 0,
            "early_reclaim_count": int(len(early_rows)) if "early_rows" in locals() else 0,
            "early_reclaim_upgraded": int(early_upgraded_count) if "early_upgraded_count" in locals() else 0,
            "early_reclaim_added": int(early_added_count) if "early_added_count" in locals() else 0,
        })

        return

    # Bucketed watchlists
    potential_df = df[df["setup_bucket"] == "POTENTIAL_MOVER"].sort_values("score", ascending=False)
    active_df = df[df["setup_bucket"] == "ACTIVE_MOMENTUM"].sort_values("score", ascending=False)
    extended_df = df[df["setup_bucket"] == "EXTENDED_CHASE_RISK"].sort_values("score", ascending=False)
    highrisk_df = df[df["setup_bucket"] == "HIGH_RISK_EXTREME"].sort_values("score", ascending=False)
    monitor_df = df[df["setup_bucket"] == "MONITOR"].sort_values("score", ascending=False)

    # Main active watchlist should prioritize potential movers first
    active_watchlist = pd.concat([
    potential_df.head(8),
    active_df.head(2)
]).head(10)

    print(f"\n{'=' * 70}")
    print(f"  RAW SCORED UNIVERSE: {len(df)} names")
    print(f"  POTENTIAL MOVERS: {len(potential_df)}")
    print(f"  ACTIVE MOMENTUM: {len(active_df)}")
    print(f"  EXTENDED / CHASE RISK: {len(extended_df)}")
    print(f"  HIGH RISK / EXTREME: {len(highrisk_df)}")
    print(f"  ACTIVE WATCHLIST: Top {len(active_watchlist)}")
    print(f"{'=' * 70}\n")

    display_cols = [
        "tier",
        "symbol",
        "price",
        "change_pct",
        "gap_pct",
        "score",
        "setup_bucket",
        "risk_category",
        "tags"
    ]

    print("\n--- POTENTIAL MOVERS ---")
    if not potential_df.empty:
        print(potential_df[display_cols].head(12).to_string(index=False))
    else:
        print("No clean potential movers found.")

    print("\n--- ACTIVE MOMENTUM ---")
    if not active_df.empty:
        print(active_df[display_cols].head(8).to_string(index=False))
    else:
        print("No active momentum names found.")

    print("\n--- EXTENDED / CHASE RISK ---")
    if not extended_df.empty:
        print(extended_df[display_cols].head(8).to_string(index=False))
    else:
        print("No extended names.")

    print("\n--- HIGH RISK / EXTREME ---")
    if not highrisk_df.empty:
        print(highrisk_df[display_cols].head(8).to_string(index=False))
    else:
        print("No high-risk extreme movers.")

    # Save files
    df.to_csv("elite_watchlist_raw.csv", index=False)
    active_watchlist.to_csv("elite_watchlist.csv", index=False)
    active_watchlist.to_json("elite_watchlist.json", orient="records", indent=2)

    potential_df.head(12).to_csv("potential_movers.csv", index=False)
    active_df.head(8).to_csv("active_momentum.csv", index=False)
    extended_df.head(10).to_csv("extended_movers.csv", index=False)
    highrisk_df.head(10).to_csv("high_risk_movers.csv", index=False)

    with open("market_regime.json", "w") as f:
        json.dump(regime, f, indent=2)

    write_scanner_meta({
        "raw_count": int(len(df)),
        "potential_count": int(len(potential_df)),
        "active_count": int(len(active_df)),
        "extended_count": int(len(extended_df)),
        "highrisk_count": int(len(highrisk_df)),
        "alpaca_enriched_count": int(enriched_count) if "enriched_count" in locals() else 0,
        "early_reclaim_count": int(len(early_rows)) if "early_rows" in locals() else 0,
        "early_reclaim_upgraded": int(early_upgraded_count) if "early_upgraded_count" in locals() else 0,
        "early_reclaim_added": int(early_added_count) if "early_added_count" in locals() else 0,
    })

    print(f"\n  Saved: elite_watchlist_raw.csv ({len(df)} stocks - full diagnostic)")
    print(f"  Saved: elite_watchlist.csv (bucketed active watchlist)")
    print(f"  Saved: elite_watchlist.json")
    print(f"  Saved: potential_movers.csv")
    print(f"  Saved: active_momentum.csv")
    print(f"  Saved: extended_movers.csv")
    print(f"  Saved: high_risk_movers.csv")
    print(f"  Saved: market_regime.json")
    print(f"  Saved: scanner_meta.json")
    print(f"\n{'=' * 70}\n")


if __name__ == "__main__":
    main()
