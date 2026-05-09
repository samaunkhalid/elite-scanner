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
  - Sector leadership snapshot
  - Catalyst strip
  - Meaning-based tag colors
  - Last scan time and market status
  - Dashboard hides Extended / High Risk sections from the main decision screen
"""

import json
import os
import html
from datetime import datetime, timezone
from string import Template

import pandas as pd

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


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


def build_card(stock):
    symbol = safe_str(stock.get("symbol"), "—").upper()
    tier = safe_str(stock.get("tier"), "—")
    score = safe_int(stock.get("score"), 0)
    price = safe_float(stock.get("price"), 0)
    change_pct = safe_float(stock.get("change_pct"), 0)
    bucket = safe_str(stock.get("setup_bucket"), "MONITOR")
    risk = safe_str(stock.get("risk_category"), "NORMAL")

    bucket_meta = get_bucket_meta(bucket)
    catalyst = get_catalyst_meta(stock)

    company_name = safe_str(stock.get("company_name"), symbol)
    sector = safe_str(stock.get("sector"), "Unknown")
    sector_etf = safe_str(stock.get("sector_etf"), "SPY")
    sector_status = safe_str(stock.get("sector_status"), "UNKNOWN").upper()
    sector_status_class = get_sector_status_class(sector_status)
    sector_change = safe_float(stock.get("sector_change_pct"), 0)
    stock_vs_sector = safe_float(stock.get("stock_vs_sector_pct"), 0)

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

    # More color variety in status row.
    vwap_cls = "status-tech" if above_vwap else "status-neutral"
    risk_cls = "status-risk" if risk not in ["NORMAL", "", "—"] else "status-neutral"

    tags_html = build_tags(stock)

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
    <div class="stock-card {bucket_meta['class']}" style="--accent:{bucket_meta['accent']};">
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
            <span>ETF <strong>{esc(sector_etf)}</strong> <b class="{'positive' if sector_change >= 0 else 'negative'}">{sector_change:+.2f}%</b></span>
            <span>Vs Sector <b class="{'positive' if stock_vs_sector >= 0 else 'negative'}">{stock_vs_sector:+.2f}%</b></span>
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


def build_section(title, subtitle, stocks, class_name, max_cards=10):
    count = len(stocks)
    cards = "".join(build_card(s) for s in stocks[:max_cards])

    if not cards:
        cards = f"""
        <div class="empty-section">
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
            <span>Bias: {esc(bias.replace("_", " "))} · Data: Yahoo + Alpaca IEX + Alpaca News</span>
        </div>
        <div class="regime-metrics">
            <span>SPY <b class="{'positive' if spy >= 0 else 'negative'}">{spy:+.2f}%</b></span>
            <span>QQQ <b class="{'positive' if qqq >= 0 else 'negative'}">{qqq:+.2f}%</b></span>
            <span>IWM <b class="{'positive' if iwm >= 0 else 'negative'}">{iwm:+.2f}%</b></span>
            <span>VIX <b>{vix:.1f}</b></span>
        </div>
    </div>
    """


def build_kpi_row(potential, active, extended, highrisk, raw, active_watchlist):
    """
    KPI logic is aligned with the visible decision dashboard.

    Active Watchlist now equals the names actually shown for review:
      Potential Movers + Active Momentum

    Extended / High Risk remain generated by the scanner for diagnostics,
    but they are intentionally hidden from the main decision view.
    """
    decision_rows = list(potential) + list(active)
    total_focus = len(decision_rows)
    avg_score = 0

    if decision_rows:
        avg_score = sum(safe_float(s.get("score"), 0) for s in decision_rows) / len(decision_rows)

    return f"""
    <div class="kpi-grid">
        <div class="kpi-card focus"><span>{total_focus}</span><label>Active Watchlist</label></div>
        <div class="kpi-card potential"><span>{len(potential)}</span><label>Potential</label></div>
        <div class="kpi-card active"><span>{len(active)}</span><label>Active Momentum</label></div>
        <div class="kpi-card"><span>{len(raw)}</span><label>Raw Scored</label></div>
        <div class="kpi-card"><span>{avg_score:.0f}</span><label>Avg Score</label></div>
    </div>
    """


def build_sector_snapshot(raw, focus_rows, regime):
    if not raw:
        return ""

    rows_by_sector = {}

    for stock in raw:
        sector = safe_str(stock.get("sector"), "Unknown")
        if not sector or sector == "Unknown":
            continue

        etf = safe_str(stock.get("sector_etf"), "SPY")
        sector_change = safe_float(stock.get("sector_change_pct"), 0)
        sector_vs_spy = safe_float(stock.get("sector_vs_spy_pct"), 0)
        sector_status = safe_str(stock.get("sector_status"), "UNKNOWN").upper()

        if sector not in rows_by_sector:
            rows_by_sector[sector] = {
                "sector": sector,
                "etf": etf,
                "sector_change": sector_change,
                "sector_vs_spy": sector_vs_spy,
                "sector_status": sector_status,
                "count": 0,
                "focus": [],
            }

        rows_by_sector[sector]["count"] += 1

    focus_symbols = set()
    for stock in focus_rows:
        sym = safe_str(stock.get("symbol"), "").upper()
        if not sym:
            continue
        focus_symbols.add(sym)

        sector = safe_str(stock.get("sector"), "Unknown")
        if sector in rows_by_sector and sym not in rows_by_sector[sector]["focus"]:
            rows_by_sector[sector]["focus"].append(sym)

    rows = list(rows_by_sector.values())
    rows.sort(key=lambda x: (x["sector_vs_spy"], x["sector_change"], x["count"]), reverse=True)
    rows = rows[:8]

    if not rows:
        return ""

    html_rows = ""

    for r in rows:
        status_class = get_sector_status_class(r["sector_status"])
        focus_text = ", ".join(r["focus"][:4]) if r["focus"] else "—"

        html_rows += f"""
        <tr>
            <td><strong>{esc(r['sector'])}</strong></td>
            <td>{esc(r['etf'])}</td>
            <td class="{'positive' if r['sector_change'] >= 0 else 'negative'}">{r['sector_change']:+.2f}%</td>
            <td class="{'positive' if r['sector_vs_spy'] >= 0 else 'negative'}">{r['sector_vs_spy']:+.2f}%</td>
            <td><span class="sector-status-pill {status_class}">{esc(r['sector_status'])}</span></td>
            <td>{r['count']}</td>
            <td>{esc(focus_text)}</td>
        </tr>
        """

    return f"""
    <section class="sector-snapshot">
        <div class="section-header">
            <div>
                <h2>Sector Leadership Snapshot</h2>
                <p>Ranks sectors by same-day ETF strength versus SPY. This is leadership context, not a full rotation model yet.</p>
            </div>
        </div>
        <div class="table-wrap compact-table">
            <table>
                <thead>
                    <tr>
                        <th>Sector</th>
                        <th>ETF</th>
                        <th>ETF %</th>
                        <th>Vs SPY</th>
                        <th>Status</th>
                        <th>Names</th>
                        <th>Active Focus</th>
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
                <h2>Desk View</h2>
                <p>Compact comparison table for the full active review list.</p>
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


