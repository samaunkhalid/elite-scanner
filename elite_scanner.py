"""
ELITE MULTI-SOURCE STOCK SCANNER v2
====================================
Upgraded with 3 critical professional improvements:

  Stage 0: Market Regime Filter (SPY/QQQ/IWM/VIX)
  Execution Quality Filter (dollar volume, ATR sweet spot)
  Earnings Exclusion (reject earnings within 2 days)

7-Layer Conviction Scoring (re-weighted):
  1. CATALYST     /20 — Earnings 5-15d, fresh news
  2. EXECUTION    /20 — Dollar volume, ATR%, liquidity (NEW)
  3. SQUEEZE      /15 — Short interest, float (DEMOTED)
  4. SMART MONEY  /10 — Accumulation, insider % (DEMOTED)
  5. SOCIAL       /10 — Reddit mention velocity
  6. STRENGTH     /15 — RS vs SPY/sector
  7. TECHNICAL    /15 — EMA stack, gap retention (DEMOTED)

Total: 105 max

Tier System:
  S (75+): Highest conviction
  1 (60+): Strong setups
  2 (45+): Watching
  3 (30+): Monitor
"""

from yahooquery import Ticker
import pandas as pd
import requests
import json
import os
from datetime import datetime, timedelta


# ==============================================================
# UNIVERSE BUILDER
# ==============================================================

def get_dynamic_universe():
    """Fetch top gainers, losers, most active stocks."""
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


# ==============================================================
# STAGE 0: MARKET REGIME FILTER (NEW)
# ==============================================================

