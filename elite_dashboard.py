"""
ELITE SCANNER DASHBOARD
========================
Rich UI showing tier badges, conviction tags, score breakdown
Reads elite_watchlist.json and renders dashboard.html

Designed for both desktop and mobile viewing.
"""

import json
import os
from datetime import datetime, timedelta

# ==============================================================
# SECTOR MAPPING
# ==============================================================

SECTORS = {
    # Crypto Mining
    "RIOT":"Crypto","MARA":"Crypto","CLSK":"Crypto","HUT":"Crypto","BITF":"Crypto",
    "CIFR":"Crypto","CORZ":"Crypto","BTBT":"Crypto","IREN":"Crypto","MSTR":"Crypto","COIN":"Crypto",
    # AI / Quantum
    "SOUN":"AI","AI":"AI","BBAI":"AI","IONQ":"Quantum","RGTI":"Quantum","ARQQ":"Quantum",
    "PLTR":"AI","NVDA":"AI","SMCI":"AI",
    # Semis
    "AMD":"Semis","INTC":"Semis","WOLF":"Semis","LSCC":"Semis","MU":"Semis","QCOM":"Semis",
    "ARM":"Semis","SMTC":"Semis","MRVL":"Semis","AVGO":"Semis","TSM":"Semis",
    # EV / Battery
    "TSLA":"EV","RIVN":"EV","NIO":"EV","XPEV":"EV","LCID":"EV","LI":"EV","QS":"EV",
    "CHPT":"EV","PLUG":"EV","FCEL":"EV","BE":"EV","BLNK":"EV","EVGO":"EV",
    # Space / Defense
    "RKLB":"Space","ASTS":"Space","LUNR":"Space","JOBY":"Mobility","ACHR":"Mobility",
    "KTOS":"Defense","LMT":"Defense","RTX":"Defense",
    # Biotech
    "HIMS":"Biotech","CRSP":"Biotech","BNGO":"Biotech","VKTX":"Biotech","MDGL":"Biotech",
    "VRDN":"Biotech","CYTK":"Biotech","IOVA":"Biotech","SAVA":"Biotech","MRNA":"Biotech",
    # Squeeze / Meme
    "GME":"Meme","AMC":"Meme","BBBY":"Meme","BB":"Meme","NOK":"Meme",
    # Fintech
    "HOOD":"Fintech","SOFI":"Fintech","AFRM":"Fintech","UPST":"Fintech","NU":"Fintech",
    # Social / Streaming
    "RDDT":"Social","PINS":"Social","SNAP":"Social","RBLX":"Gaming","ROKU":"Streaming",
    "DKNG":"Gaming","NFLX":"Streaming",
    # Cybersecurity
    "NET":"Cyber","CRWD":"Cyber","ZS":"Cyber","PANW":"Cyber","OKTA":"Cyber",
    # Energy
    "DVN":"Energy","CTRA":"Energy","XOM":"Energy","CVX":"Energy",
    # Retail / Consumer
    "GME":"Retail","CCL":"Travel","ABNB":"Travel","CART":"Retail","CAVA":"Consumer",
    "CELH":"Consumer","SHOP":"E-Commerce",
    # Mega-cap Tech
    "AAPL":"Tech","MSFT":"Tech","GOOGL":"Tech","META":"Tech","AMZN":"Tech",
}

def get_sector(symbol):
    return SECTORS.get(symbol, "Other")


# ==============================================================
# SETUP TYPE INFERENCE (from layer scores)
# ==============================================================

def get_setup_type(stock):
    """Determine primary setup type based on which layers scored highest."""
    scores = {
        "🧨 SQUEEZE PLAY": stock["squeeze"],
        "📅 CATALYST EVENT": stock["catalyst"],
        "💰 SMART MONEY": stock["smart_money"],
        "📞 OPTIONS FLOW": stock["options"],
        "🐦 SOCIAL MOMENTUM": stock["social"],
        "💪 RELATIVE STRENGTH": stock["strength"],
        "📈 BREAKOUT": stock["technical"],
    }
    # Top scoring layer determines setup type
    primary = max(scores.items(), key=lambda x: x[1])
    if primary[1] == 0:
        return "—"
    return primary[0]


# ==============================================================
# HTML BUILDER
# ==============================================================

def get_tier_color(tier):
    return {
        "S": "#fbbf24",    # gold
        "1": "#10b981",    # green
        "2": "#3b82f6",    # blue
        "3": "#6b7280",    # gray
    }.get(tier, "#6b7280")

def get_tier_bg(tier):
    return {
        "S": "rgba(251, 191, 36, 0.12)",
        "1": "rgba(16, 185, 129, 0.10)",
        "2": "rgba(59, 130, 246, 0.08)",
        "3": "rgba(107, 114, 128, 0.05)",
    }.get(tier, "rgba(107, 114, 128, 0.05)")


