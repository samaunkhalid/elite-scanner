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

Features:
  - Professional bucketed display
  - Catalyst strip on every card
  - News headline/risk flags
  - Desk View table
  - Market regime banner
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
# SECTOR MAPPING
# ==============================================================

SECTORS = {
    "RIOT": "Crypto", "MARA": "Crypto", "CLSK": "Crypto", "HUT": "Crypto",
    "BITF": "Crypto", "CIFR": "Crypto", "CORZ": "Crypto", "BTBT": "Crypto",
    "IREN": "Crypto", "MSTR": "Crypto", "COIN": "Crypto",

    "SOUN": "AI", "AI": "AI", "BBAI": "AI", "IONQ": "Quantum",
    "RGTI": "Quantum", "ARQQ": "Quantum", "PLTR": "AI", "NVDA": "AI",
    "SMCI": "AI",

    "AMD": "Semis", "INTC": "Semis", "WOLF": "Semis", "LSCC": "Semis",
    "MU": "Semis", "QCOM": "Semis", "ARM": "Semis", "SMTC": "Semis",
    "MRVL": "Semis", "AVGO": "Semis", "TSM": "Semis", "GFS": "Semis",
    "AMKR": "Semis",

    "TSLA": "EV", "RIVN": "EV", "NIO": "EV", "XPEV": "EV", "LCID": "EV",
    "LI": "EV", "QS": "EV", "CHPT": "EV", "PLUG": "EV", "FCEL": "EV",
    "BE": "EV", "BLNK": "EV", "EVGO": "EV", "RUN": "Solar",

    "RKLB": "Space", "ASTS": "Space", "LUNR": "Space", "JOBY": "Mobility",
    "ACHR": "Mobility", "KTOS": "Defense", "LMT": "Defense", "RTX": "Defense",

    "HIMS": "Biotech", "CRSP": "Biotech", "BNGO": "Biotech", "VKTX": "Biotech",
    "MDGL": "Biotech", "VRDN": "Biotech", "CYTK": "Biotech", "IOVA": "Biotech",
    "SAVA": "Biotech", "MRNA": "Biotech", "NVAX": "Biotech", "QURE": "Biotech",

    "GME": "Retail", "AMC": "Meme", "BBBY": "Meme", "BB": "Meme", "NOK": "Meme",
    "HOOD": "Fintech", "SOFI": "Fintech", "AFRM": "Fintech", "UPST": "Fintech",
    "NU": "Fintech",

    "RDDT": "Social", "PINS": "Social", "SNAP": "Social", "RBLX": "Gaming",
    "ROKU": "Streaming", "DKNG": "Gaming", "NFLX": "Streaming",

    "NET": "Cyber", "CRWD": "Cyber", "ZS": "Cyber", "PANW": "Cyber", "OKTA": "Cyber",

    "DVN": "Energy", "CTRA": "Energy", "XOM": "Energy", "CVX": "Energy",
    "TECK": "Materials",

    "CCL": "Travel", "ABNB": "Travel", "CART": "Retail", "CAVA": "Consumer",
    "CELH": "Consumer", "SHOP": "E-Commerce",

    "AAPL": "Tech", "MSFT": "Tech", "GOOGL": "Tech", "META": "Tech", "AMZN": "Tech",
}


def get_sector(symbol):
    return SECTORS.get(str(symbol).upper(), "Other")


# ==============================================================
# SAFE HELPERS
# ==============================================================

def safe_str(value, default=""):
    if value is None:
        return default
    if isinstance(value, float) and pd.isna(value):
        return default
    text = str(value)
    if text.lower() in ["nan", "none"]:
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


