"""
ELITE SCANNER DASHBOARD — PRO DESK VERSION
==========================================
Reads scanner outputs and builds dashboard.html.

Inputs:
  - potential_movers.csv
  - active_momentum.csv
  - extended_movers.csv
  - high_risk_movers.csv
  - elite_watchlist.csv
  - elite_watchlist_raw.csv
  - elite_watchlist.json
  - market_regime.json

Display:
  - Price top-right
  - Smaller score badge
  - Company name under ticker
  - Real sector / ETF / vs-sector context
  - SIP sector rotation display panel
  - Catalyst strip
  - Meaning-based tag colors
  - Last scan time and market status
  - Dashboard hides Extended / High Risk sections from the main decision screen
  - Full-width Signal Desk replaces KPI summary row
  - Signal Desk collapses when no live signals exist
  - Potential Movers loads 12; Early Reclaim Runners loads 12; Active Momentum loads 8
  - Dedicated Early Reclaim Runners section reads early_reclaim_runner=True from elite_watchlist_raw.csv
  - WATCH rows are compact in Signal Desk to reduce height
  - Ticker sections are hidden outside regular market OPEN status
"""

import json
import os
import re
import html
from datetime import datetime, timezone
from string import Template

import pandas as pd

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None



ETF_SECTOR_LABELS = {
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    "IWM": "Small Caps",
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
    "KBE": "Banks",
    "KRE": "Regional Banks",
    "ITA": "Aerospace & Defense",
    "ARKK": "Innovation/Growth",
}

def etf_sector_label(etf):
    code = safe_str(etf, "").upper()
    return ETF_SECTOR_LABELS.get(code, code or "Unknown Sector")

# Backward-compatible name used by older rendering code.
# It now returns the sector/industry label, not the long ETF fund name.
def etf_full_name(etf):
    return etf_sector_label(etf)

def etf_label(etf):
    code = safe_str(etf, "").upper()
    name = etf_sector_label(code)
    return f"{code} ({name})" if code else name

def sector_rotation_context(row, regime=None):
    regime = regime or {}
    etf = safe_str(row.get("sector_etf"), "SPY").upper()
    sector_change = safe_float(row.get("sector_change_pct"), 0)
    sector_vs_spy = safe_float(row.get("sector_vs_spy_pct"), None)
    if sector_vs_spy is None:
        sector_vs_spy = sector_change - safe_float(regime.get("spy_change"), 0)

    sector_vs_qqq = safe_float(row.get("sector_vs_qqq_pct"), None)
    if sector_vs_qqq is None:
        sector_vs_qqq = sector_change - safe_float(regime.get("qqq_change"), 0)

    stock_vs_sector = safe_float(row.get("stock_vs_sector_pct"), 0)
    status = safe_str(row.get("sector_status"), "UNKNOWN").upper()

    score = (
        0.60 * sector_change
        + 0.95 * sector_vs_spy
        + 0.45 * sector_vs_qqq
        + 0.18 * stock_vs_sector
    )
    if status in {"LEADING", "IMPROVING"}:
        score += 0.35
    elif status in {"WEAK", "ROTATION_OUT"}:
        score -= 0.55

    if score >= 1.25 and sector_change > 0:
        label = "Strong"
        class_name = "rotation-strong"
    elif score >= 0.45 and sector_change >= 0:
        label = "Supportive"
        class_name = "rotation-supportive"
    elif score <= -0.85 or (sector_change < -0.35 and sector_vs_spy < -0.25):
        label = "Weak"
        class_name = "rotation-weak"
    elif score <= -0.25:
        label = "Soft"
        class_name = "rotation-soft"
    else:
        label = "Neutral"
        class_name = "rotation-neutral"

    return {
        "etf": etf,
        "etf_name": etf_full_name(etf),
        "sector_change": sector_change,
        "sector_vs_spy": sector_vs_spy,
        "sector_vs_qqq": sector_vs_qqq,
        "stock_vs_sector": stock_vs_sector,
        "score": score,
        "label": label,
        "class": class_name,
    }


# ==============================================================
# SAFE HELPERS
# ==============================================================

def safe_str(value, default=""):
    if value is None:
        return default
    try:
        if isinstance(value, float) and pd.isna(value):
            return default
    except Exception:
        pass
    text = str(value)
    if text.lower() in ["nan", "none", "nat"]:
        return default
    return text


def safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        if isinstance(value, str) and value.strip() in ["", "—", "nan", "None"]:
            return default
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return default


def esc(value):
    return html.escape(safe_str(value))


def truthy(value):
    if isinstance(value, bool):
        return value
    text = safe_str(value).lower()
    return text in ["true", "1", "yes", "y"]


def load_csv_records(path, limit=None):
    if not os.path.exists(path):
        return []

    try:
        df = pd.read_csv(path)
        df = df.fillna("")
        if limit:
            df = df.head(limit)
        return df.to_dict("records")
    except Exception as e:
        print(f"  ⚠ Failed to load {path}: {e}")
        return []


def load_json_records(path):
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        print(f"  ⚠ Failed to load {path}: {e}")
        return []


def load_regime():
    if not os.path.exists("market_regime.json"):
        return None

    try:
        with open("market_regime.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_macro_calendar():
    """
    Optional standalone macro-event risk file generated by macro_calendar.py.
    Does not affect scanner scoring.
    """
    if not os.path.exists("macro_calendar.json"):
        return None

    try:
        with open("macro_calendar.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_json_object(path, default=None):
    """Load a JSON object safely. Returns default when missing/invalid."""
    if default is None:
        default = {}

    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def load_scanner_meta():
    """
    scanner_meta.json is written only by elite_scanner.py.
    It is the true broad-scanner data timestamp, not dashboard rebuild time.
    """
    return load_json_object("scanner_meta.json", {})


def parse_et_datetime(value):
    """
    Parse a timestamp-like value and normalize it to America/New_York when possible.
    Supports ISO strings with timezone, Zulu timestamps, and common dashboard strings.
    """
    text = safe_str(value, "").strip()
    if not text:
        return None

    cleaned = (
        text.replace("Z", "+00:00")
            .replace(" ET", "")
            .replace(" EDT", "")
            .replace(" EST", "")
            .strip()
    )

    # ISO first: 2026-05-14T13:31:00-04:00 or 2026-05-14 13:31:00
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo and ZoneInfo:
            dt = dt.astimezone(ZoneInfo("America/New_York"))
        return dt
    except Exception:
        pass

    # Common fallback: 2026-05-14 13:31
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T]+(\d{1,2}):(\d{2})", text)
    if m:
        try:
            return datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5))
            )
        except Exception:
            return None

    return None


def full_datetime_et_label(value, fallback="—"):
    """
    Display dashboard dates as:
      dd-mm-yyyy at hh:mm ET
    """
    dt = parse_et_datetime(value)
    if dt:
        return dt.strftime("%d-%m-%Y at %H:%M ET")

    text = safe_str(value, "").strip()
    if not text:
        return fallback

    # Convert plain YYYY-MM-DD HH:MM strings if parsing failed.
    m = re.search(r"(\d{4})-(\d{2})-(\d{2}).*?(\d{1,2}):(\d{2})", text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)} at {int(m.group(4)):02d}:{m.group(5)} ET"

    return text


def header_time_label(value, fallback="—"):
    """Convert stored ISO timestamps into the standard header ET label."""
    return full_datetime_et_label(value, fallback)


def build_live_market_map(signal_payload):
    """
    During signal_refresh.yml, signal_engine.py can have fresher Alpaca bar data
    than the scanner CSV files. Use that data to update displayed card
    price/VWAP/HOD without pretending the broad scanner was rerun.
    """
    live = {}

    if not isinstance(signal_payload, dict):
        return live

    rows = []
    rows.extend(signal_payload.get("rejected_candidates", []) or [])
    rows.extend(signal_payload.get("signals", []) or [])

    generated_at = safe_str(signal_payload.get("generated_at_et"), "")
    feed = safe_str(signal_payload.get("alpaca_feed"), "SIP").upper()

    for row in rows:
        if not isinstance(row, dict):
            continue

        symbol = safe_str(row.get("symbol"), "").upper()
        if not symbol:
            continue

        price = safe_float(row.get("price"), 0)
        if price <= 0:
            continue

        hod_dist = safe_float(
            row.get("hod_distance_pct"),
            safe_float(row.get("from_hod_pct"), 0)
        )

        price_source = (
            safe_str(row.get("price_source"), "")
            or f"Alpaca {feed}"
        )
        price_updated_at = (
            safe_str(row.get("price_updated_at"), "")
            or safe_str(row.get("quote_time"), "")
            or safe_str(row.get("trade_time"), "")
            or safe_str(row.get("latest_bar_time"), "")
        )

        live[symbol] = {
            "price": price,
            "intraday_last_price": price,
            "price_source": price_source,
            "price_updated_at": price_updated_at,
            "live_price_overlay": True,
            "vwap": safe_float(row.get("vwap"), 0),
            "vwap_dist_pct": safe_float(
                row.get("vwap_dist_pct"),
                safe_float(row.get("vwap_distance_pct"), 0)
            ),
            "above_vwap": row.get("above_vwap"),
            "hod": safe_float(row.get("hod"), 0),
            "from_hod_pct": hod_dist,
            "hod_distance_pct": hod_dist,
            "near_hod": hod_dist >= -1.0,
        }

    return live


def apply_live_market_overlay(rows, live_market_map):
    """
    Return copied dashboard rows with latest Signal Desk market data applied.
    Scanner ranking and bucket membership remain unchanged.
    """
    updated = []

    for row in rows:
        item = dict(row)
        symbol = safe_str(item.get("symbol"), "").upper()
        live = live_market_map.get(symbol)

        if live:
            for key, value in live.items():
                if value is None or value == "":
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
                    continue
                item[key] = value

        updated.append(item)

    return updated



# ==============================================================
# DISPLAY LOGIC
# ==============================================================

def get_tier_color(tier):
    tier = safe_str(tier)
    return {
        "S": "#fbbf24",
        "1": "#10b981",
        "2": "#38bdf8",
        "3": "#94a3b8",
    }.get(tier, "#64748b")


def get_bucket_meta(bucket):
    bucket = safe_str(bucket)

    mapping = {
        "POTENTIAL_MOVER": {
            "label": "Potential Mover",
            "class": "bucket-potential",
            "accent": "#38bdf8",
            "interpretation": "Clean continuation candidate; confirm VWAP hold and breakout structure.",
        },
        "ACTIVE_MOMENTUM": {
            "label": "Active Momentum",
            "class": "bucket-active",
            "accent": "#22c55e",
            "interpretation": "Already moving; wait for pullback or tight consolidation.",
        },
        "EXTENDED_CHASE_RISK": {
            "label": "Extended / Chase Risk",
            "class": "bucket-extended",
            "accent": "#f59e0b",
            "interpretation": "Extended move; avoid chasing unless structure becomes exceptional.",
        },
        "HIGH_RISK_EXTREME": {
            "label": "High Risk / Extreme",
            "class": "bucket-risk",
            "accent": "#ef4444",
            "interpretation": "High-risk mover; monitor only unless deliberately trading momentum.",
        },
        "MONITOR": {
            "label": "Monitor",
            "class": "bucket-monitor",
            "accent": "#94a3b8",
            "interpretation": "Watchlist candidate; not primary until structure improves.",
        },
    }

    return mapping.get(bucket, mapping["MONITOR"])


def get_sector_status_class(status):
    status = safe_str(status, "UNKNOWN").upper()
    if status == "LEADING":
        return "sector-leading"
    if status == "IMPROVING":
        return "sector-improving"
    if status == "WEAK":
        return "sector-weak"
    if status == "UNKNOWN":
        return "sector-unknown"
    return "sector-neutral"


def get_catalyst_meta(stock):
    catalyst_label = safe_str(stock.get("catalyst_label"), "No confirmed fresh news")
    catalyst_sentiment = safe_str(stock.get("catalyst_sentiment"), "NONE").upper()
    catalyst_headline = safe_str(stock.get("catalyst_headline"), "")
    catalyst_source = safe_str(stock.get("catalyst_source"), "")
    catalyst_time = safe_str(stock.get("catalyst_time"), "")
    risk_flags = safe_str(stock.get("risk_flags"), "")

    if catalyst_sentiment == "POSITIVE":
        catalyst_class = "catalyst-positive"
        icon = "🟢"
    elif catalyst_sentiment == "NEGATIVE":
        catalyst_class = "catalyst-negative"
        icon = "🔴"
    elif catalyst_sentiment == "NEUTRAL":
        catalyst_class = "catalyst-neutral"
        icon = "📰"
    else:
        catalyst_class = "catalyst-none"
        icon = "⚪"
        catalyst_label = catalyst_label or "No confirmed fresh news"

    return {
        "label": catalyst_label,
        "sentiment": catalyst_sentiment,
        "headline": catalyst_headline,
        "source": catalyst_source,
        "time": catalyst_time,
        "risk_flags": risk_flags,
        "class": catalyst_class,
        "icon": icon,
    }


def format_money_m(value):
    val = safe_float(value, 0)
    if val >= 1000:
        return f"${val / 1000:.1f}B"
    if val > 0:
        return f"${val:.0f}M"
    return "—"



def get_hod_distance_pct(stock):
    """
    Returns distance from high of day as a negative/zero percentage.
    Example:
      0.0  = at HOD
      -0.4 = 0.4% below HOD
      -2.5 = 2.5% below HOD
    """
    for key in ["hod_distance_pct", "from_hod_pct", "distance_from_hod_pct"]:
        val = stock.get(key)
        if val not in [None, "", "—"]:
            return safe_float(val, None)

    price = safe_float(stock.get("price"), 0)

    hod = (
        safe_float(stock.get("high_of_day"), 0)
        or safe_float(stock.get("day_high"), 0)
        or safe_float(stock.get("intraday_high"), 0)
        or safe_float(stock.get("hod"), 0)
    )

    if price > 0 and hod > 0:
        return round(((price - hod) / hod) * 100, 2)

    return None


def metric_class_liquidity(dollar_vol_m):
    """
    Dollar volume thresholds.
    Green = excellent, blue = good, gray = usable, orange = caution.
    """
    val = safe_float(dollar_vol_m, 0)

    if val >= 100:
        return "metric-good"
    if val >= 50:
        return "metric-ok"
    if val >= 25:
        return "metric-neutral"
    return "metric-caution"


def metric_class_atr(atr_pct):
    """
    ATR sweet spot for day-trade watchlist.
    """
    val = safe_float(atr_pct, 0)

    if 2.0 <= val <= 6.5:
        return "metric-good"
    if 6.5 < val <= 8.0:
        return "metric-caution"
    if val > 8.0:
        return "metric-risk"
    return "metric-neutral"


def metric_class_vwap(vwap_dist_pct):
    """
    VWAP distance color:
    0 to +3% = constructive
    +3 to +5% = getting extended
    > +5% or below VWAP = caution/risk
    """
    val = safe_float(vwap_dist_pct, 0)

    if 0 <= val <= 3:
        return "metric-good"
    if 3 < val <= 5:
        return "metric-caution"
    if val > 5 or val < 0:
        return "metric-risk"
    return "metric-neutral"


def metric_class_hod(hod_distance_pct):
    """
    HOD distance color:
    0 to -0.75% = close to high
    -0.75 to -1.5% = acceptable
    -1.5 to -3% = fading/caution
    worse than -3% = weak vs HOD
    """
    if hod_distance_pct is None:
        return "metric-neutral"

    val = safe_float(hod_distance_pct, -999)

    if val >= -0.75:
        return "metric-good"
    if val >= -1.5:
        return "metric-ok"
    if val >= -3.0:
        return "metric-caution"
    return "metric-risk"


def format_hod_distance(stock):
    """
    Returns text + status for HOD distance.
    """
    hod_distance_pct = get_hod_distance_pct(stock)

    if hod_distance_pct is None:
        near_hod = truthy(stock.get("near_hod"))
        if near_hod:
            return "0.0%", "Near HOD", "metric-good", "status-positive"
        return "—", "HOD N/A", "metric-neutral", "status-neutral"

    metric_text = f"{hod_distance_pct:+.1f}%"

    if hod_distance_pct >= -0.75:
        status_text = "Near HOD"
        status_cls = "status-positive"
    else:
        status_text = f"HOD {hod_distance_pct:+.1f}%"
        status_cls = "status-neutral"

    return metric_text, status_text, metric_class_hod(hod_distance_pct), status_cls

def get_tag_class(tag):
    """
    Meaning-based tag classes.

    Final color rules:
    - Blue   = VWAP / core technical location
    - Green  = constructive strength / continuation / sector support
    - Orange = caution / extension / chase risk
    - Purple = squeeze / volume / short-interest mechanics
    - Red    = hard risk / negative news
    - Gray   = neutral context
    """
    t = safe_str(tag).lower()

    risk_words = [
        "high risk", "news_risk", "offering", "downgrade", "investigation",
        "bankruptcy", "delisting", "reverse split", "dilution", "lawsuit",
        "negative", "misses", "cuts guidance", "guidance cut", "sec risk",
        "risk catalyst", "weak", "failed"
    ]
    if any(w in t for w in risk_words):
        return "tag-risk"

    caution_words = [
        "extended", "major move", "big move", "far above vwap",
        "above vwap extended", "gap", "high atr", "volatile",
        "chase", "extreme", "lower-price"
    ]
    if any(w in t for w in caution_words):
        return "tag-caution"

    squeeze_words = [
        "si ", "short", "dtc", "days to cover", "float", "squeeze",
        "rvol", "vol surge", "volume surge", "mentions"
    ]
    if any(w in t for w in squeeze_words):
        return "tag-squeeze"

    positive_words = [
        "sector leading", "sector supportive", "vs sector", "rs strong",
        "rs positive", "positive catalyst", "upgrade", "record revenue",
        "beats", "raises guidance", "accumulating", "near 52wh",
        "upper range", "near hod", "tight consolidation", "consolidating",
        "breakout", "clean continuation"
    ]
    if any(w in t for w in positive_words):
        return "tag-positive"

    tech_words = [
        "above vwap", "ema stack", "near 20d high", "vwap hold"
    ]
    if any(w in t for w in tech_words):
        return "tag-tech"

    return "tag-neutral"


def build_tags(stock, max_tags=5):
    """
    Build visible tags with category diversity.
    This prevents every card from showing only blue technical tags.
    """
    raw_tags = safe_str(stock.get("tags"), "")
    parts = [p.strip() for p in raw_tags.split(" · ") if p.strip()]

    # Add synthetic, useful context tags so sector/catalyst/risk can be visible.
    sector_status = safe_str(stock.get("sector_status"), "").upper()
    stock_vs_sector = safe_float(stock.get("stock_vs_sector_pct"), 0)
    catalyst_sentiment = safe_str(stock.get("catalyst_sentiment"), "").upper()
    risk_category = safe_str(stock.get("risk_category"), "")

    synthetic = []

    if sector_status == "LEADING":
        synthetic.append("Sector leading")
    elif sector_status == "IMPROVING":
        synthetic.append("Sector supportive")
    elif sector_status == "WEAK":
        synthetic.append("Sector weak")

    if stock_vs_sector >= 1:
        synthetic.append(f"Vs sector +{stock_vs_sector:.1f}%")
    elif stock_vs_sector <= -1:
        synthetic.append(f"Vs sector {stock_vs_sector:.1f}%")

    if catalyst_sentiment == "POSITIVE":
        synthetic.append("Positive catalyst")
    elif catalyst_sentiment == "NEGATIVE":
        synthetic.append("Negative catalyst")

    if risk_category and risk_category not in ["NORMAL", ""]:
        synthetic.append(risk_category.replace("_", " "))

    # Keep order but avoid duplicates.
    seen = set()
    combined = []
    for tag in synthetic + parts:
        key = safe_str(tag).lower()
        if key and key not in seen:
            seen.add(key)
            combined.append(tag)

    # Category-diverse selection.
    buckets = {
        "tag-risk": [],
        "tag-caution": [],
        "tag-positive": [],
        "tag-squeeze": [],
        "tag-tech": [],
        "tag-neutral": [],
    }

    for tag in combined:
        buckets[get_tag_class(tag)].append(tag)

    selected = []

    # Priority: risk/caution first, then positives, then one or two technicals, then squeeze/neutral.
    priority = [
        ("tag-risk", 1),
        ("tag-caution", 1),
        ("tag-positive", 2),
        ("tag-tech", 2),
        ("tag-squeeze", 1),
        ("tag-neutral", 1),
    ]

    for cls, limit in priority:
        for tag in buckets[cls][:limit]:
            if len(selected) < max_tags:
                selected.append((tag, cls))

    # Fill remaining slots from original order if needed.
    if len(selected) < max_tags:
        selected_keys = {safe_str(t[0]).lower() for t in selected}
        for tag in combined:
            if len(selected) >= max_tags:
                break
            key = safe_str(tag).lower()
            if key not in selected_keys:
                selected.append((tag, get_tag_class(tag)))
                selected_keys.add(key)

    html_parts = []
    for tag, tag_class in selected[:max_tags]:
        html_parts.append(f'<span class="tag {tag_class}">{esc(tag)}</span>')

    return "".join(html_parts)


def status_chip(label, cls="status-neutral"):
    return f'<span class="status-chip {cls}">{esc(label)}</span>'



def html_id_for_symbol(symbol):
    """
    Stable in-page anchor for ticker cards.
    Example: AVPT -> card-AVPT
    """
    sym = safe_str(symbol, "").upper()
    sym = re.sub(r"[^A-Z0-9_-]+", "-", sym).strip("-")
    return f"card-{sym}" if sym else "card-UNKNOWN"


def normalize_signal_status(status):
    return safe_str(status, "WAIT").upper().replace(" ", "_")


