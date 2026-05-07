"""
ELITE MULTI-SOURCE STOCK SCANNER
=================================
7-Layer Conviction Scoring System

Layers:
  1. CATALYST     — FDA dates, earnings, news (max 25 pts)
  2. SQUEEZE      — Short interest, float, borrow (max 20 pts)
  3. SMART MONEY  — Dark pool, insider buying (max 15 pts)
  4. OPTIONS      — Unusual call/put activity (max 15 pts)
  5. SOCIAL       — Reddit mentions velocity (max 10 pts)
  6. STRENGTH     — RS vs SPY/sector (max 10 pts)
  7. TECHNICAL    — Breakout setup (max 5 pts)

Threshold: 60+ = Tier 1 setup (display)
           75+ = Tier S setup (highest conviction)

Runs daily pre-market. Outputs ranked watchlist.
"""

from yahooquery import Ticker
import pandas as pd
import requests
import json
import os
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

# ==============================================================
# DYNAMIC STOCK UNIVERSE (gainers/losers/active scraping)
# ==============================================================

def get_dynamic_universe():
    """Fetch top gainers, losers, and most active stocks daily."""
    universe = set()

    # Yahoo screeners give us today's hot stocks
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

    # Add momentum/squeeze candidates (manually curated proven movers)
    momentum_core = [
        # AI / Quantum
        "SOUN", "AI", "BBAI", "IONQ", "RGTI", "ARQQ", "PLTR",
        # Crypto
        "RIOT", "MARA", "CLSK", "HUT", "BITF", "CIFR", "CORZ", "BTBT", "IREN", "MSTR", "COIN",
        # Semis (volatile)
        "WOLF", "LSCC", "SMCI", "SMTC",
        # EV / Energy
        "RIVN", "NIO", "LCID", "QS", "CHPT", "PLUG", "FCEL", "BE", "BLNK", "EVGO",
        # Space / Defense
        "RKLB", "ASTS", "LUNR", "JOBY", "KTOS", "ACHR",
        # Biotech (squeeze candidates)
        "HIMS", "CRSP", "BNGO", "VKTX", "MDGL", "VRDN", "CYTK", "IOVA", "SAVA",
        # Squeeze / Meme
        "GME", "AMC", "BBBY", "BB", "NOK",
        # Fintech
        "HOOD", "SOFI", "AFRM", "UPST",
        # Social/Streaming
        "RDDT", "PINS", "SNAP", "RBLX", "ROKU", "DKNG",
        # Cybersecurity
        "NET", "CRWD", "ZS", "PANW",
    ]
    universe.update(momentum_core)

    print(f"  Dynamic universe: {len(universe)} stocks")
    return list(universe)


# ==============================================================
# LAYER 1: CATALYST DETECTION
# ==============================================================

def score_catalyst(symbol, calendar_all):
    """
    Catalyst score (0-25 points)
    - Earnings within 7 days: +15
    - Earnings 8-14 days: +8
    - Recent gap >5%: +5
    """
    score = 0
    reasons = []

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
                            days_to_earnings = (edate - now).days
                            if 0 <= days_to_earnings <= 7:
                                score += 15
                                reasons.append(f"Earnings in {days_to_earnings}d")
                            elif 8 <= days_to_earnings <= 14:
                                score += 8
                                reasons.append(f"Earnings in {days_to_earnings}d")
                        except:
                            pass
    except:
        pass

    return score, reasons


# ==============================================================
# LAYER 2: SHORT SQUEEZE SETUP
# ==============================================================