def build_tags(stock, max_tags=4):
    tags = safe_str(stock.get("tags"), "")
    if not tags:
        return ""

    parts = [p.strip() for p in tags.split(" · ") if p.strip()]
    html_parts = []

    for tag in parts[:max_tags]:
        html_parts.append(f'<span class="tag">{esc(tag)}</span>')

    return "".join(html_parts)


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

    sector = get_sector(symbol)
    tier_color = get_tier_color(tier)
    change_class = "positive" if change_pct >= 0 else "negative"
    change_sign = "+" if change_pct >= 0 else ""

    dollar_vol = format_money_m(stock.get("dollar_vol_M"))
    atr = safe_float(stock.get("atr_pct"), 0)
    vwap_dist = safe_float(stock.get("vwap_dist_pct"), 0)
    short_pct = safe_float(stock.get("short_pct"), 0)
    float_m = safe_float(stock.get("float_M"), 0)
    days_to_cover = safe_float(stock.get("days_to_cover"), 0)

    above_vwap = truthy(stock.get("above_vwap"))
    near_hod = truthy(stock.get("near_hod"))

    vwap_text = "Above VWAP" if above_vwap else "Below/Unknown"
    hod_text = "Near HOD" if near_hod else "Not HOD"

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
            <div>
                <div class="symbol-row">
                    <span class="symbol">{esc(symbol)}</span>
                    <span class="sector">{esc(sector)}</span>
                    <span class="tier" style="color:{tier_color};border-color:{tier_color};">Tier {esc(tier)}</span>
                </div>
                <div class="bucket-label">{esc(bucket_meta['label'])}</div>
            </div>
            <div class="score-box" style="color:{tier_color};">{score}</div>
        </div>

        <div class="price-row">
            <div class="price">${price:.2f}</div>
            <div class="change {change_class}">{change_sign}{change_pct:.2f}%</div>
        </div>

        <div class="catalyst-strip {catalyst['class']}">
            <div class="catalyst-label">{catalyst['icon']} Catalyst: {esc(catalyst['label'])}</div>
            {headline_html}
            {catalyst_source_line}
            {risk_flags_html}
        </div>

        <div class="metrics-grid">
            <div><span>Liquidity</span><strong>{dollar_vol}</strong></div>
            <div><span>ATR</span><strong>{atr:.1f}%</strong></div>
            <div><span>VWAP Dist</span><strong>{vwap_dist:+.1f}%</strong></div>
            <div><span>HOD</span><strong>{esc(hod_text)}</strong></div>
        </div>

        <div class="status-row">
            <span class="status-chip">{esc(vwap_text)}</span>
            <span class="status-chip">{esc(hod_text)}</span>
            <span class="status-chip risk-chip">{esc(risk)}</span>
        </div>

        <div class="interpretation">{esc(bucket_meta['interpretation'])}</div>

        <div class="tags-row">{tags_html}</div>

        {squeeze_html}

        <div class="card-actions">
            <a href="https://www.tradingview.com/chart/?symbol={esc(symbol)}" target="_blank">Chart</a>
            <a href="https://finance.yahoo.com/quote/{esc(symbol)}" target="_blank">Yahoo</a>
            <a href="https://stocktwits.com/symbol/{esc(symbol)}" target="_blank">Twits</a>
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
    total_focus = len(active_watchlist)
    avg_score = 0

    if active_watchlist:
        avg_score = sum(safe_float(s.get("score"), 0) for s in active_watchlist) / len(active_watchlist)

    return f"""
    <div class="kpi-grid">
        <div class="kpi-card focus"><span>{total_focus}</span><label>Active Watchlist</label></div>
        <div class="kpi-card potential"><span>{len(potential)}</span><label>Potential</label></div>
        <div class="kpi-card active"><span>{len(active)}</span><label>Active Momentum</label></div>
        <div class="kpi-card extended"><span>{len(extended)}</span><label>Extended</label></div>
        <div class="kpi-card risk"><span>{len(highrisk)}</span><label>High Risk</label></div>
        <div class="kpi-card"><span>{len(raw)}</span><label>Raw Scored</label></div>
        <div class="kpi-card"><span>{avg_score:.0f}</span><label>Avg Score</label></div>
    </div>
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
        catalyst = get_catalyst_meta(stock)
        headline = catalyst["headline"][:85] if catalyst["headline"] else "—"

        html_rows += f"""
        <tr>
            <td><strong>{esc(symbol)}</strong></td>
            <td>{score}</td>
            <td>{esc(tier)}</td>
            <td>{esc(bucket)}</td>
            <td>${price:.2f}</td>
            <td class="{'positive' if chg >= 0 else 'negative'}">{chg:+.2f}%</td>
            <td>{liq}</td>
            <td>{atr:.1f}%</td>
            <td>{esc(vwap)}</td>
            <td>{esc(risk)}</td>
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
                        <th>Catalyst</th>
                        <th>Headline</th>
                    </tr>
                </thead>
                <tbody>
                    {html_rows}
                </tbody>
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

    if minutes < 9 * 60 + 30:
        return "Pre-Market", "status-blue"
    if minutes < 11 * 60 + 30:
        return "Prime Window", "status-green"
    if minutes < 14 * 60 + 30:
        return "Lunch Lull", "status-yellow"
    if minutes < 16 * 60:
        return "Afternoon", "status-green"
    return "Closed", "status-gray"


# ==============================================================
# HTML BUILDER
# ==============================================================