def signal_time_value(signal):
    """
    Pick the most relevant timestamp for a signal state.
    """
    status = normalize_signal_status(signal.get("signal_status"))

    if status in ["ACTIVE_SIGNAL", "ACTIVE"]:
        keys = ["triggered_at", "trigger_time", "signal_triggered_at", "activated_at", "timestamp"]
    elif status in ["TRIGGER_READY", "TRIGGER_TOUCHED", "READY"]:
        keys = ["ready_since", "ready_at", "detected_at", "timestamp"]
    elif status in ["INVALIDATED", "VOID"]:
        keys = ["invalidated_at", "invalid_time", "last_checked", "timestamp"]
    else:
        keys = ["detected_at", "watch_since", "timestamp", "last_checked"]

    for key in keys:
        val = safe_str(signal.get(key), "")
        if val:
            return val

    return ""


def compact_time_et(value):
    """
    Convert a timestamp-like value to compact ET display where possible.
    Accepts:
      - "2026-05-09T10:18:00-04:00"
      - "2026-05-09 10:18 ET"
      - "10:18"
    Falls back safely.
    """
    text = safe_str(value, "").strip()
    if not text:
        return ""

    # Already compact time.
    if re.match(r"^\d{1,2}:\d{2}", text):
        return text[:5] + " ET"

    # Try ISO or common datetime formats.
    cleaned = text.replace("Z", "+00:00").replace(" ET", "")
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo and ZoneInfo:
            dt = dt.astimezone(ZoneInfo("America/New_York"))
        return dt.strftime("%H:%M ET")
    except Exception:
        pass

    # Fallback: find HH:MM inside string.
    m = re.search(r"(\d{1,2}:\d{2})", text)
    if m:
        return m.group(1) + " ET"

    return text[:16]


def signal_time_label(signal):
    status = normalize_signal_status(signal.get("signal_status"))
    time_text = compact_time_et(signal_time_value(signal))

    if not time_text:
        return ""

    if status in ["ACTIVE_SIGNAL", "ACTIVE"]:
        return f"Triggered {time_text}"
    if status in ["TRIGGER_READY", "TRIGGER_TOUCHED", "READY"]:
        return f"Ready since {time_text}"
    if status in ["INVALIDATED", "VOID"]:
        return f"Invalidated {time_text}"
    if status in ["WATCH", "WATCHLIST"]:
        return f"Detected {time_text}"

    return f"Updated {time_text}"


def build_signal_map(signals):
    signal_map = {}

    for signal in signals:
        symbol = safe_str(signal.get("symbol"), "").upper()
        if not symbol:
            continue

        # Prefer the most actionable/latest state if duplicates appear.
        existing = signal_map.get(symbol)
        if not existing:
            signal_map[symbol] = signal
            continue

        rank = {
            "ACTIVE_SIGNAL": 5,
            "ACTIVE": 5,
            "TRIGGER_READY": 4,
            "READY": 4,
            "WATCH": 3,
            "WATCHLIST": 3,
            "INVALIDATED": 2,
            "VOID": 2,
            "WAIT": 1,
        }

        old_rank = rank.get(normalize_signal_status(existing.get("signal_status")), 0)
        new_rank = rank.get(normalize_signal_status(signal.get("signal_status")), 0)

        if new_rank >= old_rank:
            signal_map[symbol] = signal

    return signal_map


def build_signal_detail_html(signal):
    """
    Signal detail shown inside the ticker card.

    Display rules:
      - WATCH: monitoring context only. No entry/stop/targets/R:R/invalidation.
      - TRIGGER_READY / ACTIVE_SIGNAL: full trade plan.
      - INVALIDATED: show why it failed.
    """
    if not signal:
        return ""

    status = normalize_signal_status(signal.get("signal_status"))
    lunch_caution = truthy(signal.get("lunch_caution")) or safe_str(signal.get("actionability"), "").upper() == "LUNCH_CAUTION"
    status_text = (
        "TRIGGER READY — LUNCH CAUTION"
        if lunch_caution and status in ["TRIGGER_READY", "TRIGGER_TOUCHED", "READY"]
        else "TRIGGER TOUCHED — CONFIRMING"
        if status == "TRIGGER_TOUCHED"
        else status.replace("_", " ")
    )
    status_class = "signal-lunch" if lunch_caution and status in ["TRIGGER_READY", "TRIGGER_TOUCHED", "READY"] else get_signal_status_class(status)

    setup_type = safe_str(signal.get("setup_label") or signal.get("setup_type"), "Setup pending")
    time_label = signal_time_label(signal)
    confidence = safe_float(signal.get("confidence"), 0)

    reason = safe_str(signal.get("reason") or signal.get("trigger_reason") or signal.get("setup_reason"), "")
    invalidation_reason = safe_str(signal.get("invalidation_reason"), "")
    invalidation = safe_str(signal.get("invalidation"), "")
    last_checked = compact_time_et(signal.get("last_checked") or signal.get("updated_at"))

    confidence_text = f"{confidence:.0f}%" if confidence > 0 else "—"

    time_html = f'<span>{esc(time_label)}</span>' if time_label else ""
    last_checked_html = f'<span>Last check {esc(last_checked)}</span>' if last_checked else ""
    confidence_html = f'<span>Confidence {esc(confidence_text)}</span>'

    reason_html = f'<div class="signal-reason"><strong>Reason:</strong> {esc(reason)}</div>' if reason else ""
    event_warning = safe_str(signal.get("event_risk_warning"), "")
    lunch_warning = safe_str(signal.get("entry_warning"), "") if lunch_caution else ""
    warning_text = event_warning or lunch_warning
    warning_label = "Lunch Caution" if lunch_caution else "Event Risk"
    warning_class = "signal-reason event-warning lunch-warning" if lunch_caution else "signal-reason event-warning"
    event_warning_html = (
        f'<div class="{warning_class}"><strong>{esc(warning_label)}:</strong> {esc(warning_text)}</div>'
        if warning_text else ""
    )

    # WATCH is not a trade setup yet. Keep it intentionally minimal so it does
    # not look actionable.
    if status in ["WATCH", "WATCHLIST"]:
        return f"""
        <div class="signal-detail">
            <div class="signal-detail-top">
                <div>
                    <strong>Signal Detail</strong>
                    <span>{esc(setup_type)}</span>
                </div>
                <span class="signal-status {status_class}">{esc(status_text)}</span>
            </div>

            <div class="signal-detail-meta">
                {time_html}
                {last_checked_html}
                {confidence_html}
            </div>

            {event_warning_html}
            {reason_html}
        </div>
        """

    # INVALIDATED should explain why it failed. No active trade plan is needed.
    if status in ["INVALIDATED", "VOID"]:
        failed_reason = invalidation_reason or reason or invalidation
        failed_html = (
            f'<div class="signal-reason"><strong>Why invalidated:</strong> {esc(failed_reason)}</div>'
            if failed_reason else ""
        )

        return f"""
        <div class="signal-detail">
            <div class="signal-detail-top">
                <div>
                    <strong>Signal Detail</strong>
                    <span>{esc(setup_type)}</span>
                </div>
                <span class="signal-status {status_class}">{esc(status_text)}</span>
            </div>

            <div class="signal-detail-meta">
                {time_html}
                {last_checked_html}
                {confidence_html}
            </div>

            {event_warning_html}
            {failed_html}
        </div>
        """

    # TRIGGER_READY / ACTIVE_SIGNAL get the full execution plan.
    entry = format_signal_price(signal.get("entry_trigger") or signal.get("entry"))
    stop = format_signal_price(signal.get("stop_loss") or signal.get("stop"))
    target_1 = format_signal_price(signal.get("target_1") or signal.get("target1"))
    target_2 = format_signal_price(signal.get("target_2") or signal.get("target2"))
    rr = safe_float(signal.get("reward_risk"), 0)

    rr_text = f"{rr:.1f}:1" if rr > 0 else "—"

    invalidation_html = f'<div class="signal-reason"><strong>Invalidation:</strong> {esc(invalidation)}</div>' if invalidation else ""

    return f"""
        <div class="signal-detail">
            <div class="signal-detail-top">
                <div>
                    <strong>Signal Detail</strong>
                    <span>{esc(setup_type)}</span>
                </div>
                <span class="signal-status {status_class}">{esc(status_text)}</span>
            </div>

            <div class="signal-detail-meta">
                {time_html}
                {last_checked_html}
            </div>

            <div class="signal-plan-grid">
                <div><span>Entry</span><b>{entry}</b></div>
                <div><span>Stop</span><b>{stop}</b></div>
                <div><span>T1</span><b>{target_1}</b></div>
                <div><span>T2</span><b>{target_2}</b></div>
                <div><span>R/R</span><b>{rr_text}</b></div>
                <div><span>Conf.</span><b>{confidence_text}</b></div>
            </div>

            {event_warning_html}
            {reason_html}
            {invalidation_html}
        </div>
    """

def build_card(stock, signal=None):
    symbol = safe_str(stock.get("symbol"), "—").upper()
    card_id = html_id_for_symbol(symbol)

    tier = safe_str(stock.get("tier"), "—")
    score = safe_int(stock.get("score"), 0)
    price = safe_float(stock.get("price"), 0)
    change_pct = safe_float(stock.get("change_pct"), 0)

    price_source = safe_str(stock.get("price_source") or stock.get("data_source") or "Scanner")
    price_time = compact_time_et(stock.get("price_updated_at"))
    price_meta_html = ""
    if price_time:
        price_meta_html = f'<div class="price-meta">{esc(price_source)} · {esc(price_time)}</div>'

    bucket = safe_str(stock.get("setup_bucket"), "MONITOR")
    risk = safe_str(stock.get("risk_category"), "NORMAL")

    bucket_meta = get_bucket_meta(bucket)
    catalyst = get_catalyst_meta(stock)

    company_name = safe_str(stock.get("company_name"), symbol)
    sector = safe_str(stock.get("sector"), "Unknown")
    sector_etf = safe_str(stock.get("sector_etf"), "SPY")
    sector_etf_full = etf_full_name(sector_etf)
    sector_status = safe_str(stock.get("sector_status"), "UNKNOWN").upper()
    sector_status_class = get_sector_status_class(sector_status)
    sector_change = safe_float(stock.get("sector_change_pct"), 0)
    stock_vs_sector = safe_float(stock.get("stock_vs_sector_pct"), 0)
    rotation_ctx = sector_rotation_context(stock)

    tier_color = get_tier_color(tier)
    change_class = "positive" if change_pct >= 0 else "negative"
    change_sign = "+" if change_pct >= 0 else ""

    dollar_vol_m = safe_float(stock.get("dollar_vol_M"), 0)
    dollar_vol = format_money_m(dollar_vol_m)
    atr = safe_float(stock.get("atr_pct"), 0)
    vwap_dist = safe_float(stock.get("vwap_dist_pct"), 0)
    short_pct = safe_float(stock.get("short_pct"), 0)
    float_m = safe_float(stock.get("float_M"), 0)
    days_to_cover = safe_float(stock.get("days_to_cover"), 0)

    above_vwap = truthy(stock.get("above_vwap"))

    vwap_text = "Above VWAP" if above_vwap else "Below/Unknown"
    hod_metric_text, hod_status_text, hod_metric_class, hod_status_class = format_hod_distance(stock)

    liq_metric_class = metric_class_liquidity(dollar_vol_m)
    atr_metric_class = metric_class_atr(atr)
    vwap_metric_class = metric_class_vwap(vwap_dist)

    vwap_cls = "status-tech" if above_vwap else "status-neutral"
    risk_cls = "status-risk" if risk not in ["NORMAL", "", "—"] else "status-neutral"

    tags_html = build_tags(stock)
    signal_detail_html = build_signal_detail_html(signal)

    headline = catalyst["headline"]
    headline_html = ""
    if headline:
        headline_html = f'<div class="catalyst-headline">{esc(headline[:150])}</div>'

    risk_flags_html = ""
    if catalyst["risk_flags"]:
        risk_flags_html = f'<div class="risk-flags">Risk flags: {esc(catalyst["risk_flags"])}</div>'

    catalyst_source_line = ""
    if catalyst["source"] or catalyst["time"]:
        catalyst_source_line = (
            f'<div class="catalyst-source">'
            f'{esc(catalyst["source"])}'
            f'{(" · " + esc(catalyst["time"][:19])) if catalyst["time"] else ""}'
            f'</div>'
        )

    squeeze_html = ""
    if short_pct >= 15 or days_to_cover >= 4:
        squeeze_html = f"""
        <div class="mini-panel">
            <div class="mini-row"><span>Short %</span><strong>{short_pct:.0f}%</strong></div>
            <div class="mini-row"><span>Float</span><strong>{float_m:.1f}M</strong></div>
            <div class="mini-row"><span>DTC</span><strong>{days_to_cover:.1f}d</strong></div>
        </div>
        """

    return f"""
    <div class="stock-card {bucket_meta['class']}" id="{esc(card_id)}" style="--accent:{bucket_meta['accent']};">
        <div class="card-top">
            <div class="card-id">
                <div class="symbol-row">
                    <span class="symbol">{esc(symbol)}</span>
                    <span class="sector-chip">{esc(sector)}</span>
                    <span class="tier" style="color:{tier_color};border-color:{tier_color};">Tier {esc(tier)}</span>
                </div>
                <div class="company-name">{esc(company_name)}</div>
            </div>
            <div class="price-box">
                <div class="price">${price:.2f}</div>
                {price_meta_html}
                <div class="change {change_class}">{change_sign}{change_pct:.2f}%</div>
            </div>
        </div>

        <div class="score-risk-row">
            <span class="score-pill">Score {score}/100</span>
            <span class="risk-pill">{esc(risk)}</span>
            <span class="sector-status-pill {sector_status_class}">{esc(sector_status)}</span>
        </div>

        <div class="sector-strip">
            <span>Sector <strong>{esc(sector)}</strong></span>
            <span>ETF <strong title="{esc(sector_etf_full)}">{esc(sector_etf)} ({esc(sector_etf_full)})</strong> <b class="{'positive' if sector_change >= 0 else 'negative'}">{sector_change:+.2f}%</b></span>
            <span>Vs Sector <b class="{'positive' if stock_vs_sector >= 0 else 'negative'}">{stock_vs_sector:+.2f}%</b></span>
            <span>Rotation <b class="{rotation_ctx['class']}">{esc(rotation_ctx['label'])}</b></span>
        </div>

        <div class="catalyst-strip {catalyst['class']}">
            <div class="catalyst-label">{catalyst['icon']} Catalyst: {esc(catalyst['label'])}</div>
            {headline_html}
            {catalyst_source_line}
            {risk_flags_html}
        </div>

        <div class="metrics-grid">
            <div><span>Liquidity</span><strong class="{liq_metric_class}">{dollar_vol}</strong></div>
            <div><span>ATR</span><strong class="{atr_metric_class}">{atr:.1f}%</strong></div>
            <div><span>VWAP Dist</span><strong class="{vwap_metric_class}">{vwap_dist:+.1f}%</strong></div>
            <div><span>HOD Dist</span><strong class="{hod_metric_class}">{esc(hod_metric_text)}</strong></div>
        </div>

        <div class="status-row">
            {status_chip(vwap_text, vwap_cls)}
            {status_chip(hod_status_text, hod_status_class)}
            {status_chip(risk, risk_cls)}
        </div>

        <div class="interpretation">{esc(bucket_meta['interpretation'])}</div>

        <div class="tags-row">{tags_html}</div>

        {signal_detail_html}

        {squeeze_html}

        <div class="card-actions">
            <a class="action-btn action-chart" href="https://www.tradingview.com/chart/?symbol={esc(symbol)}" target="_blank">
                <img src="assets/tradingview.png" alt="TradingView"> Chart
            </a>
            <a class="action-btn action-yahoo" href="https://finance.yahoo.com/quote/{esc(symbol)}" target="_blank">
                <img src="assets/yahoo.png" alt="Yahoo Finance"> Yahoo
            </a>
            <a class="action-btn action-twits" href="https://stocktwits.com/symbol/{esc(symbol)}" target="_blank">
                <img src="assets/stocktwits.png" alt="Stocktwits"> Twits
            </a>
        </div>
    </div>
    """

def format_compact_count(value):
    val = safe_float(value, 0)
    if val >= 1_000_000:
        return f"{val / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if val >= 1_000:
        return f"{val / 1_000:.0f}K"
    if val > 0:
        return f"{val:.0f}"
    return "—"


def format_money_raw(value):
    val = safe_float(value, 0)
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.2f}".rstrip("0").rstrip(".") + "B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if val >= 1_000:
        return f"${val / 1_000:.0f}K"
    if val > 0:
        return f"${val:.0f}"
    return "—"


def format_price_dash(value):
    val = safe_float(value, 0)
    if val > 0:
        return f"${val:.2f}" if val >= 1 else f"${val:.4f}"
    return "—"


def build_monitor_tags(stock, max_tags=6):
    raw_tags = safe_str(stock.get("tags"), "")
    parts = [p.strip() for p in raw_tags.split(" · ") if p.strip()]

    seen = set()
    tags = []
    for tag in parts:
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)

        if "monitor" in key:
            cls = "tag-neutral"
        elif "after-hours" in key or "pre-market" in key or "premarket" in key:
            cls = "tag-positive"
        elif "spread" in key or "gap" in key:
            cls = "tag-caution"
        elif "volume" in key or "notional" in key:
            cls = "tag-tech"
        else:
            cls = "tag-neutral"

        tags.append(f'<span class="tag {cls}">{esc(tag)}</span>')
        if len(tags) >= max_tags:
            break

    return "".join(tags)


def build_monitor_mover_card(stock):
    """
    Dedicated monitor-only card for pre-market / after-hours movers.

    This intentionally hides regular-session fields such as ATR, VWAP distance,
    HOD distance, sector "Unknown" strips, catalyst panels, and regular setup
    language. Extended-hours movers are not Signal Desk candidates.
    """
    symbol = safe_str(stock.get("symbol"), "—").upper()
    card_id = html_id_for_symbol(symbol)

    session_raw = safe_str(stock.get("monitor_session"), "").upper()
    if "PRE" in session_raw:
        session_label = "Pre-Market"
        move_label = "Pre-Market Move"
        anchor_default = "Previous Close"
        accent = "#38bdf8"
    else:
        session_label = "After-Hours"
        move_label = "After-Hours Move"
        anchor_default = "16:00 Close"
        accent = "#f59e0b"

    company_name = safe_str(stock.get("company_name"), symbol)
    exchange = safe_str(stock.get("exchange"), "")
    score = safe_int(stock.get("score"), 0)
    tier = safe_str(stock.get("tier"), "—")

    price = safe_float(stock.get("extended_latest_price") or stock.get("price"), 0)
    change_pct = safe_float(stock.get("extended_change_pct") or stock.get("change_pct"), 0)
    change_class = "positive" if change_pct >= 0 else "negative"
    change_sign = "+" if change_pct >= 0 else ""

    price_source = safe_str(stock.get("price_source") or "Alpaca SIP Extended")
    price_time = compact_time_et(stock.get("price_updated_at"))
    price_meta_html = f'<div class="price-meta">{esc(price_source)}{(" · " + esc(price_time)) if price_time else ""}</div>'

    anchor_label = safe_str(stock.get("extended_anchor_label"), anchor_default)
    anchor_price = safe_float(stock.get("extended_anchor_price"), 0)
    ext_volume = safe_float(stock.get("extended_volume"), 0)
    ext_notional = safe_float(stock.get("extended_notional"), 0)
    ext_high = safe_float(stock.get("extended_high"), 0)
    ext_low = safe_float(stock.get("extended_low"), 0)
    ext_bars = safe_int(stock.get("extended_bar_count"), 0)
    bid = safe_float(stock.get("bid"), 0)
    ask = safe_float(stock.get("ask"), 0)
    spread = safe_float(stock.get("spread_pct"), 0)

    spread_text = f"{spread:.1f}%" if spread > 0 else "—"
    bid_ask_text = f"{format_price_dash(bid)} / {format_price_dash(ask)}" if bid > 0 or ask > 0 else "—"

    tags_html = build_monitor_tags(stock)
    if not tags_html:
        tags_html = (
            status_chip("MONITOR ONLY", "status-neutral")
            + status_chip(session_label, "status-positive")
        )

    volume_class = "metric-good" if ext_volume >= 100_000 else "metric-ok" if ext_volume >= 10_000 else "metric-neutral"
    notional_class = "metric-good" if ext_notional >= 1_000_000 else "metric-ok" if ext_notional >= 100_000 else "metric-neutral"
    spread_class = "metric-good" if 0 < spread <= 3 else "metric-ok" if 0 < spread <= 8 else "metric-caution" if spread > 0 else "metric-neutral"

    exchange_html = f'<span class="sector-chip">{esc(exchange)}</span>' if exchange else ""

    return f"""
    <div class="stock-card extended-monitor-card" id="{esc(card_id)}" style="--accent:{accent};">
        <div class="card-top">
            <div class="card-id">
                <div class="symbol-row">
                    <span class="symbol">{esc(symbol)}</span>
                    {exchange_html}
                    <span class="tier">Tier {esc(tier)}</span>
                </div>
                <div class="company-name">{esc(company_name)}</div>
            </div>
            <div class="price-box">
                <div class="price">{format_price_dash(price)}</div>
                {price_meta_html}
                <div class="change {change_class}">{change_sign}{change_pct:.2f}%</div>
            </div>
        </div>

        <div class="score-risk-row">
            <span class="score-pill">Score {score}/100</span>
            <span class="risk-pill">MONITOR ONLY</span>
            <span class="sector-status-pill sector-neutral">{esc(session_label)}</span>
        </div>

        <div class="extended-move-strip">
            <div>
                <span>{esc(move_label)}</span>
                <strong class="{change_class}">{change_sign}{change_pct:.2f}%</strong>
            </div>
            <div>
                <span>{esc(anchor_label)}</span>
                <strong>{format_price_dash(anchor_price)}</strong>
            </div>
            <div>
                <span>Latest Extended</span>
                <strong>{format_price_dash(price)}</strong>
            </div>
        </div>

        <div class="metrics-grid extended-metrics">
            <div><span>Ext Vol</span><strong class="{volume_class}">{format_compact_count(ext_volume)}</strong></div>
            <div><span>Ext $</span><strong class="{notional_class}">{format_money_raw(ext_notional)}</strong></div>
            <div><span>Spread</span><strong class="{spread_class}">{esc(spread_text)}</strong></div>
            <div><span>Bars</span><strong class="metric-neutral">{ext_bars if ext_bars > 0 else "—"}</strong></div>
        </div>

        <div class="metrics-grid extended-metrics secondary">
            <div><span>Ext High</span><strong>{format_price_dash(ext_high)}</strong></div>
            <div><span>Ext Low</span><strong>{format_price_dash(ext_low)}</strong></div>
            <div><span>Bid / Ask</span><strong>{esc(bid_ask_text)}</strong></div>
            <div><span>Mode</span><strong>Monitor</strong></div>
        </div>

        <div class="interpretation extended-interpretation">
            Monitor-only extended-hours mover. This does not affect Signal Desk, regular Potential Movers, Active Momentum, or trade execution.
        </div>

        <div class="tags-row">{tags_html}</div>

        <div class="card-actions">
            <a class="action-btn action-chart" href="https://www.tradingview.com/chart/?symbol={esc(symbol)}" target="_blank">
                <img src="assets/tradingview.png" alt="TradingView"> Chart
            </a>
            <a class="action-btn action-yahoo" href="https://finance.yahoo.com/quote/{esc(symbol)}" target="_blank">
                <img src="assets/yahoo.png" alt="Yahoo Finance"> Yahoo
            </a>
            <a class="action-btn action-twits" href="https://stocktwits.com/symbol/{esc(symbol)}" target="_blank">
                <img src="assets/stocktwits.png" alt="Stocktwits"> Twits
            </a>
        </div>
    </div>
    """