# ==============================================================
# HTML BUILDER
# ==============================================================

def build_dashboard(potential, active, extended, highrisk, raw, active_watchlist, regime):
    now_utc, now_ny = get_times()
    status, status_class = get_market_status(now_ny)

    regime_html = build_regime_html(regime)
    kpi_html = build_kpi_row(potential, active, extended, highrisk, raw, active_watchlist)

    # Main decision screen intentionally excludes Extended / High Risk names.
    # They are still generated and saved by the scanner for diagnostics,
    # but the dashboard focuses on actionable candidates only.
    focus_rows = []
    focus_rows.extend(potential[:10])
    focus_rows.extend(active[:10])

    sector_snapshot = build_sector_snapshot(raw, focus_rows, regime)

    potential_section = build_section(
        "Primary Focus — Potential Movers",
        "Cleanest technical setups. Review this section first.",
        potential,
        "section-potential",
        max_cards=10,
    )

    active_section = build_section(
        "Active Momentum",
        "Already moving. Wait for pullback or tight consolidation before entry.",
        active,
        "section-active",
        max_cards=10,
    )

    desk_table = build_desk_table(focus_rows)

    page = Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="300">
<title>Elite Scanner — Pro Desk</title>
<style>
* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
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

.kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
    gap: 10px;
    margin-bottom: 16px;
}

.kpi-card {
    background: rgba(15, 23, 42, 0.82);
    border: 1px solid rgba(148, 163, 184, 0.12);
    border-radius: 12px;
    padding: 14px 12px;
}

.kpi-card span {
    display: block;
    font-size: 24px;
    font-weight: 800;
}

.kpi-card label {
    color: #94a3b8;
    font-size: 11px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-top: 5px;
    display: block;
}

.kpi-card.focus span { color: #38bdf8; }
.kpi-card.potential span { color: #38bdf8; }
.kpi-card.active span { color: #22c55e; }
.kpi-card.extended span { color: #f59e0b; }
.kpi-card.risk span { color: #ef4444; }

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

.desk-section,
.desk-table-section,
.sector-snapshot {
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

@media (max-width: 760px) {
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
</style>
</head>
<body>

<header class="header">
    <div class="header-inner">
        <div class="title">
            <h1>Elite Scanner — Pro Desk</h1>
            <p>Technical Potential Movers + Catalyst Confirmation + Sector Context</p>
        </div>
        <div class="header-meta">
            <span class="scan-pill">Last Scan: $ny_time ET</span>
            <span class="status-pill $status_class">Market: $status</span>
        </div>
    </div>
</header>

<main class="container">
    $regime_html
    $sector_snapshot
    $kpi_html

    <div class="nav-tabs">
        <a href="#potential">Potential Movers</a>
        <a href="#active">Active Momentum</a>
        <a href="#desk">Desk View</a>
    </div>

    <div id="potential">$potential_section</div>
    <div id="active">$active_section</div>
    <div id="desk">$desk_table</div>

    <div class="footer-note">
        Static dashboard. Data updates only when GitHub Actions runs and rebuilds dashboard.html. Extended/high-risk names are saved in CSV but hidden from this decision view. Alpaca IEX volume is non-consolidated; confirm spread, liquidity, VWAP, and news manually before execution.
    </div>
</main>

</body>
</html>""")

    return page.safe_substitute(
        status=status,
        status_class=status_class,
        ny_time=now_ny.strftime("%Y-%m-%d %H:%M"),
        utc_time=now_utc.strftime("%Y-%m-%d %H:%M"),
        regime_html=regime_html,
        sector_snapshot=sector_snapshot,
        kpi_html=kpi_html,
        potential_section=potential_section,
        active_section=active_section,
        desk_table=desk_table,
    )


# ==============================================================
# MAIN
# ==============================================================

def main():
    print("\n" + "=" * 70)
    print("BUILDING ELITE PRO DESK DASHBOARD")
    print("=" * 70)

    potential = load_csv_records("potential_movers.csv", limit=10)
    active = load_csv_records("active_momentum.csv", limit=10)
    extended = load_csv_records("extended_movers.csv", limit=10)
    highrisk = load_csv_records("high_risk_movers.csv", limit=10)
    raw = load_csv_records("elite_watchlist_raw.csv")
    active_watchlist = load_csv_records("elite_watchlist.csv", limit=10)

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