def build_card(stock):
    """Build a card for a single stock."""
    tier = stock["tier"]
    setup = get_setup_type(stock)
    sector = get_sector(stock["symbol"])
    
    change_color = "#10b981" if stock["change_pct"] >= 0 else "#ef4444"
    sign = "+" if stock["change_pct"] >= 0 else ""
    
    # Build tag pills from text
    tags_html = ""
    if stock.get("tags"):
        for tag in stock["tags"].split(" · ")[:6]:
            tags_html += f'<span class="tag">{tag}</span>'

    # Build score bar segments
    layers = [
        ("CAT", stock["catalyst"], 25, "#a855f7"),
        ("SQZ", stock["squeeze"], 20, "#ef4444"),
        ("SM", stock["smart_money"], 15, "#3b82f6"),
        ("OPT", stock["options"], 15, "#10b981"),
        ("SOC", stock["social"], 10, "#f59e0b"),
        ("RS", stock["strength"], 10, "#06b6d4"),
        ("TECH", stock["technical"], 5, "#8b5cf6"),
    ]
    
    score_breakdown = ""
    for name, score, max_score, color in layers:
        pct = (score / max_score) * 100 if max_score > 0 else 0
        score_breakdown += f'''
            <div class="score-row">
                <span class="score-label">{name}</span>
                <div class="score-bar-bg">
                    <div class="score-bar-fill" style="width:{pct}%;background:{color};"></div>
                </div>
                <span class="score-value">{score}/{max_score}</span>
            </div>
        '''

    # Squeeze data badge if applicable
    squeeze_badge = ""
    if stock.get("short_pct", 0) >= 15:
        squeeze_badge = f'''
            <div class="data-row">
                <span class="data-label">Short %</span>
                <span class="data-value" style="color:#ef4444;">{stock["short_pct"]:.0f}%</span>
            </div>
        '''
    
    if stock.get("float_M", 0) > 0:
        squeeze_badge += f'''
            <div class="data-row">
                <span class="data-label">Float</span>
                <span class="data-value">{stock["float_M"]:.1f}M</span>
            </div>
        '''
    
    if stock.get("days_to_cover", 0) >= 3:
        squeeze_badge += f'''
            <div class="data-row">
                <span class="data-label">Days to Cover</span>
                <span class="data-value" style="color:#f59e0b;">{stock["days_to_cover"]:.1f}d</span>
            </div>
        '''

    return f'''
    <div class="card" style="border-left: 3px solid {get_tier_color(tier)}; background: {get_tier_bg(tier)};">
        <div class="card-header">
            <div class="card-left">
                <div class="tier-badge" style="background:{get_tier_color(tier)};color:#0a0a0a;">TIER {tier}</div>
                <div class="symbol">{stock["symbol"]}</div>
                <div class="sector-pill">{sector}</div>
            </div>
            <div class="card-right">
                <div class="price">${stock["price"]:.2f}</div>
                <div class="change" style="color:{change_color};">{sign}{stock["change_pct"]:.2f}%</div>
            </div>
        </div>
        
        <div class="setup-type">{setup}</div>
        
        <div class="tags">{tags_html}</div>
        
        <div class="score-section">
            <div class="total-score">
                <span class="total-label">Conviction Score</span>
                <span class="total-value" style="color:{get_tier_color(tier)};">{stock["score"]}/100</span>
            </div>
            <div class="score-breakdown">
                {score_breakdown}
            </div>
        </div>
        
        {f'<div class="squeeze-data">{squeeze_badge}</div>' if squeeze_badge else ''}
        
        <div class="card-footer">
            <a href="https://www.tradingview.com/chart/?symbol={stock["symbol"]}" target="_blank" class="action-btn">📊 Chart</a>
            <a href="https://finance.yahoo.com/quote/{stock["symbol"]}" target="_blank" class="action-btn">📈 Yahoo</a>
            <a href="https://stocktwits.com/symbol/{stock["symbol"]}" target="_blank" class="action-btn">💬 Twits</a>
        </div>
    </div>
    '''


