
'''
ELITE SCANNER DASHBOARD — PRO DESK VIEW
=======================================
Professional hedge-fund-style dashboard for the Elite Scanner.

Reads scanner output:
  - market_regime.json
  - potential_movers.csv
  - active_momentum.csv
  - extended_movers.csv
  - high_risk_movers.csv
  - elite_watchlist_raw.csv

Writes:
  - dashboard.html
'''

import csv
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo


# ==============================================================
# BASIC HELPERS
# ==============================================================

SECTORS = {
    "AMD": "Semis", "INTC": "Semis", "QCOM": "Semis", "NVDA": "Semis",
    "HIMX": "Semis", "MKSI": "Semis", "FORM": "Semis", "PENG": "Semis",
    "RKLB": "Space", "ASTS": "Space", "LUNR": "Space",
    "CORZ": "Crypto", "IREN": "Crypto", "MARA": "Crypto", "RIOT": "Crypto",
    "COIN": "Crypto", "MSTR": "Crypto",
    "CYTK": "Biotech", "MIRM": "Biotech", "NVAX": "Biotech", "MRNA": "Biotech",
    "GRPN": "Retail", "ARLO": "Consumer", "MNST": "Consumer", "GEN": "Consumer",
    "FTNT": "Cyber", "PANW": "Cyber", "CRWD": "Cyber", "NET": "Cyber",
    "DOCN": "Software", "DBX": "Software", "FROG": "Software",
    "CPAY": "Fintech", "HOOD": "Fintech", "SOFI": "Fintech", "AFRM": "Fintech",
}


def sector(symbol):
    return SECTORS.get(str(symbol).upper(), "Other")


def fnum(value, default=0.0):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    value = str(value).strip()
    if value in ("", "—", "None", "nan", "NaN"):
        return default
    try:
        return float(value)
    except Exception:
        return default


def fbool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "y")


def money_m(value):
    value = fnum(value)
    if value >= 1000:
        return f"${value/1000:.1f}B"
    if value >= 100:
        return f"${value:.0f}M"
    if value > 0:
        return f"${value:.1f}M"
    return "—"


def pct(value, digits=2):
    value = fnum(value)
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.{digits}f}%"


def clean_row(row):
    out = dict(row)
    numeric = [
        "price", "live_price", "change_pct", "score", "base_score", "catalyst",
        "momentum", "execution", "squeeze", "strength", "technical", "participation",
        "social", "short_pct", "float_M", "days_to_cover", "atr_pct", "dollar_vol_M",
        "market_cap_B", "intraday_score", "vwap", "vwap_dist_pct", "hod", "lod",
        "from_hod_pct", "range_position", "recent_range_pct", "intraday_volume",
        "bar_count",
    ]
    for key in numeric:
        if key in out:
            out[key] = fnum(out.get(key))
    for key in ["above_vwap", "near_hod", "is_earnings_reaction"]:
        if key in out:
            out[key] = fbool(out.get(key))
    out["symbol"] = str(out.get("symbol", "")).upper().strip()
    out["tier"] = str(out.get("tier", "—")).strip()
    out["setup_bucket"] = str(out.get("setup_bucket", "MONITOR")).strip()
    out["risk_category"] = str(out.get("risk_category", "NORMAL")).strip()
    out["tags"] = str(out.get("tags", "") or "")
    return out


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return [clean_row(r) for r in csv.DictReader(f) if r.get("symbol")]


def read_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_data():
    return {
        "potential": read_csv("potential_movers.csv"),
        "active": read_csv("active_momentum.csv"),
        "extended": read_csv("extended_movers.csv"),
        "highrisk": read_csv("high_risk_movers.csv"),
        "raw": read_csv("elite_watchlist_raw.csv"),
        "regime": read_json("market_regime.json"),
    }


def score_color(score):
    score = fnum(score)
    if score >= 80:
        return "#fbbf24"
    if score >= 65:
        return "#22c55e"
    if score >= 50:
        return "#38bdf8"
    if score >= 35:
        return "#94a3b8"
    return "#64748b"