def is_early_reclaim_row(stock):
    """
    True when elite_scanner.py marked the symbol as an early VWAP/EMA reclaim runner.
    This is a regular-market discovery lane, not pre-market/after-hours monitor data.
    """
    setup = safe_str(stock.get("intraday_setup_type"), "").upper()
    return truthy(stock.get("early_reclaim_runner")) or setup == "VWAP_EMA_RECLAIM_RUNNER"


def early_reclaim_sort_key(stock):
    early_score = safe_float(stock.get("early_reclaim_score"), 0)
    scanner_score = safe_float(stock.get("score"), 0)
    intraday_score = safe_float(stock.get("intraday_score"), 0)
    volume = safe_float(stock.get("intraday_volume"), 0)
    change_pct = safe_float(stock.get("change_pct"), 0)
    return (early_score, scanner_score, intraday_score, volume, change_pct)


def load_early_reclaim_rows(raw_rows):
    """
    Build the display list for the Early Reclaim Runners section.

    Source:
      elite_watchlist_raw.csv rows already passed into build_dashboard().

    Display rule:
      - Include early_reclaim_runner=True OR intraday_setup_type=VWAP_EMA_RECLAIM_RUNNER.
      - Deduplicate by symbol.
      - Sort by early reclaim score first, then scanner/intraday strength.
    """
    seen = set()
    rows = []

    for row in raw_rows or []:
        symbol = safe_str(row.get("symbol"), "").upper()
        if not symbol or symbol in seen:
            continue
        if not is_early_reclaim_row(row):
            continue
        seen.add(symbol)
        rows.append(row)

    rows.sort(key=early_reclaim_sort_key, reverse=True)
    return rows


def build_early_reclaim_card(stock, signal=None):
    """
    Regular-market Early Reclaim Runner card.

    This section is intentionally separate from Potential Movers and Active Momentum.
    It is not labeled "monitor only"; it is a live scanner lane showing whether
    the early VWAP/EMA reclaim detection path is working.
    """
    symbol = safe_str(stock.get("symbol"), "—").upper()
    card_id = html_id_for_symbol(symbol)

    price = safe_float(stock.get("intraday_last_price") or stock.get("price"), 0)
    change_pct = safe_float(stock.get("change_pct"), 0)
    change_class = "positive" if change_pct >= 0 else "negative"
    change_sign = "+" if change_pct >= 0 else ""

    company_name = safe_str(stock.get("company_name"), symbol)
    sector = safe_str(stock.get("sector"), "Unknown")
    bucket = safe_str(stock.get("setup_bucket"), "MONITOR")
    tier = safe_str(stock.get("tier"), "—")

    scanner_score = safe_int(stock.get("score"), 0)
    early_score = safe_float(stock.get("early_reclaim_score"), 0)
    intraday_score = safe_float(stock.get("intraday_score"), 0)
    intraday_volume = safe_float(stock.get("intraday_volume"), 0)
    vwap_dist = safe_float(stock.get("vwap_dist_pct"), 0)
    setup_type = safe_str(stock.get("intraday_setup_type"), "VWAP_EMA_RECLAIM_RUNNER")
    reason = safe_str(stock.get("early_reclaim_reason"), "")
    quality_label = safe_str(stock.get("vwap_reclaim_quality_label"), "")
    quality_color = safe_str(stock.get("vwap_reclaim_quality_color"), "neutral").lower()
    quality_warning = safe_str(stock.get("vwap_reclaim_quality_warning"), "")
    attempt_count = safe_int(stock.get("vwap_reclaim_attempt_count"), 0)
    failed_count = safe_int(stock.get("vwap_reclaim_failed_count"), 0)
    current_attempt = safe_int(stock.get("vwap_reclaim_current_attempt"), 0)
    bucket_promoted = str(stock.get("early_reclaim_bucket_promoted", "")).strip().lower() in {"true", "1", "yes"}
    quality_class = {
        "green": "metric-good",
        "yellow": "metric-ok",
        "red": "metric-bad",
    }.get(quality_color, "metric-neutral")
    badge_text = quality_label or "VWAP Reclaim"
    badge_style = {
        "green": "background:rgba(34,197,94,.16);color:#22c55e;border-color:rgba(34,197,94,.35);",
        "yellow": "background:rgba(245,158,11,.16);color:#f59e0b;border-color:rgba(245,158,11,.35);",
        "red": "background:rgba(239,68,68,.16);color:#ef4444;border-color:rgba(239,68,68,.35);",
    }.get(quality_color, "background:rgba(148,163,184,.14);color:#94a3b8;border-color:rgba(148,163,184,.28);")
    signal_eligible_text = "Signal Eligible" if bucket_promoted or bucket == "POTENTIAL_MOVER" else "Scanner Lane"
    tags_html = build_tags(stock)

    price_source = safe_str(stock.get("price_source") or stock.get("data_source") or "Alpaca SIP")
    price_time = compact_time_et(stock.get("price_updated_at"))
    price_meta_html = ""
    if price_time:
        price_meta_html = f'<div class="price-meta">{esc(price_source)} · {esc(price_time)}</div>'

    vwap_class = metric_class_vwap(vwap_dist)
    intraday_vol_class = "metric-good" if intraday_volume >= 100_000 else "metric-ok" if intraday_volume >= 10_000 else "metric-neutral"
    early_score_class = "metric-good" if early_score >= 80 else "metric-ok" if early_score >= 65 else "metric-neutral"
    intraday_score_class = "metric-good" if intraday_score >= 80 else "metric-ok" if intraday_score >= 65 else "metric-neutral"

    signal_detail_html = build_signal_detail_html(signal)

    return f"""
    <div class="stock-card early-reclaim-card" id="{esc(card_id)}" style="--accent:#38bdf8;">
        <div class="card-top">
            <div class="card-id">
                <div class="symbol-row">
                    <span class="symbol">{esc(symbol)}</span>
                    <span class="sector-chip">{esc(sector)}</span>
                    <span class="tier">Tier {esc(tier)}</span>
                </div>
                <div class="company-name">{esc(company_name)}</div>
            </div>
            <div class="price-box">
                <div class="price">${price:.2f}</div>
                {price_meta_html}
                <div class="change {change_class}">{change_sign}{change_pct:.2f}%</div>
            </div>
        </div>

        <div class="score-risk-row">
            <span class="score-pill">Scanner {scanner_score}/100</span>
            <span class="risk-pill">Early Reclaim</span>
            <span class="sector-status-pill sector-neutral">{esc(bucket)}</span>
            <span class="sector-status-pill" style="{badge_style}">{esc(badge_text)}</span>
        </div>

        <div class="early-reclaim-strip">
            <div>
                <span>Setup</span>
                <strong>{esc(setup_type)}</strong>
            </div>
            <div>
                <span>Early Score</span>
                <strong class="{early_score_class}">{early_score:.0f}</strong>
            </div>
            <div>
                <span>VWAP Dist</span>
                <strong class="{vwap_class}">{vwap_dist:+.1f}%</strong>
            </div>
        </div>

        <div class="metrics-grid early-reclaim-metrics">
            <div><span>Intraday Vol</span><strong class="{intraday_vol_class}">{format_compact_count(intraday_volume)}</strong></div>
            <div><span>Intraday Score</span><strong class="{intraday_score_class}">{intraday_score:.0f}</strong></div>
            <div><span>VWAP Attempts</span><strong class="{quality_class}">{attempt_count}</strong></div>
            <div><span>Failed Reclaims</span><strong class="{quality_class}">{failed_count}</strong></div>
            <div><span>Current Attempt</span><strong>{current_attempt or "—"}</strong></div>
            <div><span>Engine Path</span><strong>{esc(signal_eligible_text)}</strong></div>
        </div>

        <div class="interpretation early-reclaim-interpretation">
            {esc(reason or "VWAP/EMA reclaim lane candidate. Watch for hold, volume continuation, and clean reclaim structure before considering any trade decision.")}
            {f'<br><strong>{esc(quality_warning)}</strong>' if quality_warning else ''}
        </div>

        <div class="tags-row">{tags_html}</div>

        {signal_detail_html}

        <div class="card-actions">
            <a class="action-btn action-chart" href="https://www.tradingview.com/chart/?symbol={esc(symbol)}" target="_blank">
                <img src="assets/tradingview.png" alt="TradingView"> Chart
            </a>
            <a class="action-btn action-yahoo" href="https://finance.yahoo.com/quote/{esc(symbol)}" target="_blank">
                <img src="assets/yahoo.png" alt="Yahoo Finance"> Yahoo
            </a>
            <a class="action-btn action-twits" href="https://stocktwits.com/symbol/{esc(symbol)}" target="_blank">
                <img src="assets/stocktwits.png" alt="Stocktwits"> Twits
            </a>
        </div>
    </div>
    """


def build_early_reclaim_section(stocks, signal_map=None, max_cards=12):
    signal_map = signal_map or {}
    count = len(stocks)

    cards = "".join(
        build_early_reclaim_card(
            s,
            signal=signal_map.get(safe_str(s.get("symbol"), "").upper())
        )
        for s in stocks[:max_cards]
    )

    if not cards:
        cards = """
        <div class="empty-section compact-empty">
            <strong>No Early Reclaim Runners detected.</strong>
            <span>This section fills when the regular-market scanner detects VWAP/EMA reclaim candidates from the early reclaim lane.</span>
        </div>
        """

    return f"""
    <section class="desk-section section-early-reclaim">
        <div class="section-header">
            <div>
                <h2>Early Reclaim Runners</h2>
                <p>Regular-market VWAP/EMA reclaim lane. Shows up to {max_cards} candidates above Active Momentum with VWAP attempt quality badges.</p>
            </div>
            <span class="section-count">{count}</span>
        </div>
        <div class="cards-grid">
            {cards}
        </div>
    </section>
    """


def build_section(title, subtitle, stocks, class_name, max_cards=10, signal_map=None, collapse_empty=False):
    count = len(stocks)
    signal_map = signal_map or {}

    cards = "".join(
        build_card(s, signal=signal_map.get(safe_str(s.get("symbol"), "").upper()))
        for s in stocks[:max_cards]
    )

    if not cards:
        empty_class = "empty-section compact-empty" if collapse_empty else "empty-section"
        cards = f"""
        <div class="{empty_class}">
            <strong>No names in this bucket.</strong>
            <span>{esc(subtitle)}</span>
        </div>
        """

    return f"""
    <section class="desk-section {class_name}">
        <div class="section-header">
            <div>
                <h2>{esc(title)}</h2>
                <p>{esc(subtitle)}</p>
            </div>
            <span class="section-count">{count}</span>
        </div>
        <div class="cards-grid">
            {cards}
        </div>
    </section>
    """


def macro_display_name(name):
    """
    Compact but readable macro event labels for the dashboard.
    """
    raw = safe_str(name, "").strip()

    aliases = {
        "Consumer Price Index": "Consumer Price Index (CPI)",
        "Producer Price Index": "Producer Price Index (PPI)",
        "Personal Income and Outlays": "Personal Income and Outlays (PCE)",
        "Employment Situation": "Employment Situation (Jobs)",
        "Advance Retail Sales": "Retail Sales",
        "Initial Claims": "Initial Jobless Claims",
        "ISM Manufacturing": "ISM Manufacturing",
        "ISM Services": "ISM Services",
        "Gross Domestic Product": "Gross Domestic Product (GDP)",
        "FOMC Meeting Announcement": "FOMC Rate Decision",
        "FOMC Minutes": "FOMC Minutes",
    }

    return aliases.get(raw, raw)


def macro_event_datetime_label(event):
    """
    Return macro event date/time as:
      dd-mm-yyyy at hh:mm ET
    """
    dt_text = safe_str(event.get("datetime_et"), "")
    if dt_text:
        label = full_datetime_et_label(dt_text, "")
        if label:
            return label

    date_text = safe_str(event.get("date"), "")
    time_text = safe_str(event.get("time_et"), "TBD").replace(" ET", "").strip()

    if date_text:
        try:
            date_dt = datetime.fromisoformat(date_text[:10])
            if time_text and time_text.upper() != "TBD":
                m = re.match(r"^(\d{1,2}):(\d{2})", time_text)
                if m:
                    return f"{date_dt.strftime('%d-%m-%Y')} at {int(m.group(1)):02d}:{m.group(2)} ET"
            return date_dt.strftime("%d-%m-%Y")
        except Exception:
            pass

    if time_text:
        return f"{time_text} ET" if time_text.upper() != "TBD" else "TBD"

    return "TBD"


def format_macro_event_label(event):
    """
    Example:
    Consumer Price Index (CPI): 15-05-2026 at 08:30 ET
    """
    name = macro_display_name(event.get("name") or event.get("event") or "Macro Event")
    when = macro_event_datetime_label(event)
    return f"{name}: {when}"