def build_dashboard(stocks):
    """Build complete HTML dashboard."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ny_time = datetime.now() - timedelta(hours=11)
    
    # Stats
    total = len(stocks)
    tier_s = len([s for s in stocks if s["tier"] == "S"])
    tier_1 = len([s for s in stocks if s["tier"] == "1"])
    tier_2 = len([s for s in stocks if s["tier"] == "2"])
    tier_3 = len([s for s in stocks if s["tier"] == "3"])
    
    avg_score = sum(s["score"] for s in stocks) / total if total > 0 else 0
    
    # Build cards
    cards_html = ""
    for stock in stocks[:30]:  # Top 30
        cards_html += build_card(stock)
    
    # Market status
    h = ny_time.hour
    m = ny_time.minute
    t = h * 60 + m
    if t < 9 * 60 + 30:
        status, status_color = "Pre-Market", "#3b82f6"
    elif t < 11 * 60 + 30:
        status, status_color = "🔥 Prime Window", "#10b981"
    elif t < 14 * 60 + 30:
        status, status_color = "😴 Lunch Lull", "#f59e0b"
    elif t < 16 * 60:
        status, status_color = "✅ Afternoon", "#10b981"
    else:
        status, status_color = "Closed", "#6b7280"

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="300">
<title>Elite Stock Scanner</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0a0a0a;
    color: #e5e5e5;
    min-height: 100vh;
    padding-bottom: 40px;
}}
.header {{
    background: linear-gradient(135deg, #1a1a1a 0%, #0f0f0f 100%);
    border-bottom: 1px solid #2a2a2a;
    padding: 20px 24px;
    position: sticky;
    top: 0;
    z-index: 100;
    backdrop-filter: blur(10px);
}}
.header-content {{
    max-width: 1400px;
    margin: 0 auto;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 12px;
}}
.title-section h1 {{
    font-size: 22px;
    font-weight: 600;
    color: #fff;
    letter-spacing: -0.02em;
}}
.title-section .subtitle {{
    font-size: 12px;
    color: #888;
    margin-top: 2px;
}}
.header-meta {{
    display: flex;
    gap: 16px;
    align-items: center;
    font-size: 13px;
}}
.market-status {{
    padding: 6px 14px;
    border-radius: 20px;
    background: {status_color}22;
    color: {status_color};
    border: 1px solid {status_color}44;
    font-weight: 500;
}}
.timestamp {{
    color: #666;
    font-size: 12px;
}}

.container {{
    max-width: 1400px;
    margin: 0 auto;
    padding: 24px;
}}

.stats-bar {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
}}
.stat-card {{
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 12px;
    padding: 16px;
    text-align: center;
}}
.stat-card.highlight {{
    border-color: #fbbf24;
    background: rgba(251, 191, 36, 0.08);
}}
.stat-value {{
    font-size: 28px;
    font-weight: 600;
    color: #fff;
    line-height: 1;
}}
.stat-label {{
    font-size: 11px;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-top: 6px;
}}

.legend {{
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 12px;
    padding: 14px 18px;
    margin-bottom: 20px;
    font-size: 12px;
    color: #aaa;
    display: flex;
    flex-wrap: wrap;
    gap: 20px;
    align-items: center;
}}
.legend strong {{ color: #fff; }}
.legend-item {{ display: flex; align-items: center; gap: 6px; }}
.legend-dot {{ width: 10px; height: 10px; border-radius: 50%; }}

.cards-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
    gap: 16px;
}}

.card {{
    background: #161616;
    border: 1px solid #2a2a2a;
    border-radius: 14px;
    padding: 18px;
    transition: all 0.2s;
    position: relative;
    overflow: hidden;
}}
.card:hover {{
    transform: translateY(-2px);
    border-color: #3a3a3a;
}}

.card-header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: 10px;
}}
.card-left {{
    display: flex;
    flex-direction: column;
    gap: 6px;
}}
.tier-badge {{
    display: inline-block;
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.05em;
    width: fit-content;
}}
.symbol {{
    font-size: 24px;
    font-weight: 700;
    color: #fff;
    letter-spacing: -0.02em;
}}
.sector-pill {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    background: #2a2a2a;
    color: #aaa;
    width: fit-content;
}}
.card-right {{
    text-align: right;
}}
.price {{
    font-size: 22px;
    font-weight: 600;
    color: #fff;
}}
.change {{
    font-size: 14px;
    font-weight: 500;
    margin-top: 2px;
}}

.setup-type {{
    background: #0f0f0f;
    border: 1px solid #2a2a2a;
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 13px;
    font-weight: 500;
    color: #fff;
    margin: 12px 0;
    text-align: center;
}}

.tags {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 14px;
}}
.tag {{
    background: rgba(99, 102, 241, 0.12);
    color: #a5b4fc;
    border: 1px solid rgba(99, 102, 241, 0.3);
    padding: 3px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 500;
}}

.score-section {{
    background: #0f0f0f;
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 12px;
}}
.total-score {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
    padding-bottom: 10px;
    border-bottom: 1px solid #2a2a2a;
}}
.total-label {{
    font-size: 11px;
    color: #888;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}}
.total-value {{
    font-size: 20px;
    font-weight: 700;
}}
.score-breakdown {{
    display: flex;
    flex-direction: column;
    gap: 4px;
}}
.score-row {{
    display: grid;
    grid-template-columns: 40px 1fr 50px;
    gap: 8px;
    align-items: center;
    font-size: 11px;
}}
.score-label {{
    color: #888;
    font-weight: 500;
}}
.score-bar-bg {{
    height: 6px;
    background: #1a1a1a;
    border-radius: 3px;
    overflow: hidden;
}}
.score-bar-fill {{
    height: 100%;
    border-radius: 3px;
    transition: width 0.3s;
}}
.score-value {{
    color: #aaa;
    text-align: right;
}}

.squeeze-data {{
    background: rgba(239, 68, 68, 0.05);
    border: 1px solid rgba(239, 68, 68, 0.15);
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 12px;
}}
.data-row {{
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    padding: 3px 0;
}}
.data-label {{
    color: #888;
}}
.data-value {{
    color: #fff;
    font-weight: 500;
}}

.card-footer {{
    display: flex;
    gap: 6px;
}}
.action-btn {{
    flex: 1;
    background: #1f1f1f;
    border: 1px solid #2a2a2a;
    color: #ccc;
    text-decoration: none;
    padding: 8px;
    border-radius: 6px;
    font-size: 11px;
    text-align: center;
    transition: all 0.15s;
}}
.action-btn:hover {{
    background: #2a2a2a;
    color: #fff;
}}

.empty-state {{
    text-align: center;
    padding: 60px 20px;
    color: #666;
}}
.empty-state h3 {{ color: #aaa; margin-bottom: 8px; }}

@media (max-width: 600px) {{
    .cards-grid {{ grid-template-columns: 1fr; }}
    .header-meta {{ flex-direction: column; align-items: flex-start; }}
}}
</style>
</head>
<body>
<div class="header">
    <div class="header-content">
        <div class="title-section">
            <h1>⚡ Elite Stock Scanner</h1>
            <div class="subtitle">7-Layer Conviction Scoring · Updated {now}</div>
        </div>
        <div class="header-meta">
            <span class="market-status">{status}</span>
            <span class="timestamp">NY: {ny_time.strftime("%H:%M")}</span>
        </div>
    </div>
</div>

<div class="container">
    <div class="stats-bar">
        <div class="stat-card highlight">
            <div class="stat-value" style="color:#fbbf24;">{tier_s}</div>
            <div class="stat-label">⭐ Tier S</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" style="color:#10b981;">{tier_1}</div>
            <div class="stat-label">Tier 1</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" style="color:#3b82f6;">{tier_2}</div>
            <div class="stat-label">Tier 2</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" style="color:#6b7280;">{tier_3}</div>
            <div class="stat-label">Tier 3</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{total}</div>
            <div class="stat-label">Total Setups</div>
        </div>
        <div class="stat-card">
            <div class="stat-value">{avg_score:.0f}</div>
            <div class="stat-label">Avg Score</div>
        </div>
    </div>

    <div class="legend">
        <strong>Conviction Layers:</strong>
        <span class="legend-item"><span class="legend-dot" style="background:#a855f7;"></span>CAT (Catalyst /25)</span>
        <span class="legend-item"><span class="legend-dot" style="background:#ef4444;"></span>SQZ (Squeeze /20)</span>
        <span class="legend-item"><span class="legend-dot" style="background:#3b82f6;"></span>SM (Smart Money /15)</span>
        <span class="legend-item"><span class="legend-dot" style="background:#10b981;"></span>OPT (Options /15)</span>
        <span class="legend-item"><span class="legend-dot" style="background:#f59e0b;"></span>SOC (Social /10)</span>
        <span class="legend-item"><span class="legend-dot" style="background:#06b6d4;"></span>RS (Strength /10)</span>
        <span class="legend-item"><span class="legend-dot" style="background:#8b5cf6;"></span>TECH (Technical /5)</span>
    </div>

    {f'<div class="cards-grid">{cards_html}</div>' if stocks else '<div class="empty-state"><h3>No setups yet</h3><p>Run elite_scanner.py to populate</p></div>'}
</div>

</body>
</html>'''


def main():
    print("\n" + "=" * 60)
    print("BUILDING ELITE DASHBOARD")
    print("=" * 60)

    # Load watchlist
    if not os.path.exists("elite_watchlist.json"):
        print("\n  ⚠ elite_watchlist.json not found")
        print("  Run elite_scanner.py first")
        return

    with open("elite_watchlist.json", "r") as f:
        stocks = json.load(f)

    print(f"  Loaded {len(stocks)} stocks")

    # Build HTML
    html = build_dashboard(stocks)
    with open("dashboard.html", "w", encoding="utf-8") as f:
        f.write(html)

    print(f"  ✓ Dashboard saved to dashboard.html")
    print(f"\n  Open dashboard.html in your browser to view")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