def bucket_info(bucket):
    return {
        "POTENTIAL_MOVER": ("Primary Focus — Potential Movers", "Clean, earlier continuation setups. Review these first.", "#38bdf8", "potential"),
        "ACTIVE_MOMENTUM": ("Active Momentum", "Already moving. Wait for controlled pullback or tight continuation.", "#22c55e", "active"),
        "EXTENDED_CHASE_RISK": ("Extended / Chase Risk", "Strong movers but stretched. Avoid chasing without a reset.", "#f59e0b", "extended"),
        "HIGH_RISK_EXTREME": ("High Risk / Extreme", "Parabolic or high-risk names. Watch-only unless using special rules.", "#ef4444", "highrisk"),
    }.get(bucket, (bucket.replace("_", " ").title(), "", "#94a3b8", "monitor"))


def tag_list(stock, limit=5):
    tags = stock.get("tags", "")
    if not tags:
        return []
    return [t.strip() for t in tags.split(" · ") if t.strip()][:limit]


def risk_badge(stock):
    risk = stock.get("risk_category", "NORMAL")
    if risk == "EXTENDED":
        return '<span class="badge orange">EXTENDED</span>'
    if risk in ("HIGH_RISK", "EXTREME_MOVE"):
        return '<span class="badge red">HIGH RISK</span>'
    return '<span class="badge muted">NORMAL</span>'


def vwap_badge(stock):
    if "above_vwap" not in stock:
        return '<span class="badge muted">VWAP —</span>'
    if stock.get("above_vwap"):
        dist = fnum(stock.get("vwap_dist_pct"))
        if dist > 8:
            return f'<span class="badge orange">VWAP +{dist:.1f}%</span>'
        return '<span class="badge green">Above VWAP</span>'
    return '<span class="badge red">Below VWAP</span>'


def interpretation(stock):
    bucket = stock.get("setup_bucket")
    tags = stock.get("tags", "")
    if bucket == "POTENTIAL_MOVER":
        if "Tight consolidation" in tags:
            return "Clean continuation watch: tight consolidation near highs."
        if "Consolidating" in tags:
            return "Potential continuation candidate; monitor breakout from consolidation."
        return "Potential mover with supportive intraday structure."
    if bucket == "ACTIVE_MOMENTUM":
        return "Momentum is active; avoid chasing. Prefer pullback or tight continuation."
    if bucket == "EXTENDED_CHASE_RISK":
        return "Already extended. Watch only unless it resets near VWAP or forms a new base."
    if bucket == "HIGH_RISK_EXTREME":
        return "Extreme mover. Do not treat as a primary entry setup."
    return "Monitor for structure improvement."


# ==============================================================
# HTML COMPONENTS
# ==============================================================

def build_card(stock):
    symbol = stock.get("symbol", "—")
    score = fnum(stock.get("score"))
    price = fnum(stock.get("live_price")) or fnum(stock.get("price"))
    change = fnum(stock.get("change_pct"))
    bucket = stock.get("setup_bucket", "MONITOR")
    _, _, accent, css_class = bucket_info(bucket)
    change_class = "positive" if change >= 0 else "negative"

    near_hod = '<span class="badge green">Near HOD</span>' if stock.get("near_hod") else '<span class="badge muted">HOD —</span>'
    base_badge = ""
    if "Tight consolidation" in stock.get("tags", ""):
        base_badge = '<span class="badge blue">Tight Base</span>'
    elif "Consolidating" in stock.get("tags", ""):
        base_badge = '<span class="badge blue">Consolidating</span>'

    tags_html = "".join(f'<span class="tag">{t}</span>' for t in tag_list(stock, 5))

    short_metric = ""
    if fnum(stock.get("short_pct")) >= 15:
        short_metric = f'''
        <div class="metric">
            <span>Short</span>
            <strong class="negative">{fnum(stock.get("short_pct")):.0f}%</strong>
        </div>
        '''

    return f'''
    <article class="card {css_class}" style="--accent:{accent};">
        <div class="card-top">
            <div>
                <div class="sym-row">
                    <span class="symbol">{symbol}</span>
                    <span class="sector">{sector(symbol)}</span>
                    <span class="tier" style="color:{score_color(score)};">Tier {stock.get("tier", "—")}</span>
                </div>
                <div class="bucket">{bucket.replace("_", " ")}</div>
            </div>
            <div class="score" style="color:{score_color(score)};">{score:.0f}<small>/100</small></div>
        </div>

        <div class="price-row">
            <div>
                <div class="price">${price:.2f}</div>
                <div class="source">{stock.get("data_source", "Yahoo / scanner")}</div>
            </div>
            <div class="change {change_class}">{pct(change)}</div>
        </div>

        <div class="badge-row">
            {risk_badge(stock)}
            {vwap_badge(stock)}
            {near_hod}
            {base_badge}
        </div>

        <div class="metrics">
            <div class="metric"><span>Liquidity</span><strong>{money_m(stock.get("dollar_vol_M"))}</strong></div>
            <div class="metric"><span>ATR</span><strong>{fnum(stock.get("atr_pct")):.1f}%</strong></div>
            <div class="metric"><span>VWAP Dist</span><strong>{fnum(stock.get("vwap_dist_pct")):.1f}%</strong></div>
            <div class="metric"><span>From HOD</span><strong>{fnum(stock.get("from_hod_pct")):.1f}%</strong></div>
            {short_metric}
        </div>

        <div class="note">{interpretation(stock)}</div>

        <div class="tags">{tags_html}</div>

        <div class="actions">
            <a href="https://www.tradingview.com/chart/?symbol={symbol}" target="_blank">Chart</a>
            <a href="https://finance.yahoo.com/quote/{symbol}" target="_blank">Yahoo</a>
            <a href="https://stocktwits.com/symbol/{symbol}" target="_blank">Twits</a>
        </div>
    </article>
    '''


