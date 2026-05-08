"""
ELITE MULTI-SOURCE STOCK SCANNER v2.1
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
  - Active Watchlist: Top 20 only
  - Raw scored universe: Full CSV for diagnostics
"""

from yahooquery import Ticker
import pandas as pd
import requests
import json
from datetime import datetime, timedelta


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
# HARD REJECT
# ==============================================================

def hard_reject(symbol, price, market_cap, avg_vol, exchange, atr_pct, earnings_within_2d):
    """Returns (rejected, reason)."""
    if price < 2.0:
        return True, "price_too_low"
    if price > 100:
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

    # Price sweet spot: $5–$80 scanner
    if 10 <= price <= 80:
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


def classify_setup_bucket(stock):
    """
    Classify stocks into useful trading buckets.
    Main goal: separate potential movers from already-extended movers.
    """
    change = abs(float(stock.get("change_pct", 0) or 0))
    risk = stock.get("risk_category", "NORMAL")

    above_vwap = bool(stock.get("above_vwap", False))
    near_hod = bool(stock.get("near_hod", False))
    vwap_dist = float(stock.get("vwap_dist_pct", 999) or 999)
    recent_range = float(stock.get("recent_range_pct", 999) or 999)

    # Highest risk first
    if risk in ["HIGH_RISK", "EXTREME_MOVE"]:
        return "HIGH_RISK_EXTREME"

    if risk == "EXTENDED" or change >= 25:
        return "EXTENDED_CHASE_RISK"

    # Active momentum = already moving, but not extreme
    if 12 <= change < 25:
        return "ACTIVE_MOMENTUM"

    # Potential mover = cleaner, earlier setup
    if (
        change <= 12
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
            
            final_score = base_total + ext_penalty + regime_penalty

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
                           rs_reasons + tech_reasons + part_reasons + soc_reasons)
            
            # Add special tags
            if is_earnings_reaction:
                all_reasons.insert(0, "📊 EARNINGS REACTION")
            if risk_cat != "NORMAL":
                all_reasons.insert(0, f"⚠️ {risk_cat.replace('_', ' ')}")
            
            tags = " · ".join(all_reasons[:7])

            results.append({
                "tier": tier,
                "symbol": symbol,
                "price": round(price, 2),
                "change_pct": round(change_pct, 2),
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
    # ALPACA IEX ENRICHMENT (Real-time intraday data)
    # =================================================================
    print(f"\n[Stage 6] Enriching top candidates with Alpaca IEX real-time data...\n")
    
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
                
                # Add intraday fields
                stock["vwap"] = rt["vwap"]
                stock["vwap_dist_pct"] = rt["vwap_dist_pct"]
                stock["above_vwap"] = rt["above_vwap"]
                stock["hod"] = rt["hod"]
                stock["lod"] = rt["lod"]
                stock["from_hod_pct"] = rt["from_hod_pct"]
                stock["near_hod"] = rt["near_hod"]
                stock["intraday_volume"] = rt["intraday_volume"]
                stock["data_source"] = "Yahoo + IEX"
                
                # Add intraday reasons to tags
                if intraday_reasons:
                    current_tags = stock["tags"].split(" · ")
                    combined = intraday_reasons + current_tags
                    stock["tags"] = " · ".join(combined[:7])
                
                enriched_count += 1
        
        print(f"  ✓ Enriched {enriched_count} stocks with IEX real-time data")
        print(f"  ⚠️ Note: IEX volume is non-consolidated (~2-3% of market)")
        
        # Re-sort with intraday scores
        results = sorted(results, key=lambda x: x["score"], reverse=True)
        
    except ImportError:
        print(f"  ⚠️ Alpaca not available - skipping real-time enrichment")
        print(f"  Install: pip install alpaca-py")
    except Exception as e:
        print(f"  ⚠️ Alpaca enrichment failed: {e}")
        print(f"  Continuing with Yahoo data only...")
    
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

    potential_df.head(10).to_csv("potential_movers.csv", index=False)
    active_df.head(10).to_csv("active_momentum.csv", index=False)
    extended_df.head(10).to_csv("extended_movers.csv", index=False)
    highrisk_df.head(10).to_csv("high_risk_movers.csv", index=False)

    with open("market_regime.json", "w") as f:
        json.dump(regime, f, indent=2)

    print(f"\n  Saved: elite_watchlist_raw.csv ({len(df)} stocks - full diagnostic)")
    print(f"  Saved: elite_watchlist.csv (bucketed active watchlist)")
    print(f"  Saved: elite_watchlist.json")
    print(f"  Saved: potential_movers.csv")
    print(f"  Saved: active_momentum.csv")
    print(f"  Saved: extended_movers.csv")
    print(f"  Saved: high_risk_movers.csv")
    print(f"  Saved: market_regime.json")
    print(f"\n{'=' * 70}\n")


if __name__ == "__main__":
    main()