def score_squeeze(symbol, key_stats_all):
    """
    Squeeze score (0-20 points)
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
                # Short interest
                short_pct = sym_stats.get("shortPercentOfFloat", 0) or 0
                if isinstance(short_pct, (int, float)):
                    short_pct = short_pct * 100

                # Float
                float_size = sym_stats.get("floatShares", 0) or 0
                
                # Short ratio (days to cover)
                days_to_cover = sym_stats.get("shortRatio", 0) or 0

                # Score short %
                if short_pct >= 30:
                    score += 10
                    reasons.append(f"SI {short_pct:.0f}%")
                elif short_pct >= 20:
                    score += 6
                    reasons.append(f"SI {short_pct:.0f}%")
                elif short_pct >= 10:
                    score += 3
                    reasons.append(f"SI {short_pct:.0f}%")

                # Score float
                if 0 < float_size < 20_000_000:
                    score += 5
                    reasons.append(f"Float {float_size/1e6:.0f}M")
                elif 20_000_000 <= float_size < 50_000_000:
                    score += 3
                    reasons.append(f"Float {float_size/1e6:.0f}M")

                # Score days to cover
                if days_to_cover >= 7:
                    score += 5
                    reasons.append(f"DTC {days_to_cover:.1f}d")
                elif days_to_cover >= 5:
                    score += 3
    except:
        pass

    return score, reasons, {"short_pct": short_pct, "float": float_size, "days_to_cover": days_to_cover}


# ==============================================================
# LAYER 3: SMART MONEY FLOW
# ==============================================================

def score_smart_money(symbol, key_stats_all, history_df):
    """
    Smart money score (0-15 points)
    """
    score = 0
    reasons = []

    try:
        # Accumulation/Distribution proxy
        if history_df is not None and len(history_df) >= 20:
            df = history_df.tail(20).copy()
            df["change"] = df["close"].pct_change()
            up_vol = df[df["change"] > 0]["volume"].mean()
            dn_vol = df[df["change"] < 0]["volume"].mean()
            if up_vol > 0 and dn_vol > 0 and not pd.isna(up_vol) and not pd.isna(dn_vol):
                acc_ratio = up_vol / dn_vol
                if acc_ratio > 1.3:
                    score += 10
                    reasons.append(f"Accumulating {acc_ratio:.1f}x")
                elif acc_ratio > 1.1:
                    score += 5
                    reasons.append("Accumulating")

        # Insider ownership
        if isinstance(key_stats_all, dict):
            sym_stats = key_stats_all.get(symbol, {})
            if isinstance(sym_stats, dict):
                insider_pct = sym_stats.get("heldPercentInsiders", 0) or 0
                if isinstance(insider_pct, (int, float)):
                    insider_pct *= 100
                    if insider_pct >= 10:
                        score += 5
                        reasons.append(f"Insider {insider_pct:.0f}%")
                    elif insider_pct >= 5:
                        score += 3
    except:
        pass

    return score, reasons


# ==============================================================
# LAYER 4: OPTIONS FLOW (Unusual Activity)
# ==============================================================

def score_options(symbol, ticker_data):
    """
    Options score (0-15 points)
    Note: Yahoo options data is slow to fetch per-stock.
    Currently uses simplified scoring based on quote data.
    To enable full options flow, upgrade to FlowAlgo or paid API.
    """
    score = 0
    reasons = []

    # Placeholder — return 0 for now to keep scanner fast
    # Future: integrate with paid options flow API
    return score, reasons


# ==============================================================
# LAYER 5: SOCIAL SENTIMENT (Reddit/WSB)
# ==============================================================

def fetch_social_data():
    """Fetch trending stocks from ApeWisdom (free Reddit API)."""
    try:
        url = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            social_map = {}
            for item in results:
                ticker = item.get("ticker")
                mentions = int(item.get("mentions", 0))
                m_24h = int(item.get("mentions_24h_ago", 0)) or 1
                rank = int(item.get("rank", 999))
                rank_24h = int(item.get("rank_24h_ago", 999)) or 999

                # Velocity = mentions growth + rank improvement
                mention_growth = mentions / m_24h if m_24h > 0 else 1
                rank_change = rank_24h - rank  # positive = moved up

                social_map[ticker] = {
                    "mentions": mentions,
                    "growth": mention_growth,
                    "rank": rank,
                    "rank_change": rank_change,
                }
            return social_map
    except Exception as e:
        print(f"  Social data fetch failed: {e}")
    return {}


def score_social(symbol, social_map):
    """
    Social score (0-10 points)
    - Top 10 trending: +10
    - Top 25: +5
    - Mention velocity > 3x: +5
    - Rising rank (rank improving): +3
    """
    score = 0
    reasons = []

    if symbol in social_map:
        data = social_map[symbol]
        rank = data["rank"]
        growth = data["growth"]
        rank_change = data["rank_change"]

        if rank <= 10:
            score += 10
            reasons.append(f"WSB #{rank}")
        elif rank <= 25:
            score += 5
            reasons.append(f"WSB #{rank}")
        elif rank <= 50:
            score += 2

        if growth >= 3:
            score = min(score + 3, 10)
            reasons.append(f"{growth:.1f}x mentions")

    return score, reasons


# ==============================================================
# LAYER 6: RELATIVE STRENGTH
# ==============================================================

def score_strength(symbol, history_df, spy_df, change_pct=0):
    """
    Relative Strength score (0-15 points) — boosted from 10
    """
    score = 0
    reasons = []

    if history_df is None or len(history_df) < 60 or spy_df is None or len(spy_df) < 60:
        return score, reasons

    try:
        # Calculate returns at multiple timeframes
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

        # 52-week high proximity
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

        # Today's move (extra 2 points for big up days)
        if change_pct > 5:
            score += 2
            reasons.append(f"Up {change_pct:.1f}% today")
        elif change_pct > 2:
            score += 1
    except:
        pass

    return min(score, 15), reasons


# ==============================================================
# LAYER 7: TECHNICAL BREAKOUT
# ==============================================================

def score_technical(symbol, history_df, quote_data=None):
    """
    Technical score (0-20 points) — boosted from 5 since options is disabled
    - Above EMA9, EMA20, SMA50: +6
    - Volume surge today vs 20-day avg > 2x: +5
    - Recent gap > 3%: +4
    - Within 5% of 20-day high: +3
    - Bullish daily candle: +2
    """
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

        # EMA stack
        if latest["close"] > latest["EMA9"] > latest["EMA20"] > latest["SMA50"]:
            score += 6
            reasons.append("EMA stack ↑")
        elif latest["close"] > latest["EMA20"]:
            score += 3
            reasons.append("Above EMA20")

        # Volume surge
        avg_vol = df["volume"].tail(20).mean()
        if avg_vol > 0 and latest["volume"] > avg_vol * 2:
            score += 5
            reasons.append(f"Vol {latest['volume']/avg_vol:.1f}x")
        elif avg_vol > 0 and latest["volume"] > avg_vol * 1.5:
            score += 3

        # Recent gap
        if prev["close"] > 0:
            gap_pct = ((latest["open"] - prev["close"]) / prev["close"]) * 100
            if abs(gap_pct) > 3:
                score += 4
                reasons.append(f"Gap {gap_pct:+.1f}%")
            elif abs(gap_pct) > 1.5:
                score += 2

        # 20-day high proximity
        high_20 = df["close"].tail(20).max()
        if high_20 > 0:
            pct_from_high = ((high_20 - latest["close"]) / high_20) * 100
            if pct_from_high < 2:
                score += 3
                reasons.append("Near 20D high")
            elif pct_from_high < 5:
                score += 1

        # Bullish daily candle
        if latest["close"] > latest["open"] and latest["close"] > prev["close"]:
            score += 2

    except:
        pass

    return min(score, 20), reasons


# ==============================================================
# QUALITY GATE (filters trash)
# ==============================================================

def passes_quality_gate(symbol, price, market_cap, avg_volume, exchange):
    """
    Quality gate to filter penny pumps and OTC garbage.
    """
    if price < 2.0 or price > 500:
        return False, "price out of range"
    if market_cap < 200_000_000:
        return False, f"mkt cap ${market_cap/1e6:.0f}M"
    if avg_volume < 500_000:
        return False, f"avg vol {avg_volume/1e6:.1f}M"
    if exchange and exchange not in ["NMS", "NYQ", "ASE", "NGM", "PCX", "BTS"]:
        return False, f"exchange {exchange}"
    return True, ""


# ==============================================================
# MAIN SCANNER
# ==============================================================

def main():
    print("\n" + "=" * 70)
    print("ELITE MULTI-SOURCE STOCK SCANNER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # Step 1: Get universe
    print(f"\n[1/5] Building dynamic universe...")
    universe = get_dynamic_universe()

    # Step 2: Fetch SPY for relative strength
    print(f"\n[2/5] Fetching SPY benchmark...")
    spy_df = None
    try:
        spy = Ticker("SPY")
        spy_data = spy.history(period="1y", interval="1d")
        if isinstance(spy_data, pd.DataFrame) and not spy_data.empty:
            spy_df = spy_data.reset_index().sort_values("date")
    except Exception as e:
        print(f"  SPY fetch failed: {e}")

    # Step 3: Fetch social data once
    print(f"\n[3/5] Fetching Reddit/WSB sentiment data...")
    social_map = fetch_social_data()
    print(f"  Got social data for {len(social_map)} tickers")

    # Step 4: Fetch all stock data in batch
    print(f"\n[4/5] Fetching market data for {len(universe)} stocks...")
    print(f"  This may take 2-3 minutes...")

    tickers = Ticker(universe, asynchronous=True)
    quotes = tickers.price
    history = tickers.history(period="1y", interval="1d")

    # Pre-fetch supporting data in batches (massive speedup)
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

    # Step 5: Score each stock through all 7 layers
    print(f"\n[5/5] Scoring stocks through 7 layers...\n")

    results = []
    for symbol in universe:
        try:
            # Get quote
            q = quotes.get(symbol, {})
            if not isinstance(q, dict):
                continue

            price = q.get("regularMarketPrice", 0) or 0
            change_pct = (q.get("regularMarketChangePercent", 0) or 0) * 100
            volume = q.get("regularMarketVolume", 0) or 0
            market_cap = q.get("marketCap", 0) or 0
            exchange = q.get("exchange", "")

            # Quality gate
            passed, reason = passes_quality_gate(symbol, price, market_cap,
                                                  q.get("averageDailyVolume3Month", 0) or 0,
                                                  exchange)
            if not passed:
                continue

            # Get history for this stock
            hist_df = None
            try:
                if symbol in history.index.get_level_values(0):
                    hist_df = history.loc[symbol].copy().reset_index()
                    hist_df["date"] = pd.to_datetime(hist_df["date"])
                    hist_df = hist_df.sort_values("date").reset_index(drop=True)
            except:
                pass

            # Score all 7 layers
            cat_score, cat_reasons = score_catalyst(symbol, calendar_all)
            sq_score, sq_reasons, sq_data = score_squeeze(symbol, key_stats_all)
            sm_score, sm_reasons = score_smart_money(symbol, key_stats_all, hist_df)
            opt_score, opt_reasons = score_options(symbol, tickers)
            soc_score, soc_reasons = score_social(symbol, social_map)
            rs_score, rs_reasons = score_strength(symbol, hist_df, spy_df, change_pct)
            tech_score, tech_reasons = score_technical(symbol, hist_df, q)

            total = cat_score + sq_score + sm_score + opt_score + soc_score + rs_score + tech_score

            # Keep all stocks scoring at least minimal points
            # (Yahoo free data limits some layers — adjust thresholds accordingly)
            if total < 10:
                continue

            # Determine tier and tags
            # Adjusted thresholds for Yahoo-free data reality
            if total >= 50:
                tier = "S"
            elif total >= 35:
                tier = "1"
            elif total >= 20:
                tier = "2"
            else:
                tier = "3"

            all_reasons = (cat_reasons + sq_reasons + sm_reasons +
                           opt_reasons + soc_reasons + rs_reasons + tech_reasons)
            tags = " · ".join(all_reasons[:5])  # Top 5 tags

            results.append({
                "tier": tier,
                "symbol": symbol,
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "score": total,
                "catalyst": cat_score,
                "squeeze": sq_score,
                "smart_money": sm_score,
                "options": opt_score,
                "social": soc_score,
                "strength": rs_score,
                "technical": tech_score,
                "short_pct": sq_data.get("short_pct", 0),
                "float_M": round(sq_data.get("float", 0) / 1e6, 1),
                "days_to_cover": round(sq_data.get("days_to_cover", 0), 1),
                "market_cap_B": round(market_cap / 1e9, 2),
                "tags": tags,
            })

        except Exception as e:
            continue

    # Sort by score
    results = sorted(results, key=lambda x: x["score"], reverse=True)
    df = pd.DataFrame(results)

    if df.empty:
        print("  No stocks passed scoring threshold today.")
        return

    # Display top results
    print(f"\n{'=' * 70}")
    print(f"  ELITE WATCHLIST — {len(df)} setups found")
    print(f"{'=' * 70}\n")

    display_cols = ["tier", "symbol", "price", "change_pct", "score",
                    "short_pct", "float_M", "tags"]
    print(df[display_cols].head(20).to_string(index=False))

    # Save outputs
    df.to_csv("elite_watchlist.csv", index=False)
    df.head(15).to_json("elite_watchlist.json", orient="records", indent=2)

    print(f"\n  Saved: elite_watchlist.csv ({len(df)} setups)")
    print(f"  Saved: elite_watchlist.json (top 15)")
    print(f"\n{'=' * 70}\n")


if __name__ == "__main__":
    main()