def build_section(bucket, stocks, limit):
    title, subtitle, accent, _ = bucket_info(bucket)
    if stocks:
        cards = '<div class="grid">' + "\n".join(build_card(s) for s in stocks[:limit]) + "</div>"
    else:
        cards = '<div class="empty">No names in this bucket.</div>'

    return f'''
    <section id="{bucket.lower()}" class="section">
        <div class="section-head" style="--accent:{accent};">
            <div>
                <h2>{title}</h2>
                <p>{subtitle}</p>
            </div>
            <div class="count">{len(stocks)}</div>
        </div>
        {cards}
    </section>
    '''


def build_table(stocks):
    if not stocks:
        return ""
    rows = ""
    for s in stocks[:50]:
        chg = fnum(s.get("change_pct"))
        chg_class = "positive" if chg >= 0 else "negative"
        vwap = "Above" if s.get("above_vwap") else "Below" if "above_vwap" in s else "—"
        rows += f'''
        <tr>
            <td><strong>{s.get("symbol", "—")}</strong></td>
            <td>{fnum(s.get("score")):.0f}</td>
            <td>{s.get("setup_bucket", "—").replace("_", " ")}</td>
            <td>${fnum(s.get("price")):.2f}</td>
            <td class="{chg_class}">{pct(chg)}</td>
            <td>{money_m(s.get("dollar_vol_M"))}</td>
            <td>{fnum(s.get("atr_pct")):.1f}%</td>
            <td>{vwap}</td>
            <td>{s.get("risk_category", "NORMAL")}</td>
            <td>{", ".join(tag_list(s, 2))}</td>
        </tr>
        '''
    return f'''
    <section id="desk" class="section">
        <div class="section-head">
            <div>
                <h2>Desk View</h2>
                <p>Compact comparison table for fast manual review.</p>
            </div>
        </div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Ticker</th><th>Score</th><th>Bucket</th><th>Price</th>
                        <th>% Chg</th><th>Liq</th><th>ATR</th><th>VWAP</th><th>Risk</th><th>Notes</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </section>
    '''


def market_status():
    ny = datetime.now(ZoneInfo("America/New_York"))
    mins = ny.hour * 60 + ny.minute
    if mins < 4 * 60:
        return "Closed", "#64748b", ny
    if mins < 9 * 60 + 30:
        return "Pre-Market", "#38bdf8", ny
    if mins < 10 * 60 + 30:
        return "Opening Drive", "#22c55e", ny
    if mins < 11 * 60 + 30:
        return "Trend Window", "#22c55e", ny
    if mins < 13 * 60 + 30:
        return "Midday", "#f59e0b", ny
    if mins < 16 * 60:
        return "Afternoon", "#22c55e", ny
    if mins < 20 * 60:
        return "After Hours", "#8b5cf6", ny
    return "Closed", "#64748b", ny