def detect_market_regime():
    """Determine overall market regime before scanning."""
    print(f"\n[Stage 0] Detecting market regime...")
    
    indicators = ["SPY", "QQQ", "IWM", "^VIX"]
    regime = {
        "spy_change": 0, "qqq_change": 0, "iwm_change": 0,
        "vix_level": 20, "vix_change": 0,
        "regime": "NORMAL", "bias": "NEUTRAL", "label": "Mixed market",
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
        vix_lv = regime["vix_level"]
        
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
        
        print(f"  SPY: {spy_chg:+.2f}% | QQQ: {regime['qqq_change']:+.2f}% | "
              f"IWM: {regime['iwm_change']:+.2f}% | VIX: {vix_lv:.1f}")
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


def has_earnings_within(symbol, calendar_all, days=2):
    """Check if earnings within X days. Returns (within, days_to_earnings)."""
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
                            return (days_to >= 0 and days_to <= days), days_to
                        except:
                            pass
    except:
        pass
    return False, None


# ==============================================================
# HARD REJECT FILTERS (UPGRADED PER CHATGPT)
# ==============================================================

def hard_reject(symbol, price, market_cap, avg_vol, exchange, atr_pct, earnings_within_2d):
    """Returns (rejected, reason)."""
    if price < 2.0:
        return True, "price_too_low"
    if price > 500:
        return True, "price_too_high"
    if market_cap < 100_000_000:
        return True, "mkt_cap_too_small"
    if avg_vol < 200_000:
        return True, "avg_vol_too_low"
    
    # Dollar volume check (NEW)
    dollar_vol = avg_vol * price
    if dollar_vol < 5_000_000:
        return True, "dollar_vol_too_low"
    
    if exchange and exchange not in ["NMS", "NYQ", "ASE", "NGM", "PCX", "BTS", "NCM"]:
        return True, "bad_exchange"
    
    # ATR sweet spot (NEW)
    if atr_pct > 0:
        if atr_pct < 1.0:
            return True, "too_low_volatility"
        if atr_pct > 15:
            return True, "too_volatile"
    
    # Earnings exclusion (NEW)
    if earnings_within_2d:
        return True, "earnings_imminent"
    
    return False, ""


# ==============================================================
# LAYER 1: CATALYST (FIXED)
# ==============================================================

def score_catalyst(symbol, change_pct, history_df, days_to_earnings):
    """Catalyst score (0-20 points)."""
    score = 0
    reasons = []
    
    if days_to_earnings is not None:
        if 3 <= days_to_earnings <= 7:
            score += 8
            reasons.append(f"Earnings in {days_to_earnings}d")
        elif 8 <= days_to_earnings <= 15:
            score += 5
            reasons.append(f"Earnings in {days_to_earnings}d")
    
    if abs(change_pct) >= 15:
        score += 8
        reasons.append(f"Major move {change_pct:+.1f}%")
    elif abs(change_pct) >= 8:
        score += 5
        reasons.append(f"Big move {change_pct:+.1f}%")
    elif abs(change_pct) >= 4:
        score += 3
    
    if history_df is not None and len(history_df) >= 23:
        try:
            recent_vol = history_df["volume"].tail(3).mean()
            base_vol = history_df["volume"].iloc[-23:-3].mean()
            if base_vol > 0:
                vol_surge = recent_vol / base_vol
                if vol_surge > 3:
                    score += 5
                    reasons.append(f"Vol surge {vol_surge:.1f}x")
                elif vol_surge > 2:
                    score += 3
        except:
            pass
    
    return min(score, 20), reasons


# ==============================================================
# LAYER 2: EXECUTION QUALITY (NEW from ChatGPT)
# ==============================================================

def score_execution(symbol, price, avg_vol, atr_pct, today_vol):
    """Execution Quality score (0-20 points)."""
    score = 0
    reasons = []
    
    dollar_vol = avg_vol * price
    if dollar_vol > 100_000_000:
        score += 7
        reasons.append(f"${dollar_vol/1e6:.0f}M liq")
    elif dollar_vol > 50_000_000:
        score += 5
        reasons.append(f"${dollar_vol/1e6:.0f}M liq")
    elif dollar_vol > 25_000_000:
        score += 3
    elif dollar_vol > 10_000_000:
        score += 1
    
    if 2 <= atr_pct <= 7:
        score += 5
        reasons.append(f"ATR {atr_pct:.1f}% (clean)")
    elif 1.5 <= atr_pct < 2 or 7 < atr_pct <= 10:
        score += 3
    
    if 10 <= price <= 200:
        score += 4
        reasons.append("Clean price range")
    elif 5 <= price < 10 or 200 < price <= 300:
        score += 2
    
    if avg_vol > 0:
        rvol = today_vol / avg_vol
        if rvol > 2.5:
            score += 4
            reasons.append(f"RVOL {rvol:.1f}x")
        elif rvol > 1.5:
            score += 2
    
    return min(score, 20), reasons


# ==============================================================
# LAYER 3: SQUEEZE (DEMOTED 20→15)
# ==============================================================

def score_squeeze(symbol, key_stats_all):
    """Squeeze score (0-15 points)."""
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

                if short_pct >= 30:
                    score += 6
                    reasons.append(f"SI {short_pct:.0f}%")
                elif short_pct >= 20:
                    score += 4
                    reasons.append(f"SI {short_pct:.0f}%")
                elif short_pct >= 15:
                    score += 2
                    reasons.append(f"SI {short_pct:.0f}%")

                # Fixed float scoring per ChatGPT
                float_M = float_size / 1e6 if float_size > 0 else 0
                if 20 <= float_M <= 150:
                    score += 5
                    reasons.append(f"Float {float_M:.0f}M (sweet)")
                elif 10 <= float_M < 20:
                    score += 2
                    reasons.append(f"Float {float_M:.0f}M ⚠️")
                elif 150 < float_M <= 500:
                    score += 2

                if days_to_cover >= 7:
                    score += 4
                    reasons.append(f"DTC {days_to_cover:.1f}d")
                elif days_to_cover >= 5:
                    score += 2
    except:
        pass

    return min(score, 15), reasons, {"short_pct": short_pct, "float": float_size, "days_to_cover": days_to_cover}


# ==============================================================
# LAYER 4: SMART MONEY (DEMOTED 15→10)
# ==============================================================

def score_smart_money(symbol, key_stats_all, history_df):
    """Smart money score (0-10 points)."""
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
# LAYER 5: SOCIAL (UNCHANGED)
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
# LAYER 6: RELATIVE STRENGTH (15)
# ==============================================================

def score_strength(symbol, history_df, spy_df, change_pct, regime):
    """RS score (0-15 points) with regime alignment."""
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
            reasons.append(f"Up {change_pct:.1f}% today")
        elif change_pct > 2:
            score += 1
        
        # Regime alignment bonus (NEW)
        bias = regime.get("bias", "NEUTRAL")
        if bias == "LONG_FAVORED" and change_pct > 0:
            score += 2
            reasons.append("Aligned w/ market")
        elif bias == "SHORT_FAVORED" and change_pct < 0:
            score += 2
            reasons.append("Aligned w/ market")
    except:
        pass

    return min(score, 15), reasons


# ==============================================================
# LAYER 7: TECHNICAL (DEMOTED 20→15)
# ==============================================================

def score_technical(symbol, history_df):
    """Technical score (0-15 points)."""
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
            score += 5
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
                score += 3
                reasons.append("Near 20D high")
            elif pct_from_high < 5:
                score += 1

        if latest["close"] > latest["open"] and latest["close"] > prev["close"]:
            score += 2

        bar_range = latest["high"] - latest["low"]
        if bar_range > 0:
            close_position = (latest["close"] - latest["low"]) / bar_range
            if close_position > 0.75:
                score += 2
                reasons.append("Strong close")
    except:
        pass

    return min(score, 15), reasons


# ==============================================================
# MAIN
# ==============================================================

def main():
    print("\n" + "=" * 70)
    print("ELITE MULTI-SOURCE STOCK SCANNER v2 (Pro-Upgraded)")
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
    history = tickers.history(period="1y", interval="1d")

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

    print(f"\n[Stage 5] Hard reject + 7-layer scoring...\n")
    
    layer_stats = {name: {"hits": 0, "total_pts": 0, "max_seen": 0}
                   for name in ["catalyst", "execution", "squeeze", "smart_money",
                                "social", "strength", "technical"]}
    score_distribution = {b: 0 for b in ["0-9","10-19","20-29","30-39","40-49","50-59","60+"]}
    quality_reasons = {}
    quality_filtered = 0
    total_processed = 0
    earnings_blocked = 0

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
            within_2d, days_to_earnings = has_earnings_within(symbol, calendar_all, days=2)
            
            rejected, reason = hard_reject(symbol, price, market_cap, avg_vol, exchange, atr_pct, within_2d)
            if rejected:
                quality_reasons[reason] = quality_reasons.get(reason, 0) + 1
                quality_filtered += 1
                if reason == "earnings_imminent":
                    earnings_blocked += 1
                continue

            cat_score, cat_reasons = score_catalyst(symbol, change_pct, hist_df, days_to_earnings)
            exec_score, exec_reasons = score_execution(symbol, price, avg_vol, atr_pct, today_vol)
            sq_score, sq_reasons, sq_data = score_squeeze(symbol, key_stats_all)
            sm_score, sm_reasons = score_smart_money(symbol, key_stats_all, hist_df)
            soc_score, soc_reasons = score_social(symbol, social_map)
            rs_score, rs_reasons = score_strength(symbol, hist_df, spy_df, change_pct, regime)
            tech_score, tech_reasons = score_technical(symbol, hist_df)

            for name, score in [("catalyst", cat_score), ("execution", exec_score),
                                 ("squeeze", sq_score), ("smart_money", sm_score),
                                 ("social", soc_score), ("strength", rs_score),
                                 ("technical", tech_score)]:
                if score > 0:
                    layer_stats[name]["hits"] += 1
                layer_stats[name]["total_pts"] += score
                if score > layer_stats[name]["max_seen"]:
                    layer_stats[name]["max_seen"] = score
            
            total_processed += 1
            total = cat_score + exec_score + sq_score + sm_score + soc_score + rs_score + tech_score

            if total < 10:
                score_distribution["0-9"] += 1
            elif total < 20:
                score_distribution["10-19"] += 1
            elif total < 30:
                score_distribution["20-29"] += 1
            elif total < 40:
                score_distribution["30-39"] += 1
            elif total < 50:
                score_distribution["40-49"] += 1
            elif total < 60:
                score_distribution["50-59"] += 1
            else:
                score_distribution["60+"] += 1

            if total < 25:
                continue

            if total >= 75:
                tier = "S"
            elif total >= 60:
                tier = "1"
            elif total >= 45:
                tier = "2"
            else:
                tier = "3"

            all_reasons = (cat_reasons + exec_reasons + sq_reasons + sm_reasons +
                           soc_reasons + rs_reasons + tech_reasons)
            tags = " · ".join(all_reasons[:6])

            results.append({
                "tier": tier,
                "symbol": symbol,
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
                "score": total,
                "catalyst": cat_score,
                "execution": exec_score,
                "squeeze": sq_score,
                "smart_money": sm_score,
                "social": soc_score,
                "strength": rs_score,
                "technical": tech_score,
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
    print(f"  Successfully scored:  {total_processed}")
    
    print(f"\n  HARD REJECT BREAKDOWN:")
    for reason, count in sorted(quality_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason:<25} {count:>4}")
    
    print(f"\n  LAYER HIT RATES:")
    layer_max = {"catalyst": 20, "execution": 20, "squeeze": 15, "smart_money": 10,
                 "social": 10, "strength": 15, "technical": 15}
    print(f"  {'Layer':<14} {'Hits':>6} {'Hit%':>7} {'AvgPts':>8} {'Max':>5}/{'Possible':<8}")
    print(f"  {'-' * 55}")
    for layer_name, stats in layer_stats.items():
        hits = stats["hits"]
        hit_pct = (hits / total_processed * 100) if total_processed > 0 else 0
        avg_pts = (stats["total_pts"] / total_processed) if total_processed > 0 else 0
        max_seen = stats["max_seen"]
        max_p = layer_max.get(layer_name, 0)
        flag = " ⚠️" if hit_pct < 10 else ""
        print(f"  {layer_name:<14} {hits:>6} {hit_pct:>6.1f}% {avg_pts:>7.1f} {max_seen:>4}/{max_p:<7}{flag}")
    
    print(f"\n  SCORE DISTRIBUTION:")
    for bucket, count in score_distribution.items():
        bar = "█" * int(count / max(1, total_processed) * 50)
        print(f"  {bucket:<8}: {count:>4}  {bar}")
    
    print(f"{'=' * 70}\n")

    results = sorted(results, key=lambda x: x["score"], reverse=True)
    df = pd.DataFrame(results)

    if df.empty:
        print("  No stocks passed scoring threshold today.")
        pd.DataFrame().to_csv("elite_watchlist.csv", index=False)
        with open("elite_watchlist.json", "w") as f:
            f.write("[]")
        with open("market_regime.json", "w") as f:
            json.dump(regime, f, indent=2)
        return

    print(f"  ELITE WATCHLIST — {len(df)} setups")
    print(f"{'=' * 70}\n")
    display_cols = ["tier", "symbol", "price", "change_pct", "score",
                    "atr_pct", "dollar_vol_M", "tags"]
    print(df[display_cols].head(25).to_string(index=False))

    df.to_csv("elite_watchlist.csv", index=False)
    df.head(20).to_json("elite_watchlist.json", orient="records", indent=2)
    with open("market_regime.json", "w") as f:
        json.dump(regime, f, indent=2)

    print(f"\n  Saved: elite_watchlist.csv ({len(df)} setups)")
    print(f"  Saved: elite_watchlist.json (top 20)")
    print(f"  Saved: market_regime.json (regime info)")
    print(f"\n{'=' * 70}\n")


if __name__ == "__main__":
    main()