def build_dashboard(potential, active, extended, highrisk, raw, active_watchlist, regime):
    now_utc, now_ny = get_times()
    status, status_class = get_market_status(now_ny)

    regime_html = build_regime_html(regime)
    kpi_html = build_kpi_row(potential, active, extended, highrisk, raw, active_watchlist)

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

    extended_section = build_section(
        "Extended / Chase Risk",
        "Already stretched. Avoid chasing; use only as monitor list.",
        extended,
        "section-extended",
        max_cards=10,
    )

    highrisk_section = build_section(
        "High Risk / Extreme",
        "Volatile or news-risk names. Monitor only unless intentionally trading high-risk momentum.",
        highrisk,
        "section-risk",
        max_cards=10,
    )

    desk_rows = []
    desk_rows.extend(potential[:10])
    desk_rows.extend(active[:10])
    desk_rows.extend(extended[:10])
    desk_rows.extend(highrisk[:10])

    desk_table = build_desk_table(desk_rows)

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
        radial-gradient(circle at top right, rgba(16, 185, 129, 0.10), transparent 28%),
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

.status-pill {
    padding: 5px 11px;
    border-radius: 999px;
    font-weight: 650;
    border: 1px solid rgba(148, 163, 184, 0.18);
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
.desk-table-section {
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

.sector,
.tier {
    font-size: 10px;
    font-weight: 500;
    padding: 2px 6px;
    border-radius: 999px;
    border: 1px solid rgba(148, 163, 184, 0.18);
    color: #94a3b8;
}

.bucket-label {
    color: #94a3b8;
    font-size: 12px;
    font-weight: 400;
    margin-top: 5px;
    line-height: 1.35;
}

.score-box {
    font-size: 25px;
    font-weight: 850;
}

.price-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-top: 12px;
    padding-bottom: 10px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.10);
}

.price {
    font-size: 18px;
    font-weight: 750;
}

.change {
    font-size: 14px;
    font-weight: 700;
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
    border-color: rgba(59, 130, 246, 0.35);
    background: rgba(59, 130, 246, 0.08);
    color: #93c5fd;
}

.catalyst-none {
    border-color: rgba(148, 163, 184, 0.16);
    background: rgba(148, 163, 184, 0.05);
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

.status-row {
    display: flex;
    gap: 7px;
    flex-wrap: wrap;
    margin-top: 12px;
}

.status-chip,
.tag {
    background: rgba(59, 130, 246, 0.10);
    color: #93c5fd;
    border: 1px solid rgba(59, 130, 246, 0.20);
    border-radius: 999px;
    padding: 4px 9px;
    font-size: 10px;
    font-weight: 600;
    line-height: 1.2;
}

.risk-chip {
    background: rgba(148, 163, 184, 0.08);
    color: #cbd5e1;
    border-color: rgba(148, 163, 184, 0.16);
}

.interpretation {
    margin-top: 12px;
    background: rgba(56, 189, 248, 0.06);
    border: 1px solid rgba(56, 189, 248, 0.10);
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
    background: rgba(239, 68, 68, 0.06);
    border: 1px solid rgba(239, 68, 68, 0.12);
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
    color: #e5e7eb;
    font-weight: 650;
}

.card-actions {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-top: 14px;
}

.card-actions a {
    text-align: center;
    color: #cbd5e1;
    background: rgba(15, 23, 42, 0.9);
    border: 1px solid rgba(148, 163, 184, 0.12);
    border-radius: 7px;
    padding: 8px 8px;
    text-decoration: none;
    font-size: 11px;
}

.card-actions a:hover {
    background: rgba(30, 41, 59, 0.9);
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
}
</style>
</head>
<body>

<header class="header">
    <div class="header-inner">
        <div class="title">
            <h1>Elite Scanner — Pro Desk</h1>
            <p>Technical Potential Movers + Catalyst Confirmation + News Risk Filter</p>
        </div>
        <div class="header-meta">
            <span class="status-pill $status_class">$status</span>
            <span>NY: $ny_time</span>
            <span>UTC: $utc_time</span>
        </div>
    </div>
</header>

<main class="container">
    $regime_html
    $kpi_html

    <div class="nav-tabs">
        <a href="#potential">Potential Movers</a>
        <a href="#active">Active Momentum</a>
        <a href="#extended">Extended Risk</a>
        <a href="#risk">High Risk</a>
        <a href="#desk">Desk View</a>
    </div>

    <div id="potential">$potential_section</div>
    <div id="active">$active_section</div>
    <div id="extended">$extended_section</div>
    <div id="risk">$highrisk_section</div>
    <div id="desk">$desk_table</div>

    <div class="footer-note">
        Data note: Alpaca IEX volume is non-consolidated. Confirm liquidity, spread, VWAP, and news manually before trade execution.
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
        kpi_html=kpi_html,
        potential_section=potential_section,
        active_section=active_section,
        extended_section=extended_section,
        highrisk_section=highrisk_section,
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

    # Fallback if bucket files do not exist yet
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
    print(f"  Extended / Chase Risk:  {len(extended)}")
    print(f"  High Risk / Extreme:    {len(highrisk)}")
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