def regime_html(regime, enriched, raw_count):
    label = regime.get("label", "Market regime unavailable") if regime else "Market regime unavailable"
    bias = regime.get("bias", "NEUTRAL") if regime else "NEUTRAL"
    spy = fnum(regime.get("spy_change", 0) if regime else 0)
    qqq = fnum(regime.get("qqq_change", 0) if regime else 0)
    iwm = fnum(regime.get("iwm_change", 0) if regime else 0)
    vix = fnum(regime.get("vix_level", 0) if regime else 0)

    accent = "#22c55e" if bias == "LONG_FAVORED" else "#ef4444" if bias == "SHORT_FAVORED" else "#f59e0b" if bias == "CAUTION" else "#94a3b8"

    return f'''
    <div class="regime" style="--accent:{accent};">
        <div>
            <div class="regime-title">{label}</div>
            <div class="regime-sub">Bias: {bias.replace("_", " ")} · Data: Yahoo + Alpaca IEX · Price Filter: $5–$80</div>
        </div>
        <div class="tape">
            <span>SPY <b class="{'positive' if spy >= 0 else 'negative'}">{pct(spy)}</b></span>
            <span>QQQ <b class="{'positive' if qqq >= 0 else 'negative'}">{pct(qqq)}</b></span>
            <span>IWM <b class="{'positive' if iwm >= 0 else 'negative'}">{pct(iwm)}</b></span>
            <span>VIX <b>{vix:.1f}</b></span>
            <span>IEX <b>{enriched}</b></span>
            <span>Raw <b>{raw_count}</b></span>
        </div>
    </div>
    '''