def build_macro_html(macro):
    if not macro:
        return """
        <section class="macro-banner macro-unknown macro-compact">
            <div class="macro-main compact">
                <div>
                    <strong>Macro Risk: Not checked</strong>
                    <span>No macro_calendar.json found. Run macro_calendar.py before building the dashboard.</span>
                </div>
            </div>
        </section>
        """

    risk_level = safe_str(macro.get("risk_level"), "UNKNOWN").upper()
    risk_class = safe_str(macro.get("risk_class"), "")
    if not risk_class:
        risk_class = {
            "HIGH": "macro-high",
            "MEDIUM": "macro-medium",
            "LOW": "macro-low",
            "UNKNOWN": "macro-unknown",
        }.get(risk_level, "macro-unknown")

    action = safe_str(macro.get("action"), "Check the economic calendar manually before trading.")
    source = safe_str(macro.get("source"), "Unknown source")
    generated = full_datetime_et_label(macro.get("generated_at_et"), "")

    events = [e for e in (macro.get("events", []) or []) if isinstance(e, dict)]
    released_events_today = [e for e in (macro.get("released_events_today", []) or []) if isinstance(e, dict)]
    upcoming_events = [e for e in (macro.get("upcoming_events", []) or []) if isinstance(e, dict)]

    now_utc, now_ny = get_times()

    def event_dt(event):
        dt = parse_et_datetime(event.get("datetime_et"))
        if dt:
            return dt

        date_text = safe_str(event.get("date"), "")
        time_text = safe_str(event.get("time_et"), "").replace(" ET", "").strip()

        if date_text and time_text and time_text.upper() != "TBD":
            dt = parse_et_datetime(f"{date_text} {time_text}")
            if dt:
                return dt

        if date_text:
            try:
                return datetime.fromisoformat(date_text[:10])
            except Exception:
                return None

        return None

    def event_ts(event, default=0.0):
        dt = event_dt(event)
        if not dt:
            return default
        try:
            if dt.tzinfo is None:
                if ZoneInfo:
                    dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return default

    def is_today_event(event):
        dt = event_dt(event)
        if not dt:
            return False
        try:
            if dt.tzinfo and ZoneInfo:
                dt = dt.astimezone(ZoneInfo("America/New_York"))
            return dt.date() == now_ny.date()
        except Exception:
            return False

    def event_release_status(event):
        status = safe_str(event.get("release_status"), "").upper()
        if status == "RELEASED":
            return "RELEASED"

        minutes_until = safe_float(event.get("minutes_until"), None)
        if minutes_until is not None and minutes_until < 0 and is_today_event(event):
            return "RELEASED"

        dt = event_dt(event)
        if dt and is_today_event(event):
            try:
                if dt.tzinfo is None and ZoneInfo:
                    dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
                compare_now = now_ny
                if dt.tzinfo and compare_now.tzinfo:
                    compare_now = compare_now.astimezone(dt.tzinfo)
                if dt <= compare_now:
                    return "RELEASED"
            except Exception:
                pass

        return "UPCOMING"

    # Backward compatibility: if macro_calendar.py does not yet provide separate
    # released/upcoming lists, infer them from the events list.
    if not released_events_today:
        released_events_today = [
            e for e in events
            if is_today_event(e) and event_release_status(e) == "RELEASED"
        ]

    if not upcoming_events:
        upcoming_events = [
            e for e in events
            if event_release_status(e) != "RELEASED"
        ]

    released_events_today = sorted(
        released_events_today,
        key=lambda e: event_ts(e, 0.0),
        reverse=True,
    )

    upcoming_events = sorted(
        upcoming_events,
        key=lambda e: event_ts(e, 9999999999.0),
    )

    def event_impact_class(event):
        impact = safe_str(event.get("impact"), "MEDIUM").upper()
        if impact == "HIGH":
            return "impact-high"
        if impact == "MEDIUM":
            return "impact-medium"
        return "impact-low"

    def render_event_chip(event):
        status = event_release_status(event)
        name = macro_display_name(event.get("name") or event.get("event") or "Macro Event")
        when = macro_event_datetime_label(event)

        if status == "RELEASED":
            return f"""
            <div class="macro-event">
                <span class="impact-pill impact-low">RELEASED</span>
                <strong>{esc(name)}</strong>
                <span class="macro-event-time">{esc(when)}</span>
            </div>
            """

        impact = safe_str(event.get("impact"), "MEDIUM").upper()
        impact_class = event_impact_class(event)

        return f"""
        <div class="macro-event">
            <span class="impact-pill {impact_class}">{esc(impact)}</span>
            <strong>{esc(name)}</strong>
            <span class="macro-event-time">{esc(when)}</span>
        </div>
        """

    primary_label = "Upcoming:"
    primary_name = "No high/medium macro event"
    primary_when = ""
    primary_badge = risk_level
    primary_badge_class = "impact-high" if risk_level == "HIGH" else "impact-medium" if risk_level == "MEDIUM" else "impact-low"

    primary_event = None
    if released_events_today:
        primary_event = released_events_today[0]
        primary_label = "Today:"
        primary_badge = "RELEASED"
        primary_badge_class = "impact-low"
    elif upcoming_events:
        primary_event = upcoming_events[0]
        primary_label = "Today:" if is_today_event(primary_event) else "Upcoming:"
        primary_badge = safe_str(primary_event.get("impact"), risk_level).upper()
        primary_badge_class = event_impact_class(primary_event)

    if isinstance(primary_event, dict):
        primary_name = macro_display_name(primary_event.get("name") or primary_event.get("event") or "Macro Event")
        primary_when = macro_event_datetime_label(primary_event)

    # Compact row: released today first, then upcoming. Do not repeat sentiment.
    event_source = []
    event_source.extend(released_events_today[:3])
    event_source.extend(upcoming_events[:5])

    # De-duplicate by name/date/time.
    deduped = []
    seen = set()
    for event in event_source:
        key = (
            safe_str(event.get("name") or event.get("event"), "").lower(),
            safe_str(event.get("date"), "")[:10],
            safe_str(event.get("time_et"), ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)

    visible_limit = 4
    visible_chips = "".join(render_event_chip(e) for e in deduped[:visible_limit])
    more_count = max(0, len(deduped) - visible_limit)

    more_chip = ""
    if more_count:
        more_chip = f"""
        <div class="macro-event macro-more">
            <span class="impact-pill impact-medium">+{more_count}</span>
            <strong>more events</strong>
        </div>
        """

    event_row = visible_chips + more_chip
    if not event_row:
        event_row = '<div class="macro-event muted">No high/medium macro events in lookahead window.</div>'

    return f"""
    <section class="macro-banner {esc(risk_class)} macro-compact">
        <div class="macro-main compact">
            <div class="macro-primary">
                <div class="macro-title-line">
                    <strong>Macro Risk: {esc(risk_level)}</strong>
                    <span class="impact-pill {primary_badge_class}">{esc(primary_badge)}</span>
                </div>
                <div class="macro-next">
                    <span class="macro-label">{esc(primary_label)}</span>
                    <strong>{esc(primary_name)}</strong>
                    {f'<span class="macro-time">{esc(primary_when)}</span>' if primary_when else ''}
                </div>
                <span class="macro-action">{esc(action)}</span>
            </div>
            <div class="macro-source">
                Source: {esc(source)}{(" · " + esc(generated)) if generated else ""}
            </div>
        </div>
        <div class="macro-events compact">
            {event_row}
        </div>
    </section>
    """


def build_regime_html(regime):
    if not regime:
        return """
        <div class="regime-banner neutral">
            <div>
                <strong>Market Regime: Unknown</strong>
                <span>No market_regime.json found.</span>
            </div>
        </div>
        """

    label = safe_str(regime.get("label"), "Unknown")
    bias = safe_str(regime.get("bias"), "NEUTRAL")
    spy = safe_float(regime.get("spy_change"), 0)
    qqq = safe_float(regime.get("qqq_change"), 0)
    iwm = safe_float(regime.get("iwm_change"), 0)
    vix = safe_float(regime.get("vix_level"), 0)

    bias_class = "bullish" if bias == "LONG_FAVORED" else "bearish" if bias == "SHORT_FAVORED" else "caution" if bias == "CAUTION" else "neutral"

    return f"""
    <div class="regime-banner {bias_class}">
        <div>
            <strong>{esc(label)}</strong>
            <span>Bias: {esc(bias.replace("_", " "))} · Data: Yahoo + Alpaca SIP + Alpaca News</span>
        </div>
        <div class="regime-metrics">
            <span>SPY <b class="{'positive' if spy >= 0 else 'negative'}">{spy:+.2f}%</b></span>
            <span>QQQ <b class="{'positive' if qqq >= 0 else 'negative'}">{qqq:+.2f}%</b></span>
            <span>IWM <b class="{'positive' if iwm >= 0 else 'negative'}">{iwm:+.2f}%</b></span>
            <span>VIX <b>{vix:.1f}</b></span>
        </div>
    </div>
    """


def load_sector_rotation_payload():
    """
    Load standalone sector_rotation.py output.

    Phase A rule:
      - Display only.
      - Does not affect scanner ranking, Signal Desk decisions, or Smart Money.
    """
    return load_json_object("sector_rotation.json", default={})


def sector_rotation_rank_change_html(value):
    change = safe_int(value, 0)
    if change > 0:
        return f'<span class="rank-change rank-up">↑ {change}</span>'
    if change < 0:
        return f'<span class="rank-change rank-down">↓ {abs(change)}</span>'
    return '<span class="rank-change rank-flat">0</span>'


def sector_rotation_label_class(label):
    text = safe_str(label, "").lower()
    if "strong" in text:
        return "rotation-strong"
    if "supportive" in text:
        return "rotation-supportive"
    if "weak" in text or "rotation out" in text:
        return "rotation-weak"
    if "soft" in text or "fading" in text:
        return "rotation-soft"
    return "rotation-neutral"


def sector_rotation_volume_class(status):
    text = safe_str(status, "").lower()
    if "high volume" in text:
        return "volume-confirmed"
    if "low volume" in text:
        return "volume-warning"
    return "volume-normal"


def pct_cell(value, show_sign=True):
    val = safe_float(value, 0)
    cls = "positive" if val >= 0 else "negative"
    sign = "+" if show_sign and val >= 0 else ""
    return f'<span class="{cls}">{sign}{val:.2f}%</span>'


def build_sector_rotation_json_panel(payload):
    """
    Display-only sector rotation panel from sector_rotation.json.

    This panel intentionally does not influence Signal Desk, scanner ranking,
    Smart Money, or any trade decision. It is visual context only.
    """
    if not isinstance(payload, dict) or not payload:
        return """
        <section class="sector-snapshot sector-rotation-panel">
            <div class="section-header">
                <div>
                    <h2>Sector Rotation</h2>
                    <p>Standalone SIP sector rotation has not been generated yet. Run sector_rotation.py during FULL_SCANNER.</p>
                </div>
            </div>
            <div class="empty-section compact-empty">
                <strong>No sector_rotation.json found.</strong>
                <span>Sector rotation display is waiting for the standalone Phase A output file.</span>
            </div>
        </section>
        """

    ranked = payload.get("ranked", []) or []
    benchmarks = payload.get("benchmarks", {}) if isinstance(payload.get("benchmarks"), dict) else {}
    errors = payload.get("errors", []) or []

    generated = full_datetime_et_label(payload.get("generated_at_et"), "")
    feed = safe_str(payload.get("feed"), "sip").upper()
    market_phase = safe_str(payload.get("market_phase"), "UNKNOWN")
    data_source = safe_str(payload.get("data_source"), "Alpaca SIP")

    benchmark_bits = []
    for symbol in ["SPY", "QQQ", "IWM"]:
        row = benchmarks.get(symbol)
        if not isinstance(row, dict):
            continue
        benchmark_bits.append(
            f'<span>{esc(symbol)} {pct_cell(row.get("change_pct"))} '
            f'<em>VWAP {safe_float(row.get("vwap_dist_pct"), 0):+.2f}%</em></span>'
        )
    benchmark_html = "".join(benchmark_bits) or '<span>Benchmarks unavailable</span>'

    error_html = ""
    if errors:
        visible_errors = " | ".join(safe_str(e) for e in errors[:4])
        error_html = f"""
        <div class="sector-rotation-warning">
            <strong>Warnings:</strong> {esc(visible_errors)}
        </div>
        """

    if not ranked:
        return f"""
        <section class="sector-snapshot sector-rotation-panel">
            <div class="section-header">
                <div>
                    <h2>Sector Rotation</h2>
                    <p>Display-only SIP sector context. Feed: {esc(feed)} · Phase: {esc(market_phase)}{(" · " + esc(generated)) if generated else ""}</p>
                </div>
            </div>
            <div class="rotation-benchmarks">{benchmark_html}</div>
            {error_html}
            <div class="empty-section compact-empty">
                <strong>No ranked sector rows available.</strong>
                <span>sector_rotation.json exists, but no sector ETF rows were produced.</span>
            </div>
        </section>
        """

    html_rows = ""

    for row in ranked[:20]:
        symbol = safe_str(row.get("symbol"), "—").upper()
        sector_name = safe_str(row.get("sector_name"), symbol)
        rank = safe_int(row.get("rank"), 0)
        rank_change = sector_rotation_rank_change_html(row.get("rank_change"))
        label = safe_str(row.get("rotation_label"), "Neutral")
        trend = safe_str(row.get("rotation_trend"), "Stable")
        volume_status = safe_str(row.get("volume_status"), "Normal Volume")
        label_cls = sector_rotation_label_class(label)
        trend_cls = sector_rotation_label_class(trend)
        vol_cls = sector_rotation_volume_class(volume_status)

        html_rows += f"""
        <tr>
            <td><strong>#{rank}</strong></td>
            <td>{rank_change}</td>
            <td><strong>{esc(symbol)}</strong><br><small>{esc(sector_name)}</small></td>
            <td>{pct_cell(row.get("change_pct"))}</td>
            <td>{pct_cell(row.get("vs_spy_pct"))}</td>
            <td>{pct_cell(row.get("vs_qqq_pct"))}</td>
            <td>{pct_cell(row.get("change_15m_pct"))}</td>
            <td>{pct_cell(row.get("change_30m_pct"))}</td>
            <td>{pct_cell(row.get("change_60m_pct"))}</td>
            <td>{pct_cell(row.get("vwap_dist_pct"))}</td>
            <td>{pct_cell(row.get("hod_distance_pct"))}</td>
            <td><span class="volume-pill {vol_cls}">{safe_float(row.get("volume_ratio"), 0):.2f}x · {esc(volume_status)}</span></td>
            <td><span class="rotation-pill {label_cls}">{esc(label)}</span></td>
            <td><span class="rotation-pill {trend_cls}">{esc(trend)}</span></td>
        </tr>
        """

    return f"""
    <section class="sector-snapshot sector-rotation-panel">
        <div class="section-header">
            <div>
                <h2>Sector Rotation</h2>
                <p>Display-only SIP sector context. Does not change Smart Money, scanner ranking, or Signal Desk decisions.</p>
            </div>
        </div>

        <div class="sector-rotation-meta">
            <span>Source <strong>{esc(data_source)}</strong></span>
            <span>Feed <strong>{esc(feed)}</strong></span>
            <span>Phase <strong>{esc(market_phase)}</strong></span>
            {f'<span>Generated <strong>{esc(generated)}</strong></span>' if generated else ''}
        </div>

        <div class="rotation-benchmarks">
            {benchmark_html}
        </div>

        {error_html}

        <div class="table-wrap compact-table sector-rotation-table">
            <table>
                <thead>
                    <tr>
                        <th>Rank</th>
                        <th>Change</th>
                        <th>ETF / Sector</th>
                        <th>Session</th>
                        <th>Vs SPY</th>
                        <th>Vs QQQ</th>
                        <th>15m</th>
                        <th>30m</th>
                        <th>60m</th>
                        <th>VWAP</th>
                        <th>HOD Dist</th>
                        <th>Volume</th>
                        <th>Label</th>
                        <th>Trend</th>
                    </tr>
                </thead>
                <tbody>{html_rows}</tbody>
            </table>
        </div>
    </section>
    """

def build_desk_table(rows):
    if not rows:
        return ""

    html_rows = ""

    for stock in rows[:40]:
        symbol = safe_str(stock.get("symbol"), "—").upper()
        score = safe_int(stock.get("score"), 0)
        tier = safe_str(stock.get("tier"), "—")
        bucket = get_bucket_meta(safe_str(stock.get("setup_bucket"), "MONITOR"))["label"]
        price = safe_float(stock.get("price"), 0)
        chg = safe_float(stock.get("change_pct"), 0)
        liq = format_money_m(stock.get("dollar_vol_M"))
        atr = safe_float(stock.get("atr_pct"), 0)
        vwap = "Above" if truthy(stock.get("above_vwap")) else "Below/NA"
        risk = safe_str(stock.get("risk_category"), "NORMAL")
        sector = safe_str(stock.get("sector"), "Unknown")
        sector_status = safe_str(stock.get("sector_status"), "UNKNOWN")
        stock_vs_sector = safe_float(stock.get("stock_vs_sector_pct"), 0)
        catalyst = get_catalyst_meta(stock)
        headline = catalyst["headline"][:85] if catalyst["headline"] else "—"

        html_rows += f"""
        <tr>
            <td><strong>{esc(symbol)}</strong><br><small>{esc(sector)}</small></td>
            <td>{score}</td>
            <td>{esc(tier)}</td>
            <td>{esc(bucket)}</td>
            <td>${price:.2f}</td>
            <td class="{'positive' if chg >= 0 else 'negative'}">{chg:+.2f}%</td>
            <td>{liq}</td>
            <td>{atr:.1f}%</td>
            <td>{esc(vwap)}</td>
            <td>{esc(risk)}</td>
            <td>{esc(sector_status)}<br><small>{stock_vs_sector:+.2f}% vs sector</small></td>
            <td>{esc(catalyst['label'])}</td>
            <td>{esc(headline)}</td>
        </tr>
        """

    return f"""
    <section class="desk-table-section">
        <div class="section-header">
            <div>
                <h2>Table View</h2>
                <p>Compact comparison table for visible decision candidates.</p>
            </div>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Ticker</th>
                        <th>Score</th>
                        <th>Tier</th>
                        <th>Bucket</th>
                        <th>Price</th>
                        <th>% Chg</th>
                        <th>Liq</th>
                        <th>ATR</th>
                        <th>VWAP</th>
                        <th>Risk</th>
                        <th>Sector</th>
                        <th>Catalyst</th>
                        <th>Headline</th>
                    </tr>
                </thead>
                <tbody>{html_rows}</tbody>
            </table>
        </div>
    </section>
    """


def get_times():
    now_utc = datetime.now(timezone.utc)

    if ZoneInfo:
        now_ny = now_utc.astimezone(ZoneInfo("America/New_York"))
    else:
        now_ny = datetime.now()

    return now_utc, now_ny


def get_market_status(now_ny):
    minutes = now_ny.hour * 60 + now_ny.minute
    weekday = now_ny.weekday()

    if weekday >= 5:
        return "CLOSED", "status-gray"

    if minutes < 4 * 60:
        return "CLOSED", "status-gray"
    if minutes < 9 * 60 + 30:
        return "PRE-MARKET", "status-blue"
    if minutes < 16 * 60:
        return "OPEN", "status-green"
    if minutes < 20 * 60:
        return "AFTER-HOURS", "status-yellow"
    return "CLOSED", "status-gray"


def is_regular_market_open(status):
    """
    Dashboard safety gate.

    Only regular market OPEN can display ticker candidates.
    PRE-MARKET, AFTER-HOURS, CLOSED, and weekends must not show ticker cards,
    ticker tables, sector ticker context, or Signal Desk ticker rows.
    """
    return safe_str(status).upper() == "OPEN"


def build_market_inactive_signal_panel(status):
    """
    Signal Desk replacement outside regular market hours.

    This intentionally shows no ticker symbols. It prevents after-hours or
    pre-market scanner output from looking actionable.
    """
    return f"""
    <section class="signal-desk-panel signal-desk-compact" id="signals">
        <div class="signal-desk-top">
            <div>
                <strong>Signal Desk</strong>
                <span>Intraday execution status</span>
            </div>
            <div class="signal-desk-counts">
                <span><b>0</b> Active</span>
                <span><b>0</b> Ready</span>
                <span><b>0</b> Watch</span>
            </div>
        </div>
        <div class="signal-desk-empty">
            <strong>Market is {esc(status)}.</strong>
            <span>Intraday scanner inactive. Ticker candidates are hidden outside regular market hours.</span>
        </div>
    </section>
    """


# ==============================================================
# MORNING SCANNER GUARD
# ==============================================================

FIRST_REGULAR_SCANNER_HOUR = 9
FIRST_REGULAR_SCANNER_MINUTE = 45


def scanner_data_is_regular_session_ready(now_ny, scanner_meta):
    """
    Only allow ticker cards/table/sector context after today's first valid
    regular-market scanner has run at or after 09:45 ET.

    This prevents stale 09:00/09:01 pre-market scanner files from appearing
    after the 09:30 dashboard-only OPEN refresh.
    """
    scanner_dt = parse_et_datetime(scanner_meta.get("scanner_generated_at_et"))

    if scanner_dt is None:
        return False

    try:
        if scanner_dt.date() != now_ny.date():
            return False
    except Exception:
        return False

    scan_minutes = scanner_dt.hour * 60 + scanner_dt.minute
    first_scan_minutes = FIRST_REGULAR_SCANNER_HOUR * 60 + FIRST_REGULAR_SCANNER_MINUTE

    return scan_minutes >= first_scan_minutes


def build_waiting_first_scanner_panel(status, scanner_time_label):
    """
    Signal Desk replacement when market is OPEN but the first valid 09:45+
    regular-market scanner data is not available yet.

    Strict rule:
    - show no ticker symbols
    - show no Signal Desk rows
    - show no old pre-market candidates
    """
    return f"""
    <section class="signal-desk-panel signal-desk-compact" id="signals">
        <div class="signal-desk-top">
            <div>
                <strong>Signal Desk</strong>
                <span>Intraday execution status</span>
            </div>
            <div class="signal-desk-counts">
                <span><b>0</b> Active</span>
                <span><b>0</b> Ready</span>
                <span><b>0</b> Watch</span>
            </div>
        </div>
        <div class="signal-desk-empty">
            <strong>Market is {esc(status)}.</strong>
            <span>Waiting for first regular-market scanner at 09:45 ET. Scanner candidates are hidden until valid 09:45+ scanner data is available. Last scanner data: {esc(scanner_time_label)}</span>
        </div>
    </section>
    """



# ==============================================================
# EXTENDED-HOURS MONITOR-ONLY DISPLAY
# ==============================================================

PREMARKET_DISPLAY_START_HOUR = 7
PREMARKET_DISPLAY_START_MINUTE = 0
PREMARKET_DISPLAY_END_HOUR = 9
PREMARKET_DISPLAY_END_MINUTE = 29

AFTER_HOURS_DISPLAY_START_HOUR = 16
AFTER_HOURS_DISPLAY_START_MINUTE = 0
AFTER_HOURS_DISPLAY_END_HOUR = 20
AFTER_HOURS_DISPLAY_END_MINUTE = 0


def minutes_of_day(dt):
    return dt.hour * 60 + dt.minute


def is_premarket_monitor_window(status, now_ny):
    """
    Pre-market monitor-only window.

    Shows premarket_movers.csv/json from 07:00 through 09:29 ET.
    This is display-only and never promotes names into Signal Desk.
    """
    status = safe_str(status).upper()
    mins = minutes_of_day(now_ny)
    start = PREMARKET_DISPLAY_START_HOUR * 60 + PREMARKET_DISPLAY_START_MINUTE
    end = PREMARKET_DISPLAY_END_HOUR * 60 + PREMARKET_DISPLAY_END_MINUTE
    return status == "PRE-MARKET" and start <= mins <= end


def is_after_hours_monitor_window(status, now_ny):
    """
    After-hours monitor-only window.

    Shows after_hours_movers.csv/json from 16:00 through 20:00 ET.
    This is display-only and never promotes names into Signal Desk.
    """
    status = safe_str(status).upper()
    mins = minutes_of_day(now_ny)
    start = AFTER_HOURS_DISPLAY_START_HOUR * 60 + AFTER_HOURS_DISPLAY_START_MINUTE
    end = AFTER_HOURS_DISPLAY_END_HOUR * 60 + AFTER_HOURS_DISPLAY_END_MINUTE
    return status == "AFTER-HOURS" and start <= mins <= end

def ensure_et_aware(dt, now_ny=None):
    """
    Normalize a datetime to ET-aware when possible.

    Some files store naive ET timestamps. Treat naive values as ET so freshness
    checks do not accidentally pass stale snapshots from another session.
    """
    if not dt:
        return None

    if dt.tzinfo is None:
        if now_ny is not None and getattr(now_ny, "tzinfo", None) is not None:
            return dt.replace(tzinfo=now_ny.tzinfo)
        if ZoneInfo:
            return dt.replace(tzinfo=ZoneInfo("America/New_York"))
        return dt

    if ZoneInfo:
        return dt.astimezone(ZoneInfo("America/New_York"))
    return dt


def file_mtime_et(path):
    """
    Return file modification time as ET-aware datetime when possible.
    """
    try:
        ts = os.path.getmtime(path)
    except Exception:
        return None

    dt_utc = datetime.fromtimestamp(ts, timezone.utc)
    if ZoneInfo:
        return dt_utc.astimezone(ZoneInfo("America/New_York"))
    return dt_utc


def json_monitor_generated_time_et(json_path, now_ny=None):
    """
    Extract a generated/as-of time from monitor mover JSON if available.

    extended_hours_movers.py has used object wrappers such as:
      {"metadata": {...}, "symbols": [...], "movers": [...]}

    If no usable timestamp exists, caller should fall back to file mtime.
    """
    if not json_path or not os.path.exists(json_path):
        return None

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    candidates = []

    for key in (
        "generated_at_et",
        "generated_at",
        "created_at_et",
        "created_at",
        "asof_et",
        "as_of_et",
        "snapshot_time_et",
        "snapshot_time",
        "latest_extended_time",
    ):
        if data.get(key):
            candidates.append(data.get(key))

    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        for key in (
            "generated_at_et",
            "generated_at",
            "created_at_et",
            "created_at",
            "asof_et",
            "as_of_et",
            "snapshot_time_et",
            "snapshot_time",
            "latest_extended_time",
        ):
            if metadata.get(key):
                candidates.append(metadata.get(key))

    for value in candidates:
        dt = parse_et_datetime(value)
        dt = ensure_et_aware(dt, now_ny)
        if dt:
            return dt

    return None


def monitor_scan_timestamp_label(csv_path, json_path, now_ny, fallback="waiting"):
    """
    Header timestamp for extended-hours monitor files.

    During PRE-MARKET and AFTER-HOURS the dashboard should not display the
    stale regular scanner timestamp. It should display the timestamp from the
    monitor-only scan file instead:
      - premarket_movers.json/csv during PRE-MARKET
      - after_hours_movers.json/csv during AFTER-HOURS

    Priority:
      1. JSON generated/as-of timestamp.
      2. Newest JSON/CSV file modification time.
      3. Fallback text.
    """
    times = []

    generated = json_monitor_generated_time_et(json_path, now_ny)
    if generated:
        times.append(generated)

    csv_mtime = file_mtime_et(csv_path)
    if csv_mtime:
        times.append(csv_mtime)

    json_mtime = file_mtime_et(json_path) if json_path else None
    if json_mtime:
        times.append(json_mtime)

    if not times:
        return fallback

    newest = max(ensure_et_aware(dt, now_ny) for dt in times if dt)
    if not newest:
        return fallback

    return newest.strftime("%d-%m-%Y at %H:%M ET")


def build_header_scan_time_label(status, now_ny, scanner_meta):
    """
    Build the first header scan-time pill.

    Regular market:
      Scanner Data: <regular scanner time>

    Premarket:
      Premarket Scan: <premarket_movers timestamp>

    After-hours:
      After-Hours Scan: <after_hours_movers timestamp>

    This prevents stale regular scanner timestamps, such as 15:31 ET, from being
    shown as the primary scan time while the dashboard is displaying premarket
    or after-hours monitor-only movers.
    """
    status_norm = safe_str(status).upper()

    if status_norm == "PRE-MARKET":
        ts = monitor_scan_timestamp_label(
            "premarket_movers.csv",
            "premarket_movers.json",
            now_ny,
            fallback="waiting",
        )
        return f"Premarket Scan: {ts}"

    if status_norm == "AFTER-HOURS":
        ts = monitor_scan_timestamp_label(
            "after_hours_movers.csv",
            "after_hours_movers.json",
            now_ny,
            fallback="waiting",
        )
        return f"After-Hours Scan: {ts}"

    return f"Scanner Data: {header_time_label(scanner_meta.get('scanner_generated_at_et'))}"


def monitor_snapshot_fresh_for_afterhours(csv_path, json_path, now_ny):
    """
    Guard against stale after-hours files during the 16:00-16:15 transition.

    Problem fixed:
    - At 16:00 ET the dashboard market state becomes AFTER-HOURS.
    - Before the first fresh after-hours scan, old after_hours_movers files can
      still exist from a prior after-hours run.
    - Without this guard, stale cards can appear until the 16:15 scan replaces them.

    Rule:
    - During AFTER-HOURS, only display after_hours_movers when the freshest CSV/JSON
      timestamp is from the current ET date and at/after today's 16:00 ET.
    - Otherwise show a waiting message and hide old rows.
    """
    if not now_ny:
        return False, "Current ET time unavailable."

    session_start = now_ny.replace(
        hour=AFTER_HOURS_DISPLAY_START_HOUR,
        minute=AFTER_HOURS_DISPLAY_START_MINUTE,
        second=0,
        microsecond=0,
    )

    times = []

    csv_mtime = file_mtime_et(csv_path)
    if csv_mtime:
        times.append(csv_mtime)

    json_mtime = file_mtime_et(json_path) if json_path else None
    if json_mtime:
        times.append(json_mtime)

    json_generated = json_monitor_generated_time_et(json_path, now_ny)
    if json_generated:
        times.append(json_generated)

    if not times:
        return False, "Waiting for first same-day after-hours scan."

    newest = max(times)
    newest = ensure_et_aware(newest, now_ny)

    # Same ET date + generated/modified after 16:00 ET.
    if newest.date() != now_ny.date():
        return False, f"Waiting for today's after-hours scan; latest snapshot is {newest.strftime('%Y-%m-%d %H:%M ET')}."

    if newest < session_start:
        return False, f"Waiting for fresh after-hours scan after 16:00 ET; latest snapshot is {newest.strftime('%H:%M ET')}."

    # Defensive future-time guard: if system clock / metadata is badly ahead,
    # do not display impossible after-hours rows.
    if newest > now_ny.replace(second=59, microsecond=999999):
        return False, f"After-hours snapshot timestamp is ahead of current ET time: {newest.strftime('%H:%M ET')}."

    return True, f"Fresh after-hours snapshot: {newest.strftime('%H:%M ET')}."


def build_monitor_snapshot_waiting_section(title, subtitle, reason, class_name):
    """
    Explicit waiting section used when extended-hours files are stale or missing.
    This avoids showing stale cards during session transitions.
    """
    return f"""
    <section class="desk-section {esc(class_name)}">
        <div class="section-header">
            <div>
                <h2>{esc(title)}</h2>
                <p>{esc(subtitle)}</p>
            </div>
            <span class="section-count">0</span>
        </div>
        <div class="empty-section compact-empty">
            <strong>Waiting for fresh after-hours scan.</strong>
            <span>{esc(reason)}</span>
        </div>
    </section>
    """


def load_monitor_mover_records(csv_path, json_path=None, limit=12):
    """
    Load monitor-only pre-market / after-hours mover snapshots.

    Runner writes these files after extended-hours scanner snapshots:
      - premarket_movers.csv/json
      - after_hours_movers.csv/json

    CSV is preferred because it preserves the same card schema as the scanner.
    JSON fallback supports either a list or common object wrappers.
    """
    rows = load_csv_records(csv_path, limit=limit)
    if rows:
        return rows

    if not json_path or not os.path.exists(json_path):
        return []

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = (
                data.get("movers")
                or data.get("rows")
                or data.get("symbols")
                or data.get("data")
                or []
            )
        else:
            rows = []

        rows = rows if isinstance(rows, list) else []
        clean_rows = [r for r in rows if isinstance(r, dict)]
        return clean_rows[:limit]
    except Exception as e:
        print(f"  ⚠ Failed to load monitor movers {json_path}: {e}")
        return []


def build_monitor_only_signal_panel(status, title, subtitle):
    """
    Signal Desk shell for pre-market / after-hours monitor windows.

    Keeps execution logic visibly disabled while allowing the separate
    monitor-only mover section to appear below.
    """
    return f"""
    <section class="signal-desk-panel signal-desk-compact" id="signals">
        <div class="signal-desk-top">
            <div>
                <strong>Signal Desk Disabled</strong>
                <span>{esc(title)}</span>
            </div>
            <div class="signal-desk-counts">
                <span><b>0</b> Active</span>
                <span><b>0</b> Ready</span>
                <span><b>0</b> Watch</span>
            </div>
        </div>
        <div class="signal-desk-empty">
            <strong>Market is {esc(status)}.</strong>
            <span>{esc(subtitle)}</span>
        </div>
    </section>
    """


def build_monitor_movers_section(title, subtitle, stocks, class_name, max_cards=12):
    """
    Monitor-only mover section for pre-market and after-hours snapshots.

    These cards are ranked/replaced inside their own section only.
    They do not become Potential Movers, Active Momentum, Trigger Ready,
    or Active Signal candidates from dashboard display.
    """
    count = len(stocks)
    cards = "".join(build_monitor_mover_card(s) for s in stocks[:max_cards])

    if not cards:
        cards = f"""
        <div class="empty-section compact-empty">
            <strong>No monitor-only movers yet.</strong>
            <span>{esc(subtitle)}</span>
        </div>
        """

    return f"""
    <section class="desk-section {esc(class_name)}">
        <div class="section-header">
            <div>
                <h2>{esc(title)}</h2>
                <p>{esc(subtitle)}</p>
            </div>
            <span class="section-count">{count}</span>
        </div>
        <div class="signal-desk-empty" style="margin-bottom:14px;">
            <strong>Monitor Only</strong>
            <span>These names are ranked/replaced only inside this extended-hours section. They do not affect Signal Desk, regular Potential Movers, Active Momentum, or trade execution.</span>
        </div>
        <div class="cards-grid">
            {cards}
        </div>
    </section>
    """


# ==============================================================
# SIGNAL DESK SHELL
# ==============================================================

def load_signal_payload(path="signal_desk.json"):
    """
    Load the full Signal Desk payload.

    Current signal_engine.py can write:
      {
        "signals": [...],
        "rejected_candidates": [...],
        "counts": {...}
      }

    Older formats are also supported for backward compatibility.
    """
    if not os.path.exists(path):
        return {"signals": [], "rejected_candidates": [], "counts": {}}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return {"signals": data, "rejected_candidates": [], "counts": {}}

        if isinstance(data, dict):
            signals = data.get("signals", [])
            rejected = data.get("rejected_candidates", [])
            counts = data.get("counts", {})

            return {
                "signals": signals if isinstance(signals, list) else [],
                "rejected_candidates": rejected if isinstance(rejected, list) else [],
                "counts": counts if isinstance(counts, dict) else {},
                "market_phase": data.get("market_phase", ""),
                "generated_at_et": data.get("generated_at_et", ""),
            }

        return {"signals": [], "rejected_candidates": [], "counts": {}}
    except Exception as e:
        print(f"  ⚠ Failed to load {path}: {e}")
        return {"signals": [], "rejected_candidates": [], "counts": {}}


def load_signal_records(path="signal_desk.json"):
    """
    Backward-compatible helper for places that only need signal rows.
    """
    return load_signal_payload(path).get("signals", [])


def get_signal_status_class(status):
    status = normalize_signal_status(status)

    if status in ["ACTIVE_SIGNAL", "ACTIVE"]:
        return "signal-active"
    if status in ["TRIGGER_TOUCHED"]:
        return "signal-touched"
    if status in ["TRIGGER_READY", "READY"]:
        return "signal-ready"
    if status in ["WATCH", "WATCHLIST"]:
        return "signal-watch"
    if status in ["INVALIDATED", "VOID"]:
        return "signal-invalid"
    return "signal-wait"


def format_signal_price(value):
    val = safe_float(value, 0)
    if val > 0:
        return f"${val:.2f}"
    return "—"


def signal_is_actionable(signal):
    return normalize_signal_status(signal.get("signal_status")) in [
        "ACTIVE_SIGNAL",
        "ACTIVE",
        "TRIGGER_READY",
        "READY",
    ]


def signal_timestamp_sort_value(signal):
    """
    Numeric timestamp used only for dashboard display sorting.
    Higher value = newer signal.
    """
    text = safe_str(signal_time_value(signal), "").strip()
    if not text:
        return 0.0

    cleaned = text.replace("Z", "+00:00").replace(" ET", "")

    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo:
            return dt.timestamp()
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        pass

    # Fallback for compact HH:MM style values.
    m = re.search(r"(\d{1,2}):(\d{2})", cleaned)
    if m:
        try:
            hour = int(m.group(1))
            minute = int(m.group(2))
            now_utc, now_ny = get_times()
            if ZoneInfo:
                dt = datetime(
                    now_ny.year,
                    now_ny.month,
                    now_ny.day,
                    hour,
                    minute,
                    tzinfo=ZoneInfo("America/New_York"),
                )
                return dt.timestamp()
        except Exception:
            return 0.0

    return 0.0


def signal_sort_key(signal):
    """
    General status sort used as a safe fallback.
    Signal Desk columns use signal_display_sort_key() so each bucket is ranked
    by usefulness instead of alphabetically.
    """
    status = normalize_signal_status(signal.get("signal_status"))

    priority = {
        "ACTIVE_SIGNAL": 1,
        "ACTIVE": 1,
        "TRIGGER_READY": 2,
        "TRIGGER_TOUCHED": 2,
        "READY": 2,
        "WATCH": 3,
        "WATCHLIST": 3,
        "INVALIDATED": 4,
        "VOID": 4,
    }

    return (
        priority.get(status, 9),
        -safe_float(signal.get("confidence"), 0),
        safe_str(signal.get("symbol"), ""),
    )


def signal_display_sort_key(signal):
    """
    Signal Desk display priority.

    ACTIVE_SIGNAL:
      1. Highest confidence
      2. Best reward/risk
      3. Newest trigger

    TRIGGER_READY:
      1. Highest confidence
      2. Best reward/risk
      3. Newest ready time

    WATCH:
      1. Highest confidence
      2. Scanner score
      3. Original scanner rank
    """
    status = normalize_signal_status(signal.get("signal_status"))
    confidence = safe_float(signal.get("confidence"), 0)
    rr = safe_float(signal.get("reward_risk"), 0)
    scanner_score = safe_float(signal.get("scanner_score"), 0)
    signal_rank = safe_int(signal.get("signal_rank"), 9999)
    timestamp = signal_timestamp_sort_value(signal)
    symbol = safe_str(signal.get("symbol"), "")

    if status in ["ACTIVE_SIGNAL", "ACTIVE"]:
        return (-confidence, -rr, -timestamp, symbol)

    if status in ["TRIGGER_READY", "TRIGGER_TOUCHED", "READY"]:
        return (-confidence, -rr, -timestamp, symbol)

    if status in ["WATCH", "WATCHLIST"]:
        return (-confidence, -scanner_score, signal_rank, symbol)

    if status in ["INVALIDATED", "VOID"]:
        return (-timestamp, symbol)

    return (999, symbol)


def signal_status_group(signal):
    """
    Group signal states into the three execution buckets shown in Signal Desk.
    Invalidated / wait states are intentionally excluded from the main top tile.
    """
    status = normalize_signal_status(signal.get("signal_status"))

    if status in ["ACTIVE_SIGNAL", "ACTIVE"]:
        return "active"
    if status in ["TRIGGER_READY", "TRIGGER_TOUCHED", "READY"]:
        return "ready"
    if status in ["WATCH", "WATCHLIST"]:
        return "watch"
    return "other"


def build_signal_desk_item(signal, compact=False):
    """
    Compact signal row for the full-width Signal Desk.

    Display rule:
      - ACTIVE_SIGNAL / TRIGGER_READY show the mini trade plan.
      - WATCH is monitor-only, so it stays compact and does not show Entry/Stop/T1/R:R.
        Full details still appear inside the ticker card.
    """
    symbol = safe_str(signal.get("symbol"), "—").upper()
    setup_type = safe_str(signal.get("setup_type"), "Setup pending")
    status = normalize_signal_status(signal.get("signal_status"))
    lunch_caution = truthy(signal.get("lunch_caution")) or safe_str(signal.get("actionability"), "").upper() == "LUNCH_CAUTION"
    status_text = (
        "LUNCH CAUTION"
        if lunch_caution and status in ["TRIGGER_READY", "TRIGGER_TOUCHED", "READY"]
        else "TOUCHED"
        if status == "TRIGGER_TOUCHED"
        else status.replace("_", " ")
    )
    status_class = "signal-lunch" if lunch_caution and status in ["TRIGGER_READY", "TRIGGER_TOUCHED", "READY"] else get_signal_status_class(status)
    time_label = signal_time_label(signal)
    href = "#" + html_id_for_symbol(symbol)

    confidence = safe_float(signal.get("confidence"), 0)
    confidence_text = f"{confidence:.0f}%" if confidence > 0 else "—"
    time_html = f'<span class="signal-desk-time">{esc(time_label)}</span>' if time_label else ""
    lunch_note_html = (
        '<div class="signal-desk-lunch-warning">Lunch caution: manual chart confirmation only; no automatic Active Signal.</div>'
        if lunch_caution else ""
    )

    if compact or status in ["WATCH", "WATCHLIST"]:
        return f"""
        <div class="signal-desk-item signal-desk-watch-compact">
            <div class="signal-desk-item-top">
                <a class="signal-desk-symbol" href="{esc(href)}">{esc(symbol)}</a>
                <span class="signal-status {status_class}">{esc(status_text)}</span>
            </div>
            <div class="signal-desk-compact-line">
                <span>{esc(setup_type)}</span>
                <b>{confidence_text}</b>
            </div>
            {time_html}
            {lunch_note_html}
        </div>
        """

    entry = format_signal_price(signal.get("entry_trigger") or signal.get("entry"))
    stop = format_signal_price(signal.get("stop_loss") or signal.get("stop"))
    target_1 = format_signal_price(signal.get("target_1") or signal.get("target1"))
    rr = safe_float(signal.get("reward_risk"), 0)

    rr_text = f"{rr:.1f}:1" if rr > 0 else "—"

    return f"""
        <div class="signal-desk-item">
            <div class="signal-desk-item-top">
                <a class="signal-desk-symbol" href="{esc(href)}">{esc(symbol)}</a>
                <span class="signal-status {status_class}">{esc(status_text)}</span>
            </div>
            <div class="signal-desk-setup">{esc(setup_type)}</div>
            {time_html}
            {lunch_note_html}
            <div class="signal-desk-plan">
                <span>E <b>{entry}</b></span>
                <span>S <b>{stop}</b></span>
                <span>T1 <b>{target_1}</b></span>
                <span>R/R <b>{rr_text}</b></span>
                <span>Conf <b>{confidence_text}</b></span>
            </div>
        </div>
    """


def build_signal_desk_column(title, subtitle, signals, column_class, max_items=8, compact_items=False):
    count = len(signals)
    visible = signals[:max_items]

    if visible:
        items = "".join(build_signal_desk_item(s, compact=compact_items) for s in visible)
        extra = count - len(visible)
        extra_html = f'<div class="signal-desk-extra">+{extra} more in this bucket</div>' if extra > 0 else ""
        body = f"""
            <div class="signal-desk-list">
                {items}
                {extra_html}
            </div>
        """
    else:
        body = f"""
            <div class="signal-desk-column-empty">
                <strong>None</strong>
                <span>{esc(subtitle)}</span>
            </div>
        """

    return f"""
        <div class="signal-desk-column {esc(column_class)}">
            <div class="signal-desk-column-top">
                <div>
                    <strong>{esc(title)}</strong>
                    <span>{esc(subtitle)}</span>
                </div>
                <b>{count}</b>
            </div>
            {body}
        </div>
    """



def normalize_blocker_reason(reason):
    """
    Group verbose engine diagnostics into readable blocker buckets.
    """
    text = safe_str(reason, "").strip()
    low = text.lower()

    if not text:
        return "Other"

    if "market severe risk-off" in low or "risk-off" in low:
        return "Market severe risk-off"
    if "confidence" in low and "<" in low:
        if "watch" in low:
            return "Confidence below Watch minimum"
        if "ready" in low:
            return "Confidence below Ready minimum"
        return "Confidence below threshold"
    if "r/r" in low or "reward/risk" in low or "reward risk" in low:
        return "R/R below minimum"
    if "target 1 r/r" in low:
        return "Target 1 R/R below minimum"
    if "vwap condition" in low:
        return "VWAP condition failed"
    if "4th+ vwap touch" in low:
        return "4th+ VWAP touch"
    if "below vwap" in low:
        return "Below VWAP"
    if "too extended" in low:
        return "Too extended above VWAP"
    if "hod" in low and ("not close" in low or "too far" in low):
        return "Not close enough to HOD"
    if "sector weak" in low:
        return "Sector weak"
    if "volume" in low:
        return "Volume confirmation missing"
    if "stop distance" in low:
        return "Stop distance invalid"
    if "no intraday" in low or "no alpaca" in low or "no bars" in low:
        return "No intraday data"

    return text


def rejected_candidate_sort_key(candidate):
    """
    Rejected diagnostics should show the closest-to-usable names first.
    """
    return (
        -safe_float(candidate.get("confidence"), 0),
        -safe_float(candidate.get("scanner_score"), 0),
        safe_int(candidate.get("signal_rank"), 9999),
        safe_str(candidate.get("symbol"), ""),
    )


def build_signal_diagnostics_panel(rejected_candidates, max_preview=3):
    """
    Compact diagnostics shown only when Signal Desk has:
      0 Active / 0 Ready / 0 Watch

    It prevents crowding by showing:
      - rejected count
      - top blocker categories
      - top 3 rejected names only
    """
    rejected_candidates = [c for c in (rejected_candidates or []) if isinstance(c, dict)]

    if not rejected_candidates:
        return ""

    from collections import Counter

    blocker_counter = Counter()

    for c in rejected_candidates:
        reasons = []
        reasons.extend(c.get("rejected_reasons") or [])
        reasons.extend(c.get("not_ready_reasons") or [])

        if not reasons:
            reasons.append("No signal-quality rule passed")

        for reason in reasons:
            blocker_counter[normalize_blocker_reason(reason)] += 1

    top_blockers = blocker_counter.most_common(4)
    blocker_html = ""

    if top_blockers:
        blocker_items = []
        for reason, count in top_blockers:
            blocker_items.append(f"""
                <span class="diagnostic-pill">
                    <b>{esc(str(count))}</b> {esc(reason)}
                </span>
            """)
        blocker_html = f"""
            <div class="diagnostic-blockers">
                {''.join(blocker_items)}
            </div>
        """

    sorted_rejected = sorted(rejected_candidates, key=rejected_candidate_sort_key)
    preview = sorted_rejected[:max_preview]
    hidden_count = max(0, len(sorted_rejected) - len(preview))

    preview_rows = []

    for c in preview:
        symbol = safe_str(c.get("symbol"), "—").upper()
        confidence = safe_float(c.get("confidence"), 0)
        rr = safe_float(c.get("reward_risk"), 0)
        scanner_score = safe_float(c.get("scanner_score"), 0)

        reasons = []
        reasons.extend(c.get("rejected_reasons") or [])
        reasons.extend(c.get("not_ready_reasons") or [])

        grouped_reasons = []
        seen = set()
        for reason in reasons:
            group = normalize_blocker_reason(reason)
            if group not in seen:
                grouped_reasons.append(group)
                seen.add(group)
            if len(grouped_reasons) >= 3:
                break

        reason_text = "; ".join(grouped_reasons) if grouped_reasons else "No valid Watch/Ready/Active rule passed"

        conf_text = f"{confidence:.1f}%" if confidence > 0 else "—"
        rr_text = f"{rr:.2f}:1" if rr > 0 else "—"
        score_text = f"{scanner_score:.0f}" if scanner_score > 0 else "—"

        preview_rows.append(f"""
            <div class="diagnostic-row">
                <div>
                    <strong>{esc(symbol)}</strong>
                    <span>{esc(reason_text)}</span>
                </div>
                <div class="diagnostic-metrics">
                    <span>Conf <b>{esc(conf_text)}</b></span>
                    <span>R/R <b>{esc(rr_text)}</b></span>
                    <span>Score <b>{esc(score_text)}</b></span>
                </div>
            </div>
        """)

    hidden_html = (
        f'<div class="diagnostic-more">+{hidden_count} more rejected candidates hidden</div>'
        if hidden_count > 0 else ""
    )

    return f"""
        <div class="signal-diagnostics">
            <div class="signal-diagnostics-top">
                <div>
                    <strong>Signal Diagnostics</strong>
                    <span>No valid Watch / Ready / Active setup. Showing compact rejection summary.</span>
                </div>
                <b>{len(rejected_candidates)} Rejected</b>
            </div>
            {blocker_html}
            <div class="diagnostic-preview">
                {''.join(preview_rows)}
                {hidden_html}
            </div>
        </div>
    """


def load_signal_outcomes_summary():
    summary = load_json_object("signal_outcomes_summary.json", default={})
    if isinstance(summary, dict) and summary:
        return summary

    # Fallback: build a small summary directly from CSV if JSON has not been created yet.
    rows = load_csv_records("signal_outcomes.csv")
    if not rows:
        return {}

    now_utc, now_ny = get_times()
    today = now_ny.strftime("%Y-%m-%d")

    def row_date_value(row):
        try:
            return datetime.fromisoformat(safe_str(row.get("session_date"))).date()
        except Exception:
            return None

    cutoff = now_ny.date().toordinal() - 6
    today_rows = [r for r in rows if safe_str(r.get("session_date")) == today]
    last_7d_rows = [r for r in rows if row_date_value(r) and row_date_value(r).toordinal() >= cutoff]

    def counts_for(selected_rows):
        counts = {
            "total": len(selected_rows),
            "watch": 0,
            "trigger_ready": 0,
            "trigger_touched": 0,
            "active_signal": 0,
            "t1_hit": 0,
            "t2_hit": 0,
            "stop_hit": 0,
            "invalidated_before_entry": 0,
            "invalidated_after_entry": 0,
            "watch_removed": 0,
            "ready_only_success": 0,
            "open": 0,
            "completed": 0,
            "ready_events": 0,
            "touched_events": 0,
            "active_events": 0,
            "ready_to_touched_pct": 0.0,
            "touched_to_active_pct": 0.0,
            "ready_to_t1_pct": 0.0,
            "most_common_invalidation_reason": "",
        }

        invalid_reasons = {}

        for row in selected_rows:
            status = safe_str(row.get("outcome_status")).upper()
            ready_event = bool(safe_str(row.get("trigger_ready_time")))
            touched_event = (
                bool(safe_str(row.get("trigger_touched_time")))
                or str(row.get("hit_entry", "")).strip().lower() in ["true", "1", "yes", "y"]
                or str(row.get("entry_touched_before_active", "")).strip().lower() in ["true", "1", "yes", "y"]
                or status in ["TRIGGER_TOUCHED", "ACTIVE_SIGNAL", "T1_HIT", "T2_HIT", "STOP_HIT", "INVALIDATED_AFTER_ENTRY"]
            )
            active_event = bool(safe_str(row.get("active_time"))) or status in ["ACTIVE_SIGNAL", "T1_HIT", "T2_HIT", "STOP_HIT"]

            if ready_event:
                counts["ready_events"] += 1
            if touched_event:
                counts["touched_events"] += 1
            if active_event:
                counts["active_events"] += 1

            key = status.lower()
            if key in counts:
                counts[key] += 1
            if str(row.get("success_without_active", "")).strip().lower() in ["true", "1", "yes", "y"]:
                counts["ready_only_success"] += 1
            if status in ["WATCH", "TRIGGER_READY", "TRIGGER_TOUCHED", "ACTIVE_SIGNAL"]:
                counts["open"] += 1
            if status not in ["", "WATCH", "TRIGGER_READY", "TRIGGER_TOUCHED", "ACTIVE_SIGNAL"]:
                counts["completed"] += 1

            if status.startswith("INVALIDATED"):
                reason = safe_str(row.get("invalidation_reason") or row.get("outcome_detail"), "")
                if reason:
                    reason = reason[:120]
                    invalid_reasons[reason] = invalid_reasons.get(reason, 0) + 1

        if counts["ready_events"]:
            counts["ready_to_touched_pct"] = round(counts["touched_events"] / counts["ready_events"] * 100, 1)
            counts["ready_to_t1_pct"] = round(counts["t1_hit"] / counts["ready_events"] * 100, 1)
        if counts["touched_events"]:
            counts["touched_to_active_pct"] = round(counts["active_events"] / counts["touched_events"] * 100, 1)

        if invalid_reasons:
            counts["most_common_invalidation_reason"] = sorted(invalid_reasons.items(), key=lambda kv: kv[1], reverse=True)[0][0]

        return counts

    recent = sorted(today_rows, key=lambda r: safe_str(r.get("last_checked")), reverse=True)[:12]
    return {
        "generated_at_et": "",
        "strategy_version": "",
        "today_session": today,
        "today": counts_for(today_rows),
        "last_7_days": counts_for(last_7d_rows),
        "recent": recent,
        "file": "signal_outcomes.csv",
    }
def outcome_status_class(status):
    status = safe_str(status).upper()
    if status in ["T1_HIT", "T2_HIT"]:
        return "outcome-good"
    if status in ["STOP_HIT"]:
        return "outcome-bad"
    if status in ["INVALIDATED_BEFORE_ENTRY", "INVALIDATED_AFTER_ENTRY", "WATCH_REMOVED", "MISSED_WINDOW", "EXPIRED"]:
        return "outcome-neutral"
    if status in ["ACTIVE_SIGNAL"]:
        return "outcome-active"
    if status in ["TRIGGER_READY", "TRIGGER_TOUCHED"]:
        return "outcome-ready"
    return "outcome-watch"


def short_outcome_status(status):
    status = safe_str(status, "—").upper()
    mapping = {
        "T1_HIT": "T1",
        "T2_HIT": "T2",
        "STOP_HIT": "STOP",
        "INVALIDATED_BEFORE_ENTRY": "INV BEFORE",
        "INVALIDATED_AFTER_ENTRY": "INV AFTER",
        "WATCH_REMOVED": "WATCH REMOVED",
        "ACTIVE_SIGNAL": "ACTIVE",
        "TRIGGER_READY": "READY",
        "TRIGGER_TOUCHED": "TOUCHED",
        "WATCH": "WATCH",
    }
    return mapping.get(status, status.replace("_", " "))


def build_signal_outcomes_panel(summary):
    if not isinstance(summary, dict) or not summary:
        return """
        <section class="signal-outcomes-panel" id="outcomes">
            <div class="signal-outcomes-top">
                <div>
                    <strong>Signal Outcomes</strong>
                    <span>Tracking starts after signal_outcomes.csv is created.</span>
                </div>
            </div>
        </section>
        """

    today = summary.get("today", {}) if isinstance(summary.get("today"), dict) else {}
    last7 = summary.get("last_7_days", {}) if isinstance(summary.get("last_7_days"), dict) else {}
    recent = summary.get("recent", []) if isinstance(summary.get("recent"), list) else []

    stat_items = [
        ("Total", safe_int(today.get("total"), 0)),
        ("Ready", safe_int(today.get("ready_events"), safe_int(today.get("trigger_ready"), 0))),
        ("Touched", safe_int(today.get("touched_events"), safe_int(today.get("trigger_touched"), 0))),
        ("Active", safe_int(today.get("active_events"), safe_int(today.get("active_signal"), 0))),
        ("R→Tch", f"{safe_float(today.get('ready_to_touched_pct'), 0):.0f}%"),
        ("Tch→Act", f"{safe_float(today.get('touched_to_active_pct'), 0):.0f}%"),
        ("T1", safe_int(today.get("t1_hit"), 0)),
        ("Ready Win", safe_int(today.get("ready_only_success"), 0)),
        ("Invalid", safe_int(today.get("invalidated_before_entry"), 0) + safe_int(today.get("invalidated_after_entry"), 0)),
    ]

    stats_html = "".join(
        f'<span class="outcome-stat"><b>{esc(value)}</b>{esc(label)}</span>'
        for label, value in stat_items
    )

    last7_line = (
        f"Last 7D: Ready {safe_int(last7.get('ready_events'), 0)} · "
        f"Touched {safe_int(last7.get('touched_events'), 0)} · "
        f"Active {safe_int(last7.get('active_events'), 0)} · "
        f"T1 {safe_int(last7.get('t1_hit'), 0)}"
    )
    common_invalid = safe_str(today.get("most_common_invalidation_reason"), "")
    common_invalid_html = ""
    if common_invalid:
        if len(common_invalid) > 110:
            common_invalid = common_invalid[:107] + "..."
        common_invalid_html = f'<em class="outcome-help">Most common invalidation today: {esc(common_invalid)}</em>'

    if recent:
        rows_html = []
        for row in recent[:10]:
            symbol = safe_str(row.get("symbol"), "—").upper()
            setup = safe_str(row.get("setup_type"), "—")
            status = safe_str(row.get("outcome_status"), "—").upper()
            cls = outcome_status_class(status)
            entry = format_signal_price(row.get("entry"))
            t1 = format_signal_price(row.get("target_1"))
            stop = format_signal_price(row.get("stop"))
            best_r = safe_float(row.get("best_r_multiple"), 0)
            final_r = safe_float(row.get("final_r_multiple"), 0)
            checked = compact_time_et(row.get("last_checked"))
            price_time = compact_time_et(row.get("price_updated_at") or row.get("latest_bar_time") or row.get("quote_time"))
            reason = safe_str(row.get("invalidation_reason") or row.get("outcome_detail"), "")
            if len(reason) > 78:
                reason = reason[:75] + "..."

            r_text = f"{best_r:.2f}R" if best_r > 0 else "—"
            final_text = f"{final_r:.2f}R" if final_r not in [0, 0.0] else "—"
            success_without_active = str(row.get("success_without_active", "")).strip().lower() in ["true", "1", "yes", "y"]
            entry_before_active = str(row.get("entry_touched_before_active", "")).strip().lower() in ["true", "1", "yes", "y"]
            extra_flags = []
            if success_without_active:
                extra_flags.append('<span class="outcome-flag outcome-flag-good">Ready-only target hit</span>')
            elif entry_before_active:
                extra_flags.append('<span class="outcome-flag">Entry touched before Active</span>')
            if price_time:
                extra_flags.append(f'<span class="outcome-flag">Px {esc(price_time)}</span>')
            flags_html = "".join(extra_flags)

            rows_html.append(f"""
                <div class="outcome-row">
                    <div class="outcome-main">
                        <strong>{esc(symbol)}</strong>
                        <span>{esc(setup)}</span>
                    </div>
                    <div class="outcome-side">
                        <b class="outcome-pill {cls}">{esc(short_outcome_status(status))}</b>
                        <span>{esc(checked)}</span>
                    </div>
                    <div class="outcome-plan">
                        <span>E <b>{entry}</b></span>
                        <span>T1 <b>{t1}</b></span>
                        <span>S <b>{stop}</b></span>
                        <span>Best <b>{esc(r_text)}</b></span>
                        <span>Final <b>{esc(final_text)}</b></span>
                    </div>
                    {flags_html}
                    <div class="outcome-reason">{esc(reason)}</div>
                </div>
            """)
        body_html = f'<div class="outcome-rows">{"".join(rows_html)}</div>'
    else:
        body_html = """
            <div class="outcome-empty">
                <strong>No signal outcome rows yet today.</strong>
                <span>Rows appear after Watch, Trigger Ready, Active, Invalidated, T1/T2, or Stop events are recorded.</span>
            </div>
        """

    return f"""
    <section class="signal-outcomes-panel" id="outcomes">
        <div class="signal-outcomes-top">
            <div>
                <strong>Signal Outcomes</strong>
                <span>Today’s conversion tracker. Not your manual trade journal.</span>
                <em class="outcome-help">{esc(last7_line)}</em>
                {common_invalid_html}
            </div>
        </div>
        <div class="outcome-stats">
            {stats_html}
        </div>
        {body_html}
    </section>
    """
def build_signal_desk_panel(signals, rejected_candidates=None):
    """
    Full-width Signal Desk.

    Design decision:
      - Removed KPI summary cards from the top area because Potential / Active counts
        are already visible beside each section title.
      - Kept Signal Desk as the only top decision tile because execution status is more
        actionable than static count summaries.
      - Collapses when there are no Active / Ready / Watch signals.
    """
    signals = sorted(signals, key=signal_sort_key)

    active_signals = sorted(
        [s for s in signals if signal_status_group(s) == "active"],
        key=signal_display_sort_key,
    )
    ready_signals = sorted(
        [s for s in signals if signal_status_group(s) == "ready"],
        key=signal_display_sort_key,
    )
    watch_signals = sorted(
        [s for s in signals if signal_status_group(s) == "watch"],
        key=signal_display_sort_key,
    )

    active_count = len(active_signals)
    ready_count = len(ready_signals)
    watch_count = len(watch_signals)
    live_count = active_count + ready_count + watch_count

    if live_count == 0:
        diagnostics_html = build_signal_diagnostics_panel(rejected_candidates or [])

        return f"""
        <section class="signal-desk-panel signal-desk-compact" id="signals">
            <div class="signal-desk-top">
                <div>
                    <strong>Signal Desk</strong>
                    <span>Intraday execution status</span>
                </div>
                <div class="signal-desk-counts">
                    <span><b>{active_count}</b> Active</span>
                    <span><b>{ready_count}</b> Ready</span>
                    <span><b>{watch_count}</b> Watch</span>
                </div>
            </div>
            <div class="signal-desk-empty">
                <strong>No signal at this moment.</strong>
                <span>Scanner is monitoring for Watch, Trigger Ready, and Active setups.</span>
            </div>
            {diagnostics_html}
        </section>
        """

    active_column = build_signal_desk_column(
        "Active",
        "Triggered setups that need immediate execution review.",
        active_signals,
        "signal-col-active",
        max_items=8,
    )

    ready_column = build_signal_desk_column(
        "Trigger Ready",
        "Near-entry setups and touched triggers waiting for confirmation. Lunch caution items are manual-review only.",
        ready_signals,
        "signal-col-ready",
        max_items=8,
    )

    watch_column = build_signal_desk_column(
        "Watch",
        "Developing setups; monitor until conditions improve.",
        watch_signals,
        "signal-col-watch",
        max_items=6,
        compact_items=True,
    )

    return f"""
    <section class="signal-desk-panel" id="signals">
        <div class="signal-desk-top">
            <div>
                <strong>Signal Desk</strong>
                <span>Intraday execution status. Click a ticker to jump to its full card details.</span>
            </div>
            <div class="signal-desk-counts">
                <span><b>{active_count}</b> Active</span>
                <span><b>{ready_count}</b> Ready</span>
                <span><b>{watch_count}</b> Watch</span>
            </div>
        </div>
        <div class="signal-desk-columns">
            {active_column}
            {ready_column}
            {watch_column}
        </div>
    </section>
    """

# ==============================================================
# HTML BUILDER
# ==============================================================

def build_dashboard(potential, active, extended, highrisk, raw, active_watchlist, regime):
    now_utc, now_ny = get_times()
    status, status_class = get_market_status(now_ny)
    market_open = is_regular_market_open(status)
    premarket_monitor = is_premarket_monitor_window(status, now_ny)
    after_hours_monitor = is_after_hours_monitor_window(status, now_ny)

    scanner_meta = load_scanner_meta()
    scanner_time_label = header_time_label(scanner_meta.get("scanner_generated_at_et"))
    primary_scan_time_label = build_header_scan_time_label(status, now_ny, scanner_meta)
    dashboard_time_label = now_ny.strftime("%Y-%m-%d %H:%M ET")

    # Load once so header, Signal Desk, and live card overlay use the same payload.
    signal_payload = load_signal_payload()
    signal_refresh_time_label = header_time_label(signal_payload.get("generated_at_et"))

    regime_html = build_regime_html(regime)
    macro = load_macro_calendar()
    macro_html = build_macro_html(macro)

    scanner_regular_ready = scanner_data_is_regular_session_ready(now_ny, scanner_meta)

    premarket_section = ""
    afterhours_section = ""
    early_reclaim_section = ""

    if market_open and scanner_regular_ready:
        signals = signal_payload.get("signals", [])
        rejected_candidates = signal_payload.get("rejected_candidates", [])
        signal_map = build_signal_map(signals)

        # Refresh visible card price/VWAP/HOD from latest Signal Desk Alpaca data.
        # This does not change scanner ranking or bucket membership.
        live_market_map = build_live_market_map(signal_payload)
        potential = apply_live_market_overlay(potential, live_market_map)
        active = apply_live_market_overlay(active, live_market_map)
        raw = apply_live_market_overlay(raw, live_market_map)
        early_reclaim = load_early_reclaim_rows(raw)

        signal_desk_html = build_signal_desk_panel(signals, rejected_candidates)
        outcome_summary = load_signal_outcomes_summary()
        signal_outcomes_html = build_signal_outcomes_panel(outcome_summary)

        # Main decision screen intentionally excludes Extended / High Risk names.
        # They are still generated and saved by the scanner for diagnostics,
        # but the dashboard focuses on actionable candidates only.
        # Match the visible card limit so table/sector context reflects the decision screen.
        focus_rows = []
        focus_rows.extend(potential[:12])
        focus_rows.extend(active[:8])

        sector_snapshot = build_sector_rotation_json_panel(load_sector_rotation_payload())

        potential_section = build_section(
            "Primary Focus — Potential Movers",
            "Cleanest technical setups. Review this section first.",
            potential,
            "section-potential",
            max_cards=12,
            signal_map=signal_map,
            collapse_empty=True,
        )

        early_reclaim_section = build_early_reclaim_section(
            early_reclaim,
            signal_map=signal_map,
            max_cards=12,
        )

        active_section = build_section(
            "Active Momentum",
            "Already moving. Wait for pullback or tight consolidation before entry.",
            active,
            "section-active",
            max_cards=8,
            signal_map=signal_map,
            collapse_empty=True,
        )

        desk_table = build_desk_table(focus_rows)

        nav_tabs = """
        <div class="nav-tabs">
            <a href="#signals">Signal Desk</a>
            <a href="#potential">Potential Movers</a>
            <a href="#early">Early Reclaim</a>
            <a href="#active">Active Momentum</a>
            <a href="#sectors">Sector Rotation</a>
            <a href="#outcomes">Outcomes</a>
            <a href="#desk">Table View</a>
        </div>
        """

    elif premarket_monitor:
        # Pre-market monitor-only display.
        # Shows ranked/replaced extended-hours snapshot files only.
        # Does not run or display Signal Desk execution logic.
        movers = load_monitor_mover_records(
            "premarket_movers.csv",
            "premarket_movers.json",
            limit=12,
        )

        signal_desk_html = build_monitor_only_signal_panel(
            status,
            "Pre-market monitor window",
            "Pre-market movers are ranked by latest pre-market price versus the previous regular close. Monitor only — no entries before regular market open; wait at least 15–30 minutes after 09:30 ET before execution.",
        )
        signal_outcomes_html = ""
        potential_section = ""
        active_section = ""
        desk_table = ""
        sector_snapshot = build_sector_rotation_json_panel(load_sector_rotation_payload())

        premarket_section = build_monitor_movers_section(
            "Pre-Market Movers",
            "Visible 07:00–09:29 ET. Ranked by latest pre-market price vs previous regular close. Monitor Only — No Entries Before Regular Market Open.",
            movers,
            "section-premarket",
            max_cards=12,
        )

        nav_tabs = """
        <div class="nav-tabs">
            <a href="#signals">Signal Desk Disabled</a>
            <a href="#premarket">Pre-Market Movers</a>
            <a href="#sectors">Sector Rotation</a>
        </div>
        """

    elif after_hours_monitor:
        # After-hours monitor-only display.
        # Shows ranked/replaced extended-hours snapshot files only.
        # Does not run or display Signal Desk execution logic.
        #
        # Stale-file guard:
        # At 16:00 ET, the market status changes to AFTER-HOURS while the first
        # fresh after-hours scan may not have run yet. Hide old after-hours files
        # until a same-day 16:00+ snapshot exists.
        afterhours_fresh, afterhours_fresh_reason = monitor_snapshot_fresh_for_afterhours(
            "after_hours_movers.csv",
            "after_hours_movers.json",
            now_ny,
        )

        if afterhours_fresh:
            movers = load_monitor_mover_records(
                "after_hours_movers.csv",
                "after_hours_movers.json",
                limit=12,
            )
        else:
            movers = []

        signal_desk_html = build_monitor_only_signal_panel(
            status,
            "After-hours monitor window",
            "After-hours movers are ranked by latest after-hours price versus the regular 16:00 close. Monitor only — no after-hours entries; use this section for next-session watchlist preparation.",
        )
        signal_outcomes_html = ""
        potential_section = ""
        active_section = ""
        desk_table = ""
        sector_snapshot = build_sector_rotation_json_panel(load_sector_rotation_payload())

        if afterhours_fresh:
            afterhours_section = build_monitor_movers_section(
                "After-Hours Movers",
                "Visible 16:00–20:00 ET. Ranked by latest after-hours price vs regular 16:00 close. Monitor Only — No After-Hours Entries.",
                movers,
                "section-afterhours",
                max_cards=12,
            )
        else:
            afterhours_section = build_monitor_snapshot_waiting_section(
                "After-Hours Movers",
                "Visible 16:00–20:00 ET. Ranked by latest after-hours price vs regular 16:00 close. Monitor Only — No After-Hours Entries.",
                afterhours_fresh_reason,
                "section-afterhours",
            )

        nav_tabs = """
        <div class="nav-tabs">
            <a href="#signals">Signal Desk Disabled</a>
            <a href="#afterhours">After-Hours Movers</a>
            <a href="#sectors">Sector Rotation</a>
        </div>
        """

    else:
        # Hard display gate outside regular market hours OR before the first
        # valid 09:45+ regular-market scanner has completed.
        #
        # This prevents stale pre-market / after-hours scanner output from
        # appearing in regular market decision sections.
        if market_open:
            signal_desk_html = build_waiting_first_scanner_panel(status, scanner_time_label)
        else:
            signal_desk_html = build_market_inactive_signal_panel(status)

        signal_outcomes_html = ""
        potential_section = ""
        active_section = ""
        sector_snapshot = ""
        desk_table = ""
        nav_tabs = """
        <div class="nav-tabs">
            <a href="#signals">Signal Desk</a>
        </div>
        """

    page = Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>Elite Scanner — Pro Desk</title>
<style>
* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

html {
    scroll-padding-top: 118px;
}

body {
    background:
        radial-gradient(circle at top right, rgba(16, 185, 129, 0.08), transparent 30%),
        linear-gradient(180deg, #05070b 0%, #080b12 100%);
    color: #e5e7eb;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
    font-weight: 400;
    line-height: 1.45;
    min-height: 100vh;
    padding-bottom: 44px;
}

.header {
    position: sticky;
    top: 0;
    z-index: 50;
    background: rgba(3, 7, 18, 0.92);
    border-bottom: 1px solid rgba(148, 163, 184, 0.12);
    backdrop-filter: blur(14px);
}

.header-inner {
    max-width: 1480px;
    margin: 0 auto;
    padding: 16px 22px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 18px;
}

.title h1 {
    font-size: 20px;
    font-weight: 700;
    letter-spacing: -0.02em;
}

.title-refresh {
    color: #e5e7eb;
    text-decoration: none;
    cursor: pointer;
}

.title-refresh:hover {
    color: #38bdf8;
}

.title p {
    color: #94a3b8;
    font-size: 12px;
    margin-top: 3px;
    line-height: 1.4;
}

.header-meta {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    justify-content: flex-end;
    font-size: 12px;
    color: #94a3b8;
}

.status-pill,
.scan-pill {
    padding: 5px 11px;
    border-radius: 999px;
    font-weight: 650;
    border: 1px solid rgba(148, 163, 184, 0.18);
    background: rgba(15, 23, 42, 0.72);
}

.status-blue { color: #60a5fa; background: rgba(59, 130, 246, 0.10); }
.status-green { color: #34d399; background: rgba(16, 185, 129, 0.10); }
.status-yellow { color: #fbbf24; background: rgba(245, 158, 11, 0.10); }
.status-gray { color: #94a3b8; background: rgba(148, 163, 184, 0.08); }

.container {
    max-width: 1480px;
    margin: 0 auto;
    padding: 22px;
}

.regime-banner {
    border-radius: 14px;
    border: 1px solid rgba(148, 163, 184, 0.14);
    padding: 16px 18px;
    display: flex;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 16px;
    background: rgba(15, 23, 42, 0.82);
}

.regime-banner strong {
    display: block;
    font-size: 15px;
    font-weight: 700;
    margin-bottom: 4px;
}

.regime-banner span {
    color: #94a3b8;
    font-size: 12px;
    line-height: 1.4;
}

.regime-banner.bullish { border-left: 4px solid #10b981; }
.regime-banner.bearish { border-left: 4px solid #ef4444; }
.regime-banner.caution { border-left: 4px solid #f59e0b; }
.regime-banner.neutral { border-left: 4px solid #64748b; }

.macro-banner {
    border-radius: 14px;
    border: 1px solid rgba(148, 163, 184, 0.14);
    padding: 10px 14px;
    margin-bottom: 12px;
    background: rgba(15, 23, 42, 0.82);
}

.macro-compact {
    min-height: unset;
}

.macro-main {
    display: flex;
    justify-content: space-between;
    gap: 14px;
    flex-wrap: wrap;
    align-items: flex-start;
}

.macro-main.compact {
    align-items: center;
}

.macro-primary {
    min-width: 280px;
    flex: 1 1 auto;
}

.macro-title-line,
.macro-next {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}

.macro-title-line strong {
    font-size: 13.5px;
    font-weight: 800;
}

.macro-next {
    margin-top: 4px;
    color: #cbd5e1;
    font-size: 12px;
}

.macro-next strong {
    font-weight: 750;
}

.macro-label,
.macro-action,
.macro-source,
.macro-event-time,
.macro-time {
    color: #94a3b8;
    font-size: 11.5px;
    line-height: 1.35;
}

.macro-action {
    display: block;
    margin-top: 4px;
}

.macro-source {
    text-align: right;
    max-width: 520px;
}

.macro-events {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-top: 8px;
    align-items: center;
}

.macro-events.compact {
    margin-top: 7px;
}

.macro-event {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 4px 8px;
    border-radius: 999px;
    background: rgba(2, 6, 23, 0.38);
    border: 1px solid rgba(148, 163, 184, 0.12);
    color: #cbd5e1;
    font-size: 10.5px;
    white-space: nowrap;
    max-width: 100%;
    flex: 0 0 auto;
}

.macro-event strong {
    font-weight: 650;
}

.macro-event.muted {
    color: #94a3b8;
}

.macro-more {
    color: #94a3b8;
    background: rgba(148, 163, 184, 0.055);
}

.impact-pill {
    font-size: 9px;
    font-weight: 750;
    border-radius: 999px;
    padding: 2px 6px;
    border: 1px solid rgba(148, 163, 184, 0.16);
}

.impact-high {
    color: #f87171;
    background: rgba(239, 68, 68, 0.12);
    border-color: rgba(239, 68, 68, 0.30);
}

.impact-medium {
    color: #fbbf24;
    background: rgba(245, 158, 11, 0.12);
    border-color: rgba(245, 158, 11, 0.30);
}

.impact-low {
    color: #94a3b8;
    background: rgba(148, 163, 184, 0.08);
}

.macro-high {
    border-left: 4px solid #ef4444;
}

.macro-event-soon,
.macro-medium {
    border-left: 4px solid #f59e0b;
}

.macro-low {
    border-left: 4px solid #10b981;
}

.macro-unknown {
    border-left: 4px solid #94a3b8;
}

@media (max-width: 760px) {
    .macro-source {
        text-align: left;
    }

    .macro-event {
        white-space: normal;
        border-radius: 12px;
    }
}

.regime-metrics {
    display: flex;
    gap: 14px;
    align-items: center;
    flex-wrap: wrap;
}

.regime-metrics span {
    color: #cbd5e1;
}

.positive { color: #22c55e !important; }
.negative { color: #ef4444 !important; }

.signal-desk-panel {
    scroll-margin-top: 118px;
    background: rgba(15, 23, 42, 0.84);
    border: 1px solid rgba(148, 163, 184, 0.13);
    border-radius: 14px;
    padding: 14px;
    margin-bottom: 16px;
    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.18);
}

.signal-desk-compact {
    padding: 12px 14px;
    min-height: 84px;
}

.signal-desk-top {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
    margin-bottom: 12px;
}

.signal-desk-top strong {
    display: block;
    font-size: 15px;
    font-weight: 800;
    letter-spacing: -0.01em;
}

.signal-desk-top span {
    display: block;
    color: #94a3b8;
    font-size: 11px;
    margin-top: 2px;
    line-height: 1.35;
}

.signal-desk-counts {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    justify-content: flex-end;
}

.signal-desk-counts span {
    padding: 4px 8px;
    border-radius: 999px;
    border: 1px solid rgba(148, 163, 184, 0.14);
    background: rgba(2, 6, 23, 0.40);
    color: #94a3b8;
    font-size: 10px;
    font-weight: 650;
    white-space: nowrap;
}

.signal-desk-counts b {
    color: #e5e7eb;
}

.signal-desk-empty {
    display: flex;
    flex-direction: column;
    gap: 2px;
    color: #94a3b8;
    font-size: 11px;
    padding: 8px 10px;
    border-radius: 10px;
    background: rgba(2, 6, 23, 0.30);
    border: 1px solid rgba(148, 163, 184, 0.08);
}

.signal-desk-empty strong {
    color: #cbd5e1;
    font-size: 12px;
}

.signal-diagnostics {
    margin-top: 8px;
    padding: 8px;
    border-radius: 12px;
    border: 1px solid rgba(56, 189, 248, 0.16);
    background: rgba(2, 6, 23, 0.26);
}

.signal-diagnostics-top {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
    margin-bottom: 6px;
}

.signal-diagnostics-top strong {
    display: block;
    color: #e5e7eb;
    font-size: 12px;
    font-weight: 800;
}

.signal-diagnostics-top span {
    display: block;
    color: #94a3b8;
    font-size: 10.5px;
    margin-top: 1px;
}

.signal-diagnostics-top b {
    color: #7dd3fc;
    border: 1px solid rgba(56, 189, 248, 0.18);
    background: rgba(14, 165, 233, 0.10);
    border-radius: 999px;
    padding: 4px 8px;
    font-size: 10px;
    white-space: nowrap;
}

.diagnostic-blockers {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-bottom: 6px;
}

.diagnostic-pill {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 7px;
    border-radius: 999px;
    border: 1px solid rgba(148, 163, 184, 0.14);
    background: rgba(15, 23, 42, 0.62);
    color: #cbd5e1;
    font-size: 9.5px;
}

.diagnostic-pill b {
    color: #fbbf24;
}

.diagnostic-preview {
    display: grid;
    gap: 4px;
}

.diagnostic-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    padding: 5px 8px;
    border-radius: 8px;
    background: rgba(15, 23, 42, 0.48);
    border: 1px solid rgba(148, 163, 184, 0.08);
}

.diagnostic-row > div:first-child {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
}

.diagnostic-row strong {
    display: inline-block;
    color: #38bdf8;
    font-size: 12px;
    font-weight: 850;
    min-width: 44px;
}

.diagnostic-row span {
    color: #94a3b8;
    font-size: 10px;
    line-height: 1.2;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.diagnostic-metrics {
    display: flex;
    gap: 4px;
    flex-wrap: nowrap;
    justify-content: flex-end;
    align-content: center;
    min-width: 190px;
}

.diagnostic-metrics span {
    border-radius: 7px;
    padding: 3px 5px;
    background: rgba(2, 6, 23, 0.35);
    border: 1px solid rgba(148, 163, 184, 0.08);
    white-space: nowrap;
}

.diagnostic-metrics b {
    color: #e5e7eb;
}

.diagnostic-more {
    color: #94a3b8;
    font-size: 10.5px;
    padding: 3px 2px 0;
}

.signal-desk-columns {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
    align-items: start;
}

.signal-desk-column {
    border-radius: 12px;
    background: rgba(2, 6, 23, 0.32);
    border: 1px solid rgba(148, 163, 184, 0.10);
    padding: 10px;
    min-width: 0;
}

.signal-col-active {
    border-left: 3px solid #22c55e;
}

.signal-col-ready {
    border-left: 3px solid #f59e0b;
}

.signal-col-watch {
    border-left: 3px solid #38bdf8;
}

.signal-desk-column-top {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    align-items: flex-start;
    margin-bottom: 8px;
}

.signal-desk-column-top strong {
    display: block;
    font-size: 12px;
    font-weight: 800;
}

.signal-desk-column-top span {
    display: block;
    color: #94a3b8;
    font-size: 10px;
    margin-top: 2px;
    line-height: 1.3;
}

.signal-desk-column-top b {
    min-width: 24px;
    height: 22px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.10);
    color: #e2e8f0;
    font-size: 11px;
}

.signal-desk-list {
    display: flex;
    flex-direction: column;
    gap: 7px;
}

.signal-desk-item {
    padding: 8px;
    border-radius: 10px;
    background: rgba(15, 23, 42, 0.54);
    border: 1px solid rgba(148, 163, 184, 0.08);
}

.signal-desk-watch-compact {
    padding: 6px 8px;
}

.signal-desk-watch-compact .signal-desk-item-top {
    margin-bottom: 3px;
}

.signal-desk-compact-line {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    align-items: center;
    color: #94a3b8;
    font-size: 10px;
    line-height: 1.25;
}

.signal-desk-compact-line span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.signal-desk-compact-line b {
    color: #cbd5e1;
    font-size: 10px;
    white-space: nowrap;
}

.signal-desk-item-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 7px;
    margin-bottom: 4px;
}

.signal-desk-symbol {
    color: #38bdf8;
    font-size: 14px;
    font-weight: 850;
    text-decoration: none;
}

.signal-desk-symbol:hover {
    text-decoration: underline;
}

.signal-desk-setup {
    color: #cbd5e1;
    font-size: 11px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.signal-desk-time {
    display: block;
    color: #94a3b8;
    font-size: 10px;
    margin-top: 2px;
}

.signal-desk-plan {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 4px;
    margin-top: 7px;
}

.signal-desk-plan span {
    color: #94a3b8;
    font-size: 9px;
    white-space: nowrap;
}

.signal-desk-plan b {
    color: #e5e7eb;
    font-size: 10px;
}

.signal-desk-column-empty {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 9px;
    border-radius: 9px;
    background: rgba(15, 23, 42, 0.36);
    border: 1px dashed rgba(148, 163, 184, 0.12);
}

.signal-desk-column-empty strong {
    color: #cbd5e1;
    font-size: 11px;
}

.signal-desk-column-empty span,
.signal-desk-extra {
    color: #94a3b8;
    font-size: 10px;
    line-height: 1.35;
}

.signal-desk-extra {
    padding-left: 4px;
}

.nav-tabs {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 18px;
}

.nav-tabs a {
    color: #cbd5e1;
    text-decoration: none;
    font-size: 12px;
    padding: 7px 11px;
    border-radius: 999px;
    border: 1px solid rgba(148, 163, 184, 0.12);
    background: rgba(15, 23, 42, 0.62);
}

.floating-top {
    position: fixed;
    right: 18px;
    bottom: 18px;
    z-index: 9999;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 52px;
    height: 42px;
    padding: 0 14px;
    border-radius: 999px;
    border: 1px solid rgba(56, 189, 248, 0.35);
    background: rgba(15, 23, 42, 0.92);
    color: #e0f2fe;
    text-decoration: none;
    font-size: 12px;
    font-weight: 800;
    letter-spacing: 0.01em;
    box-shadow: 0 16px 38px rgba(0, 0, 0, 0.34);
    backdrop-filter: blur(12px);
}

.floating-top:hover {
    border-color: rgba(56, 189, 248, 0.72);
    background: rgba(14, 116, 144, 0.72);
    color: #ffffff;
}

.desk-section,
.desk-table-section,
.sector-snapshot,
.signal-desk-section {
    scroll-margin-top: 118px;
    margin-bottom: 24px;
}

.section-header {
    background: rgba(15, 23, 42, 0.86);
    border: 1px solid rgba(148, 163, 184, 0.12);
    border-radius: 12px;
    padding: 16px 18px;
    margin-bottom: 14px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
}

.section-header h2 {
    font-size: 16px;
    font-weight: 700;
    letter-spacing: -0.01em;
}

.section-header p {
    color: #94a3b8;
    font-size: 13px;
    font-weight: 400;
    margin-top: 4px;
    line-height: 1.45;
}

.section-count {
    min-width: 32px;
    height: 26px;
    padding: 0 9px;
    border-radius: 999px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: rgba(148, 163, 184, 0.10);
    color: #e2e8f0;
    font-weight: 750;
    font-size: 12px;
}

.cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
    gap: 16px;
}

.stock-card {
    scroll-margin-top: 118px;
    border-radius: 16px;
    border: 1px solid rgba(148, 163, 184, 0.13);
    border-top: 2px solid var(--accent);
    background: rgba(15, 23, 42, 0.78);
    padding: 18px;
    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.22);
}

.stock-card:hover {
    border-color: rgba(148, 163, 184, 0.25);
}

.card-top {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
}

.card-id {
    min-width: 0;
}

.symbol-row {
    display: flex;
    gap: 6px;
    align-items: center;
    flex-wrap: wrap;
}

.symbol {
    font-size: 24px;
    font-weight: 800;
    letter-spacing: -0.03em;
}

.company-name {
    color: #cbd5e1;
    font-size: 12px;
    margin-top: 5px;
    line-height: 1.35;
}

.sector-chip,
.tier {
    font-size: 10px;
    font-weight: 500;
    padding: 2px 7px;
    border-radius: 999px;
    border: 1px solid rgba(148, 163, 184, 0.18);
    color: #94a3b8;
    background: rgba(148, 163, 184, 0.06);
}

.price-box {
    text-align: right;
    white-space: nowrap;
}

.price {
    font-size: 20px;
    font-weight: 800;
    color: #f8fafc;
}

.price-meta {
    margin-top: 2px;
    font-size: 10px;
    line-height: 1.2;
    color: #94a3b8;
}

.change {
    font-size: 14px;
    font-weight: 700;
    margin-top: 2px;
}

.score-risk-row {
    display: flex;
    gap: 7px;
    flex-wrap: wrap;
    margin-top: 11px;
}

.score-pill,
.risk-pill,
.sector-status-pill {
    padding: 4px 9px;
    border-radius: 999px;
    font-size: 10px;
    font-weight: 650;
    border: 1px solid rgba(148, 163, 184, 0.16);
    background: rgba(148, 163, 184, 0.07);
    color: #cbd5e1;
}

.sector-leading {
    color: #34d399;
    border-color: rgba(16, 185, 129, 0.25);
    background: rgba(16, 185, 129, 0.08);
}

.sector-improving {
    color: #86efac;
    border-color: rgba(34, 197, 94, 0.20);
    background: rgba(34, 197, 94, 0.06);
}

.sector-neutral,
.sector-unknown {
    color: #94a3b8;
}

.sector-weak {
    color: #fca5a5;
    border-color: rgba(239, 68, 68, 0.25);
    background: rgba(239, 68, 68, 0.08);
}


.rotation-pill,
.rotation-strong,
.rotation-supportive,
.rotation-neutral,
.rotation-soft,
.rotation-weak {
    font-weight: 750;
}

.rotation-pill {
    display: inline-flex;
    align-items: center;
    border-radius: 999px;
    padding: 4px 9px;
    font-size: 10px;
    border: 1px solid rgba(148, 163, 184, 0.16);
    background: rgba(148, 163, 184, 0.07);
}

.rotation-strong {
    color: #34d399;
    border-color: rgba(16, 185, 129, 0.30);
    background: rgba(16, 185, 129, 0.10);
}

.rotation-supportive {
    color: #86efac;
    border-color: rgba(34, 197, 94, 0.24);
    background: rgba(34, 197, 94, 0.08);
}

.rotation-neutral {
    color: #cbd5e1;
}

.rotation-soft {
    color: #fbbf24;
    border-color: rgba(251, 191, 36, 0.24);
    background: rgba(245, 158, 11, 0.08);
}

.rotation-weak {
    color: #fca5a5;
    border-color: rgba(239, 68, 68, 0.28);
    background: rgba(239, 68, 68, 0.10);
}

.rotation-summary {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
    margin-bottom: 12px;
}

.rotation-summary > div {
    border: 1px solid rgba(148, 163, 184, 0.12);
    background: rgba(15, 23, 42, 0.68);
    border-radius: 12px;
    padding: 10px 12px;
}

.rotation-summary span {
    display: block;
    color: #94a3b8;
    font-size: 11px;
    margin-bottom: 4px;
}

.rotation-summary strong {
    color: #e5e7eb;
    font-size: 12px;
    line-height: 1.35;
}



.sector-rotation-meta,
.rotation-benchmarks {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 10px;
}

.sector-rotation-meta span,
.rotation-benchmarks span {
    border: 1px solid rgba(148, 163, 184, 0.12);
    background: rgba(15, 23, 42, 0.68);
    border-radius: 999px;
    padding: 6px 10px;
    color: #94a3b8;
    font-size: 11px;
    line-height: 1.35;
}

.sector-rotation-meta strong,
.rotation-benchmarks strong {
    color: #e5e7eb;
}

.rotation-benchmarks em {
    color: #94a3b8;
    font-style: normal;
    margin-left: 4px;
}

.rank-change,
.volume-pill {
    display: inline-flex;
    align-items: center;
    white-space: nowrap;
    border-radius: 999px;
    padding: 4px 8px;
    font-size: 10px;
    font-weight: 750;
    border: 1px solid rgba(148, 163, 184, 0.16);
    background: rgba(148, 163, 184, 0.07);
}

.rank-up {
    color: #34d399;
    border-color: rgba(16, 185, 129, 0.30);
    background: rgba(16, 185, 129, 0.10);
}

.rank-down {
    color: #f87171;
    border-color: rgba(239, 68, 68, 0.30);
    background: rgba(239, 68, 68, 0.10);
}

.rank-flat {
    color: #94a3b8;
}

.volume-confirmed {
    color: #34d399;
    border-color: rgba(16, 185, 129, 0.30);
    background: rgba(16, 185, 129, 0.10);
}

.volume-warning {
    color: #fbbf24;
    border-color: rgba(245, 158, 11, 0.30);
    background: rgba(245, 158, 11, 0.10);
}

.volume-normal {
    color: #cbd5e1;
}

.sector-rotation-warning {
    margin-bottom: 10px;
    border-radius: 10px;
    padding: 9px 11px;
    border: 1px solid rgba(245, 158, 11, 0.22);
    background: rgba(245, 158, 11, 0.08);
    color: #fde68a;
    font-size: 11px;
    line-height: 1.4;
}

.sector-rotation-table table {
    min-width: 1180px;
}


.sector-strip {
    margin-top: 11px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    padding: 8px 9px;
    border-radius: 9px;
    border: 1px solid rgba(148, 163, 184, 0.12);
    background: rgba(2, 6, 23, 0.36);
}

.sector-strip span {
    font-size: 11px;
    color: #94a3b8;
}

.sector-strip strong {
    color: #e5e7eb;
    font-weight: 650;
}

.catalyst-strip {
    margin: 12px 0;
    padding: 10px 11px;
    border-radius: 9px;
    border: 1px solid rgba(148, 163, 184, 0.14);
    background: rgba(2, 6, 23, 0.52);
}

.catalyst-label {
    font-size: 12px;
    font-weight: 650;
    letter-spacing: 0.02em;
    line-height: 1.4;
}

.catalyst-headline {
    font-size: 12px;
    color: #cbd5e1;
    margin-top: 6px;
    line-height: 1.45;
}

.catalyst-source {
    color: #94a3b8;
    font-size: 10px;
    font-weight: 400;
    margin-top: 5px;
    line-height: 1.35;
}

.risk-flags {
    color: #fca5a5;
    font-size: 10px;
    margin-top: 5px;
    line-height: 1.35;
}

.catalyst-positive {
    border-color: rgba(16, 185, 129, 0.35);
    background: rgba(16, 185, 129, 0.08);
    color: #34d399;
}

.catalyst-negative {
    border-color: rgba(239, 68, 68, 0.40);
    background: rgba(239, 68, 68, 0.08);
    color: #f87171;
}

.catalyst-neutral {
    border-color: rgba(148, 163, 184, 0.18);
    background: rgba(148, 163, 184, 0.06);
    color: #cbd5e1;
}

.catalyst-none {
    border-color: rgba(148, 163, 184, 0.14);
    background: rgba(148, 163, 184, 0.04);
    color: #94a3b8;
}

.metrics-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    margin-top: 12px;
}

.metrics-grid div {
    background: rgba(2, 6, 23, 0.45);
    border: 1px solid rgba(148, 163, 184, 0.10);
    border-radius: 8px;
    padding: 9px 9px;
}

.metrics-grid span {
    display: block;
    color: #94a3b8;
    font-size: 10px;
    font-weight: 400;
    margin-bottom: 3px;
    line-height: 1.35;
}

.metrics-grid strong {
    font-size: 12px;
    font-weight: 650;
    line-height: 1.35;
}


.metric-good {
    color: #34d399 !important;
}

.metric-ok {
    color: #60a5fa !important;
}

.metric-neutral {
    color: #cbd5e1 !important;
}

.metric-caution {
    color: #fbbf24 !important;
}

.metric-risk {
    color: #f87171 !important;
}


.extended-monitor-card {
    border-top-color: var(--accent);
}

.extended-move-strip {
    margin-top: 12px;
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
    padding: 9px;
    border-radius: 10px;
    border: 1px solid rgba(148, 163, 184, 0.12);
    background: rgba(2, 6, 23, 0.42);
}

.extended-move-strip div {
    min-width: 0;
}

.extended-move-strip span {
    display: block;
    color: #94a3b8;
    font-size: 10px;
    margin-bottom: 2px;
}

.extended-move-strip strong {
    color: #e5e7eb;
    font-size: 12px;
    font-weight: 800;
}

.extended-metrics.secondary {
    margin-top: 8px;
}

.extended-interpretation {
    border-color: rgba(245, 158, 11, 0.18);
    background: rgba(245, 158, 11, 0.06);
}

.extended-monitor-card .sector-chip {
    color: #cbd5e1;
}

.extended-monitor-card .tags-row {
    margin-top: 10px;
}


.early-reclaim-card {
    border-top-color: #38bdf8;
}

.early-reclaim-strip {
    margin-top: 12px;
    display: grid;
    grid-template-columns: 1.5fr 0.7fr 0.8fr;
    gap: 8px;
    padding: 9px;
    border-radius: 10px;
    border: 1px solid rgba(56, 189, 248, 0.18);
    background: rgba(14, 165, 233, 0.055);
}

.early-reclaim-strip div {
    min-width: 0;
}

.early-reclaim-strip span {
    display: block;
    color: #94a3b8;
    font-size: 10px;
    margin-bottom: 2px;
}

.early-reclaim-strip strong {
    color: #e5e7eb;
    font-size: 12px;
    font-weight: 800;
    word-break: break-word;
}

.early-reclaim-metrics {
    margin-top: 8px;
}

.early-reclaim-interpretation {
    border-color: rgba(56, 189, 248, 0.18);
    background: rgba(14, 165, 233, 0.055);
}

.section-early-reclaim .section-header {
    border-color: rgba(56, 189, 248, 0.18);
}

.section-early-reclaim .section-count {
    background: rgba(14, 165, 233, 0.12);
    color: #bae6fd;
}

.status-row {
    display: flex;
    gap: 7px;
    flex-wrap: wrap;
    margin-top: 12px;
}

.status-chip,
.tag {
    border-radius: 999px;
    padding: 4px 9px;
    font-size: 10px;
    font-weight: 600;
    line-height: 1.2;
    border: 1px solid rgba(148, 163, 184, 0.18);
}

.status-tech,
.tag-tech {
    background: rgba(59, 130, 246, 0.12);
    color: #60a5fa;
    border-color: rgba(59, 130, 246, 0.32);
}

.status-positive,
.tag-positive {
    background: rgba(16, 185, 129, 0.14);
    color: #34d399;
    border-color: rgba(16, 185, 129, 0.34);
}

.tag-caution {
    background: rgba(245, 158, 11, 0.16);
    color: #fbbf24;
    border-color: rgba(245, 158, 11, 0.38);
}

.tag-squeeze {
    background: rgba(168, 85, 247, 0.16);
    color: #c084fc;
    border-color: rgba(168, 85, 247, 0.38);
}

.status-risk,
.tag-risk {
    background: rgba(239, 68, 68, 0.14);
    color: #f87171;
    border-color: rgba(239, 68, 68, 0.38);
}

.status-neutral,
.tag-neutral {
    background: rgba(148, 163, 184, 0.08);
    color: #cbd5e1;
    border-color: rgba(148, 163, 184, 0.18);
}

.interpretation {
    margin-top: 12px;
    background: rgba(148, 163, 184, 0.05);
    border: 1px solid rgba(148, 163, 184, 0.10);
    border-radius: 8px;
    padding: 10px 11px;
    color: #cbd5e1;
    font-size: 12px;
    line-height: 1.5;
}

.tags-row {
    display: flex;
    gap: 7px;
    flex-wrap: wrap;
    margin-top: 12px;
}

.mini-panel {
    margin-top: 12px;
    background: rgba(168, 85, 247, 0.06);
    border: 1px solid rgba(168, 85, 247, 0.14);
    border-radius: 8px;
    padding: 9px 10px;
}

.mini-row {
    display: flex;
    justify-content: space-between;
    color: #94a3b8;
    font-size: 11px;
    padding: 2px 0;
    line-height: 1.4;
}

.mini-row strong {
    color: #c084fc;
    font-weight: 700;
}


.stock-card:target {
    border-color: rgba(251, 191, 36, 0.65);
    box-shadow:
        0 0 0 2px rgba(251, 191, 36, 0.18),
        0 18px 38px rgba(0, 0, 0, 0.28);
}

.signal-detail {
    margin-top: 12px;
    padding: 11px;
    border-radius: 10px;
    border: 1px solid rgba(56, 189, 248, 0.20);
    background: rgba(14, 165, 233, 0.055);
}

.signal-detail-top {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 10px;
    margin-bottom: 8px;
}

.signal-detail-top strong {
    display: block;
    font-size: 12px;
    font-weight: 750;
}

.signal-detail-top span {
    color: #94a3b8;
    font-size: 10px;
}

.signal-detail-meta {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    color: #94a3b8;
    font-size: 10px;
    margin-bottom: 8px;
}

.signal-plan-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 7px;
    margin-bottom: 8px;
}

.signal-plan-grid div {
    background: rgba(2, 6, 23, 0.45);
    border: 1px solid rgba(148, 163, 184, 0.10);
    border-radius: 7px;
    padding: 7px;
}

.signal-plan-grid span {
    display: block;
    color: #94a3b8;
    font-size: 9px;
    margin-bottom: 2px;
}

.signal-plan-grid b {
    color: #e5e7eb;
    font-size: 11px;
}

.signal-reason {
    color: #cbd5e1;
    font-size: 11px;
    line-height: 1.45;
    margin-top: 5px;
}

.signal-reason strong {
    color: #e5e7eb;
}

.event-warning {
    border-left: 3px solid #f59e0b;
    padding-left: 8px;
    color: #fde68a;
}

.lunch-warning {
    border-left-color: #ef4444;
    color: #fecaca;
    background: rgba(239, 68, 68, 0.06);
    border-radius: 8px;
    padding: 7px 8px;
}

.signal-desk-lunch-warning {
    margin-top: 5px;
    color: #fecaca;
    font-size: 10px;
    line-height: 1.35;
    border-left: 3px solid #ef4444;
    padding-left: 7px;
}

.compact-empty {
    padding: 14px 16px;
    max-width: 420px;
}

.compact-empty strong {
    font-size: 13px;
}

.compact-empty span {
    font-size: 12px;
}

.card-actions {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-top: 14px;
}

.card-actions a,
.action-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    text-align: center;
    color: #cbd5e1;
    background: rgba(15, 23, 42, 0.9);
    border: 1px solid rgba(148, 163, 184, 0.12);
    border-radius: 7px;
    padding: 8px 8px;
    text-decoration: none;
    font-size: 11px;
    font-weight: 650;
    transition: background 0.15s ease, transform 0.15s ease, border-color 0.15s ease;
}

.action-btn img {
    width: 15px;
    height: 15px;
    object-fit: contain;
    display: inline-block;
}

.action-chart {
    border-color: rgba(59, 130, 246, 0.30);
}

.action-yahoo {
    border-color: rgba(168, 85, 247, 0.34);
}

.action-twits {
    border-color: rgba(59, 130, 246, 0.34);
}

.card-actions a:hover,
.action-btn:hover {
    background: rgba(30, 41, 59, 0.95);
    transform: translateY(-1px);
}


.signal-empty {
    padding: 14px 16px;
    border-radius: 12px;
    border: 1px dashed rgba(148, 163, 184, 0.18);
    background: rgba(15, 23, 42, 0.54);
    color: #94a3b8;
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.signal-empty strong {
    color: #e2e8f0;
    font-size: 13px;
}

.signal-empty span {
    font-size: 12px;
    line-height: 1.4;
}

.signal-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 12px;
}

.signal-card {
    border-radius: 12px;
    border: 1px solid rgba(16, 185, 129, 0.18);
    background: rgba(15, 23, 42, 0.78);
    padding: 13px;
}

.signal-card-top {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    align-items: flex-start;
    margin-bottom: 10px;
}

.signal-card-top strong {
    display: block;
    font-size: 17px;
    font-weight: 800;
    letter-spacing: -0.02em;
}

.signal-card-top span {
    display: block;
    color: #94a3b8;
    font-size: 11px;
    line-height: 1.35;
    margin-top: 2px;
}

.signal-status {
    white-space: nowrap;
    border-radius: 999px;
    padding: 4px 8px;
    font-size: 10px;
    font-weight: 700;
    border: 1px solid rgba(148, 163, 184, 0.18);
}

.signal-active {
    color: #34d399;
    background: rgba(16, 185, 129, 0.12);
    border-color: rgba(16, 185, 129, 0.30);
}

.signal-ready {
    color: #fbbf24;
    background: rgba(245, 158, 11, 0.12);
    border-color: rgba(245, 158, 11, 0.30);
}

.signal-touched {
    color: #fde68a;
    background: rgba(245, 158, 11, 0.18);
    border-color: rgba(245, 158, 11, 0.48);
}

.signal-lunch {
    color: #fecaca;
    background: rgba(239, 68, 68, 0.16);
    border-color: rgba(239, 68, 68, 0.45);
}

.signal-watch {
    color: #60a5fa;
    background: rgba(59, 130, 246, 0.10);
    border-color: rgba(59, 130, 246, 0.26);
}

.signal-wait {
    color: #cbd5e1;
    background: rgba(148, 163, 184, 0.08);
    border-color: rgba(148, 163, 184, 0.18);
}

.signal-invalid {
    color: #f87171;
    background: rgba(239, 68, 68, 0.12);
    border-color: rgba(239, 68, 68, 0.30);
}

.signal-metrics {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 7px;
}

.signal-metrics div {
    background: rgba(2, 6, 23, 0.42);
    border: 1px solid rgba(148, 163, 184, 0.10);
    border-radius: 8px;
    padding: 7px 8px;
}

.signal-metrics span {
    display: block;
    color: #94a3b8;
    font-size: 9px;
    margin-bottom: 3px;
}

.signal-metrics b {
    color: #e5e7eb;
    font-size: 11px;
}


.empty-section {
    padding: 26px;
    border-radius: 12px;
    border: 1px dashed rgba(148, 163, 184, 0.18);
    color: #94a3b8;
    background: rgba(15, 23, 42, 0.5);
    display: flex;
    flex-direction: column;
    gap: 5px;
}

.table-wrap {
    overflow-x: auto;
    border: 1px solid rgba(148, 163, 184, 0.12);
    border-radius: 12px;
    background: rgba(15, 23, 42, 0.72);
}

table {
    width: 100%;
    border-collapse: collapse;
    min-width: 1100px;
}

.compact-table table {
    min-width: 760px;
}

th {
    text-align: left;
    color: #94a3b8;
    font-size: 10px;
    font-weight: 650;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 10px;
    background: rgba(2, 6, 23, 0.55);
}

td {
    padding: 9px 10px;
    border-top: 1px solid rgba(148, 163, 184, 0.08);
    font-size: 11px;
    color: #cbd5e1;
    vertical-align: top;
    line-height: 1.4;
}

td small {
    color: #94a3b8;
    font-size: 10px;
}

.footer-note {
    color: #64748b;
    font-size: 11px;
    font-weight: 400;
    text-align: center;
    margin-top: 22px;
}

@media (max-width: 1100px) {
    .signal-desk-columns {
        grid-template-columns: 1fr;
    }
}

@media (max-width: 760px) {

    .signal-desk-top {
        flex-direction: column;
    }

    .signal-desk-counts {
        justify-content: flex-start;
    }

    .signal-desk-plan,
    .signal-plan-grid {
        grid-template-columns: repeat(2, 1fr);
    }

    .header-inner {
        align-items: flex-start;
        flex-direction: column;
    }

    .cards-grid {
        grid-template-columns: 1fr;
    }

    .metrics-grid {
        grid-template-columns: repeat(2, 1fr);
    }

    .card-top {
        flex-direction: row;
    }
}

/* =========================
   Signal Outcomes Panel
   ========================= */
.signal-outcomes-panel {
    margin: 18px 0 16px;
    padding: 14px 16px;
    border-radius: 16px;
    border: 1px solid rgba(148, 163, 184, 0.16);
    background:
        linear-gradient(180deg, rgba(15, 23, 42, 0.88), rgba(15, 23, 42, 0.62)),
        radial-gradient(circle at top right, rgba(34, 211, 238, 0.10), transparent 36%);
    box-shadow: 0 14px 40px rgba(0, 0, 0, 0.22);
}

.signal-outcomes-top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 10px;
}

.signal-outcomes-top > div:first-child {
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.signal-outcomes-top strong {
    font-size: 16px;
    letter-spacing: -0.01em;
}

.signal-outcomes-top span {
    color: #94a3b8;
    font-size: 12px;
}

.signal-outcomes-meta {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    flex-wrap: wrap;
    color: #94a3b8;
    font-size: 11px;
}

.signal-outcomes-meta span {
    border: 1px solid rgba(148, 163, 184, 0.16);
    background: rgba(2, 6, 23, 0.42);
    padding: 5px 9px;
    border-radius: 999px;
}

.outcome-help {
    display: block;
    margin-top: 3px;
    color: #94a3b8;
    font-size: 11px;
    font-style: normal;
    line-height: 1.35;
}


.outcome-stats {
    display: grid;
    grid-template-columns: repeat(9, minmax(72px, 1fr));
    gap: 7px;
    margin-bottom: 10px;
}

.outcome-stat {
    border: 1px solid rgba(148, 163, 184, 0.14);
    background: rgba(2, 6, 23, 0.46);
    border-radius: 11px;
    padding: 7px 8px;
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-height: 44px;
}

.outcome-stat b {
    color: #f8fafc;
    font-size: 15px;
    line-height: 1;
}

.outcome-stat {
    color: #94a3b8;
    font-size: 11px;
    font-weight: 650;
}

.outcome-rows {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 8px;
    justify-content: stretch;
    align-items: stretch;
}

.outcome-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-areas:
        "main side"
        "plan plan"
        "flag flag"
        "reason reason";
    gap: 6px 8px;
    align-items: flex-start;
    padding: 8px 9px;
    border-radius: 12px;
    border: 1px solid rgba(148, 163, 184, 0.14);
    background: rgba(2, 6, 23, 0.36);
    min-width: 0;
}

.outcome-main {
    grid-area: main;
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
}

.outcome-main strong {
    color: #38bdf8;
    font-size: 14px;
    letter-spacing: 0.01em;
}

.outcome-main span {
    color: #cbd5e1;
    font-size: 10px;
    text-transform: uppercase;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.outcome-plan {
    grid-area: plan;
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    color: #94a3b8;
    font-size: 10px;
}

.outcome-plan span {
    border: 1px solid rgba(148, 163, 184, 0.12);
    background: rgba(15, 23, 42, 0.72);
    border-radius: 999px;
    padding: 3px 6px;
    white-space: nowrap;
}

.outcome-plan b {
    color: #f8fafc;
}

.outcome-side {
    grid-area: side;
    display: flex;
    align-items: flex-end;
    justify-content: flex-start;
    flex-direction: column;
    gap: 4px;
    color: #94a3b8;
    font-size: 10px;
}

.outcome-pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 999px;
    padding: 5px 9px;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 0.02em;
    border: 1px solid rgba(148, 163, 184, 0.16);
    background: rgba(15, 23, 42, 0.72);
    color: #cbd5e1;
    text-transform: uppercase;
    white-space: nowrap;
}

.outcome-good {
    color: #34d399;
    border-color: rgba(52, 211, 153, 0.30);
    background: rgba(16, 185, 129, 0.12);
}

.outcome-bad {
    color: #fb7185;
    border-color: rgba(251, 113, 133, 0.30);
    background: rgba(244, 63, 94, 0.12);
}

.outcome-neutral {
    color: #cbd5e1;
    border-color: rgba(148, 163, 184, 0.22);
    background: rgba(148, 163, 184, 0.10);
}

.outcome-active {
    color: #34d399;
    border-color: rgba(52, 211, 153, 0.35);
    background: rgba(16, 185, 129, 0.14);
}

.outcome-ready {
    color: #fbbf24;
    border-color: rgba(251, 191, 36, 0.35);
    background: rgba(245, 158, 11, 0.14);
}

.outcome-watch {
    color: #60a5fa;
    border-color: rgba(96, 165, 250, 0.32);
    background: rgba(59, 130, 246, 0.12);
}

.outcome-flag {
    grid-area: flag;
    display: inline-flex;
    width: fit-content;
    border-radius: 999px;
    padding: 4px 8px;
    border: 1px solid rgba(148, 163, 184, 0.16);
    background: rgba(15, 23, 42, 0.68);
    color: #cbd5e1;
    font-size: 10px;
    font-weight: 750;
}

.outcome-flag-good {
    color: #34d399;
    border-color: rgba(52, 211, 153, 0.28);
    background: rgba(16, 185, 129, 0.12);
}

.outcome-reason {
    grid-area: reason;
    color: #94a3b8;
    font-size: 11px;
    line-height: 1.35;
    border-top: 1px dashed rgba(148, 163, 184, 0.12);
    padding-top: 8px;
    margin-top: 1px;
}

.outcome-empty {
    padding: 14px;
    border-radius: 12px;
    border: 1px dashed rgba(148, 163, 184, 0.18);
    background: rgba(2, 6, 23, 0.35);
    display: flex;
    flex-direction: column;
    gap: 3px;
}

.outcome-empty strong {
    font-size: 13px;
}

.outcome-empty span {
    color: #94a3b8;
    font-size: 12px;
}

@media (max-width: 1600px) {
    .outcome-rows {
        grid-template-columns: repeat(4, minmax(0, 1fr));
    }
}

@media (max-width: 1180px) {
    .outcome-stats {
        grid-template-columns: repeat(5, minmax(72px, 1fr));
    }

    .outcome-rows {
        grid-template-columns: repeat(3, minmax(0, 1fr));
    }
}

@media (max-width: 860px) {
    .rotation-summary { grid-template-columns: 1fr; }

    .outcome-stats {
        grid-template-columns: repeat(4, minmax(74px, 1fr));
    }

    .outcome-rows {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .outcome-row {
        grid-template-columns: 1fr auto;
        align-items: flex-start;
    }

    .outcome-side {
        align-items: flex-end;
        flex-direction: column;
        justify-content: flex-start;
    }
}

@media (max-width: 640px) {
    .outcome-stats {
        grid-template-columns: repeat(2, minmax(74px, 1fr));
    }

    .signal-outcomes-panel {
        padding: 14px;
    }

    .outcome-rows {
        grid-template-columns: 1fr;
    }

    .outcome-row {
        grid-template-columns: 1fr;
        grid-template-areas:
            "main"
            "side"
            "plan"
            "flag"
            "reason";
    }

    .outcome-side {
        align-items: flex-start;
        flex-direction: row;
        justify-content: flex-start;
    }
}

</style>
</head>
<body>

<a class="floating-top" href="#top" onclick="window.scrollTo({top:0,left:0,behavior:'smooth'}); return false;" title="Back to top">Top ↑</a>

<header class="header" id="top">
    <div class="header-inner">
        <div class="title">
            <h1><a class="title-refresh" href="" title="Refresh dashboard">Elite Scanner — Pro Desk</a></h1>
            <p>Technical Potential Movers + Catalyst Confirmation + Sector Context</p>
        </div>
        <div class="header-meta">
            <span class="scan-pill">$primary_scan_time</span>
            <span class="scan-pill">Signal Refresh: $signal_refresh_time</span>
            <span class="scan-pill">Dashboard Built: $dashboard_time</span>
            <span class="status-pill $status_class">Market: $status</span>
        </div>
    </div>
</header>

<main class="container">
    $regime_html
    $macro_html
    $signal_desk_html

    $nav_tabs

    <div id="premarket">$premarket_section</div>
    <div id="potential">$potential_section</div>
    <div id="early">$early_reclaim_section</div>
    <div id="active">$active_section</div>
    <div id="afterhours">$afterhours_section</div>
    <div id="sectors">$sector_snapshot</div>
    $signal_outcomes_html
    <div id="desk">$desk_table</div>

    <div class="footer-note">
        © Elite Scanner Pro Desk. Data refreshes during market hours via Elite Runner; page auto-refreshes every 60 seconds. Extended/high-risk names are saved to CSV but hidden. Alpaca SIP data enabled; confirm spread, liquidity, VWAP, and news before execution.
    </div>
</main>

</body>
</html>""")

    return page.safe_substitute(
        status=status,
        status_class=status_class,
        primary_scan_time=primary_scan_time_label,
        signal_refresh_time=signal_refresh_time_label,
        dashboard_time=dashboard_time_label,
        regime_html=regime_html,
        macro_html=macro_html,
        sector_snapshot=sector_snapshot,
        signal_desk_html=signal_desk_html,
        signal_outcomes_html=signal_outcomes_html,
        nav_tabs=nav_tabs,
        premarket_section=premarket_section,
        potential_section=potential_section,
        early_reclaim_section=early_reclaim_section,
        active_section=active_section,
        afterhours_section=afterhours_section,
        desk_table=desk_table,
    )


# ==============================================================
# MAIN
# ==============================================================

def main():
    print("\n" + "=" * 70)
    print("BUILDING ELITE PRO DESK DASHBOARD")
    print("=" * 70)

    potential = load_csv_records("potential_movers.csv", limit=12)
    active = load_csv_records("active_momentum.csv", limit=8)
    extended = load_csv_records("extended_movers.csv", limit=10)
    highrisk = load_csv_records("high_risk_movers.csv", limit=10)
    raw = load_csv_records("elite_watchlist_raw.csv")
    active_watchlist = load_csv_records("elite_watchlist.csv", limit=20)

    if not active_watchlist:
        active_watchlist = load_json_records("elite_watchlist.json")

    # Fallback if bucket files do not exist yet.
    if not potential and not active and active_watchlist:
        potential = [
            s for s in active_watchlist
            if safe_str(s.get("setup_bucket")) == "POTENTIAL_MOVER"
        ]
        active = [
            s for s in active_watchlist
            if safe_str(s.get("setup_bucket")) == "ACTIVE_MOMENTUM"
        ]
        extended = [
            s for s in active_watchlist
            if safe_str(s.get("setup_bucket")) == "EXTENDED_CHASE_RISK"
        ]
        highrisk = [
            s for s in active_watchlist
            if safe_str(s.get("setup_bucket")) == "HIGH_RISK_EXTREME"
        ]

    regime = load_regime()

    print(f"  Potential Movers:       {len(potential)}")
    print(f"  Active Momentum:        {len(active)}")
    print(f"  Early Reclaim Runners:  {len(load_early_reclaim_rows(raw))} (visible section max 12)")
    print(f"  Extended / Chase Risk:  {len(extended)} (generated, hidden on dashboard)")
    print(f"  High Risk / Extreme:    {len(highrisk)} (generated, hidden on dashboard)")
    print(f"  Raw Scored:             {len(raw)}")
    print(f"  Active Watchlist:       {len(active_watchlist)}")

    html_output = build_dashboard(
        potential=potential,
        active=active,
        extended=extended,
        highrisk=highrisk,
        raw=raw,
        active_watchlist=active_watchlist,
        regime=regime,
    )

    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(html_output)

    print("  ✓ Dashboard saved to dashboard.html")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