def build_dashboard(data):
    potential = sorted(data["potential"], key=lambda x: fnum(x.get("score")), reverse=True)
    active = sorted(data["active"], key=lambda x: fnum(x.get("score")), reverse=True)
    extended = sorted(data["extended"], key=lambda x: fnum(x.get("score")), reverse=True)
    highrisk = sorted(data["highrisk"], key=lambda x: fnum(x.get("score")), reverse=True)
    raw = data["raw"]
    regime = data["regime"]

    all_stocks = potential + active + extended + highrisk
    enriched = len([s for s in all_stocks if s.get("data_source")])
    avg_score = sum(fnum(s.get("score")) for s in all_stocks) / len(all_stocks) if all_stocks else 0

    status, status_color, ny = market_status()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    kpis = [
        ("Potential", len(potential), "#38bdf8"),
        ("Active Mom.", len(active), "#22c55e"),
        ("Extended", len(extended), "#f59e0b"),
        ("High Risk", len(highrisk), "#ef4444"),
        ("Raw Universe", len(raw), "#94a3b8"),
        ("Avg Score", f"{avg_score:.0f}", "#f8fafc"),
    ]

    kpi_html = ""
    for label, value, color in kpis:
        kpi_html += f'<div class="kpi"><div style="color:{color};">{value}</div><span>{label}</span></div>'

    sections = ""
    sections += build_section("POTENTIAL_MOVER", potential, 12)
    sections += build_section("ACTIVE_MOMENTUM", active, 8)
    sections += build_section("EXTENDED_CHASE_RISK", extended, 8)
    sections += build_section("HIGH_RISK_EXTREME", highrisk, 8)
    desk = build_table(all_stocks)

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="300">
<title>Elite Scanner — Pro Desk</title>
<style>
:root {{
    --bg:#080b10; --panel:#101620; --panel2:#0d121a; --border:#1f2937;
    --text:#e5e7eb; --muted:#94a3b8; --muted2:#64748b;
    --green:#22c55e; --red:#ef4444; --blue:#38bdf8; --orange:#f59e0b;
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{
    background: radial-gradient(circle at top left, rgba(56,189,248,.08), transparent 30%),
                radial-gradient(circle at top right, rgba(34,197,94,.05), transparent 25%),
                var(--bg);
    color:var(--text);
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
a {{ color:inherit; }}
.positive {{ color:var(--green); }} .negative {{ color:var(--red); }}
.topbar {{
    position:sticky; top:0; z-index:50; border-bottom:1px solid var(--border);
    background:rgba(8,11,16,.92); backdrop-filter:blur(14px);
}}
.topbar-inner {{
    max-width:1540px; margin:0 auto; padding:18px 24px;
    display:flex; justify-content:space-between; gap:18px; align-items:center;
}}
.brand h1 {{ font-size:22px; letter-spacing:-.03em; }}
.brand p {{ color:var(--muted); font-size:12px; margin-top:4px; }}
.status {{
    padding:8px 14px; border:1px solid {status_color}; color:{status_color};
    border-radius:999px; font-size:12px; font-weight:800; text-align:center;
}}
.time {{ color:var(--muted); font-size:12px; text-align:right; margin-top:4px; }}
.container {{ max-width:1540px; margin:0 auto; padding:24px; }}
.regime {{
    border:1px solid var(--border); border-left:4px solid var(--accent);
    background:linear-gradient(135deg, rgba(16,22,32,.96), rgba(13,18,26,.96));
    border-radius:16px; padding:18px 20px; display:flex; justify-content:space-between;
    gap:16px; align-items:center; margin-bottom:18px;
}}
.regime-title {{ font-size:17px; font-weight:900; }}
.regime-sub {{ color:var(--muted); font-size:12px; margin-top:4px; }}
.tape {{ display:flex; gap:10px; flex-wrap:wrap; justify-content:flex-end; }}
.tape span {{
    border:1px solid var(--border); background:rgba(15,23,42,.7);
    border-radius:999px; padding:7px 10px; color:var(--muted); font-size:12px;
}}
.tape b {{ margin-left:4px; }}
.kpis {{ display:grid; grid-template-columns:repeat(6,minmax(120px,1fr)); gap:12px; margin-bottom:22px; }}
.kpi {{ border:1px solid var(--border); background:rgba(16,22,32,.85); border-radius:14px; padding:15px; }}
.kpi div {{ font-size:26px; font-weight:900; letter-spacing:-.03em; }}
.kpi span {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; margin-top:5px; display:block; }}
.nav {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:24px; }}
.nav a {{
    text-decoration:none; border:1px solid var(--border); background:rgba(15,23,42,.65);
    color:var(--muted); border-radius:999px; padding:9px 13px; font-size:12px; font-weight:800;
}}
.nav a:hover {{ color:var(--text); border-color:#334155; }}
.section {{ margin-bottom:32px; }}
.section-head {{
    --accent:var(--blue); border:1px solid var(--border); border-left:4px solid var(--accent);
    background:rgba(16,22,32,.78); padding:16px 18px; border-radius:14px;
    display:flex; justify-content:space-between; gap:12px; align-items:center; margin-bottom:14px;
}}
.section-head h2 {{ font-size:18px; letter-spacing:-.02em; }}
.section-head p {{ color:var(--muted); font-size:12px; margin-top:4px; }}
.count {{
    border:1px solid var(--border); background:rgba(15,23,42,.9); border-radius:12px;
    min-width:46px; text-align:center; padding:10px 12px; font-weight:900;
}}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(390px,1fr)); gap:14px; }}
.card {{
    --accent:var(--blue); border:1px solid var(--border); border-top:3px solid var(--accent);
    background:linear-gradient(180deg, rgba(16,22,32,.96), rgba(10,15,22,.96));
    border-radius:16px; padding:16px; box-shadow:0 10px 30px rgba(0,0,0,.18);
}}
.card:hover {{ border-color:#334155; transform:translateY(-1px); transition:.15s ease; }}
.card-top {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
.sym-row {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
.symbol {{ font-size:25px; font-weight:950; letter-spacing:-.04em; }}
.sector,.tier {{
    border:1px solid var(--border); background:rgba(15,23,42,.8); border-radius:999px;
    padding:4px 8px; font-size:11px; font-weight:850;
}}
.bucket {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.08em; margin-top:5px; }}
.score {{ min-width:62px; text-align:right; font-size:27px; font-weight:950; }}
.score small {{ color:var(--muted2); font-size:11px; }}
.price-row {{
    display:flex; justify-content:space-between; align-items:end; margin-top:16px;
    padding-bottom:14px; border-bottom:1px solid var(--border);
}}
.price {{ font-size:22px; font-weight:900; }}
.source {{ color:var(--muted2); font-size:11px; margin-top:2px; }}
.change {{ font-size:17px; font-weight:900; }}
.badge-row {{ display:flex; flex-wrap:wrap; gap:7px; margin:13px 0; }}
.badge {{
    border-radius:999px; padding:5px 8px; font-size:10px; font-weight:900;
    letter-spacing:.04em; border:1px solid transparent;
}}
.green {{ background:rgba(34,197,94,.12); color:#86efac; border-color:rgba(34,197,94,.25); }}
.red {{ background:rgba(239,68,68,.12); color:#fca5a5; border-color:rgba(239,68,68,.25); }}
.orange {{ background:rgba(245,158,11,.12); color:#fcd34d; border-color:rgba(245,158,11,.25); }}
.blue {{ background:rgba(56,189,248,.12); color:#7dd3fc; border-color:rgba(56,189,248,.25); }}
.muted {{ background:rgba(148,163,184,.10); color:#cbd5e1; border-color:rgba(148,163,184,.20); }}
.metrics {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin-bottom:13px; }}
.metric {{
    border:1px solid var(--border); background:rgba(8,13,20,.75); border-radius:10px;
    padding:9px 10px; display:flex; justify-content:space-between; gap:8px; font-size:12px;
}}
.metric span {{ color:var(--muted); }} .metric strong {{ color:var(--text); }}
.note {{
    border:1px solid rgba(56,189,248,.16); background:rgba(56,189,248,.06);
    color:#dbeafe; border-radius:11px; padding:10px 11px; font-size:12px;
    line-height:1.35; margin-bottom:12px;
}}
.tags {{ display:flex; flex-wrap:wrap; gap:6px; min-height:25px; }}
.tag {{
    background:rgba(99,102,241,.11); color:#c4b5fd; border:1px solid rgba(99,102,241,.22);
    border-radius:999px; padding:4px 8px; font-size:10px; font-weight:800;
}}
.actions {{ display:flex; gap:8px; margin-top:14px; }}
.actions a {{
    flex:1; text-align:center; text-decoration:none; border:1px solid var(--border);
    background:rgba(15,23,42,.78); color:var(--muted); border-radius:9px;
    padding:8px; font-size:11px; font-weight:850;
}}
.actions a:hover {{ color:var(--text); border-color:#334155; }}
.empty {{ border:1px dashed #334155; border-radius:14px; padding:28px; color:var(--muted); text-align:center; }}
.table-wrap {{ overflow-x:auto; border:1px solid var(--border); border-radius:14px; background:rgba(16,22,32,.72); }}
table {{ width:100%; border-collapse:collapse; font-size:12px; }}
th,td {{ padding:11px 12px; border-bottom:1px solid var(--border); text-align:left; white-space:nowrap; }}
th {{ color:var(--muted); font-size:10px; letter-spacing:.08em; text-transform:uppercase; background:rgba(15,23,42,.95); }}
td {{ color:#dbe4ef; }} tr:hover td {{ background:rgba(30,41,59,.35); }}
.footer {{ color:var(--muted2); font-size:11px; text-align:center; margin:28px 0 10px; }}
@media (max-width:920px) {{
    .topbar-inner,.regime {{ flex-direction:column; align-items:flex-start; }}
    .tape {{ justify-content:flex-start; }}
    .kpis {{ grid-template-columns:repeat(2,1fr); }}
    .grid {{ grid-template-columns:1fr; }}
}}
</style>
</head>
<body>
<header class="topbar">
    <div class="topbar-inner">
        <div class="brand">
            <h1>Elite Scanner — Pro Desk</h1>
            <p>Potential movers first · Bucketed by setup quality · Updated {now}</p>
        </div>
        <div>
            <div class="status">{status}</div>
            <div class="time">NY {ny.strftime("%H:%M:%S")}</div>
        </div>
    </div>
</header>

<main class="container">
    {regime_html(regime, enriched, len(raw))}

    <div class="kpis">{kpi_html}</div>

    <nav class="nav">
        <a href="#potential_mover">Potential Movers</a>
        <a href="#active_momentum">Active Momentum</a>
        <a href="#extended_chase_risk">Extended Risk</a>
        <a href="#high_risk_extreme">High Risk</a>
        <a href="#desk">Desk View</a>
    </nav>

    {sections}
    {desk}

    <div class="footer">
        Data note: Alpaca Free uses IEX-only, non-consolidated data. Volume and VWAP are useful proxies, not full SIP market data.
    </div>
</main>
</body>
</html>'''


def main():
    print("\n" + "=" * 60)
    print("BUILDING ELITE DASHBOARD — PRO DESK")
    print("=" * 60)

    data = load_data()
    total = len(data["potential"]) + len(data["active"]) + len(data["extended"]) + len(data["highrisk"])

    if total == 0:
        print("  ⚠ No bucketed scanner output found.")
        print("  Run elite_scanner.py first.")
        return

    print(f"  Potential Movers:     {len(data['potential'])}")
    print(f"  Active Momentum:      {len(data['active'])}")
    print(f"  Extended / Chase:     {len(data['extended'])}")
    print(f"  High Risk / Extreme:  {len(data['highrisk'])}")
    print(f"  Raw Universe:         {len(data['raw'])}")

    html = build_dashboard(data)

    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(html)

    print("  ✓ Dashboard saved to dashboard.html")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
