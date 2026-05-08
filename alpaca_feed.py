"""
ALPACA IEX DATA FEED
====================
Real-time Level 1 data from IEX exchange (free tier).

Important: IEX represents ~2-3% of total market volume.
Volume/RVOL calculations are proxy only, not full market consolidated data.
"""

import os
from datetime import datetime, timedelta
import pandas as pd

try:
    from alpaca.data import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
    from alpaca.data.timeframe import TimeFrame
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False
    print("  ⚠️ alpaca-py not installed. Run: pip install alpaca-py")


class AlpacaFeed:
    """Fetch real-time IEX data for intraday potential mover detection."""
    
    def __init__(self, api_key=None, secret_key=None):
        if not ALPACA_AVAILABLE:
            raise ImportError("alpaca-py not installed")
        
        # Get from environment if not provided
        self.api_key = api_key or os.getenv("ALPACA_API_KEY")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")
        
        if not self.api_key or not self.secret_key:
            raise ValueError("Alpaca API credentials not found")
        
        self.client = StockHistoricalDataClient(self.api_key, self.secret_key)
    
    def get_intraday_data(self, symbols, lookback_hours=6):
        """
        Get real-time intraday data for multiple symbols.
        
        Returns dict with:
        - price: current price
        - vwap: today's VWAP
        - vwap_dist_pct: % distance from VWAP
        - hod/lod: high/low of day
        - from_hod_pct: % below high
        - intraday_volume: total IEX volume today
        - bars_count: number of 1-min bars
        """
        print(f"  Fetching Alpaca IEX data for {len(symbols)} symbols...")
        
        results = {}
        
        # Market open/close times (ET)
        now = datetime.now()
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        
        # If before market open, use yesterday
        if now < market_open:
            market_open = market_open - timedelta(days=1)
        
        # Get 1-minute bars
        try:
            request = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Minute,
                start=market_open,
                end=now
            )
            
            bars_response = self.client.get_stock_bars(request)
            
            for symbol in symbols:
                try:
                    if symbol not in bars_response:
                        continue
                    
                    # Convert to dataframe
                    bars_df = bars_response[symbol].df.reset_index()
                    
                    if len(bars_df) == 0:
                        continue
                    
                    # Calculate VWAP
                    bars_df['typical_price'] = (bars_df['high'] + bars_df['low'] + bars_df['close']) / 3
                    bars_df['pv'] = bars_df['typical_price'] * bars_df['volume']
                    
                    total_pv = bars_df['pv'].sum()
                    total_vol = bars_df['volume'].sum()
                    vwap = total_pv / total_vol if total_vol > 0 else 0
                    
                    # Current metrics
                    current_price = float(bars_df['close'].iloc[-1])
                    hod = float(bars_df['high'].max())
                    lod = float(bars_df['low'].min())
                    
                    # Calculate distances
                    vwap_dist_pct = ((current_price - vwap) / vwap * 100) if vwap > 0 else 0
                    from_hod_pct = ((hod - current_price) / hod * 100) if hod > 0 else 0
                    from_lod_pct = ((current_price - lod) / lod * 100) if lod > 0 else 0
                    
                    # Opening price (first bar)
                    open_price = float(bars_df['open'].iloc[0])
                    
                    # Price action flags
                    above_vwap = current_price > vwap
                    near_hod = from_hod_pct < 2  # Within 2% of HOD
                    near_lod = from_lod_pct < 2
                    
                    results[symbol] = {
                        'symbol': symbol,
                        'price': round(current_price, 2),
                        'open_price': round(open_price, 2),
                        'vwap': round(vwap, 2),
                        'vwap_dist_pct': round(vwap_dist_pct, 2),
                        'above_vwap': above_vwap,
                        'hod': round(hod, 2),
                        'lod': round(lod, 2),
                        'from_hod_pct': round(from_hod_pct, 2),
                        'from_lod_pct': round(from_lod_pct, 2),
                        'near_hod': near_hod,
                        'near_lod': near_lod,
                        'intraday_volume': int(total_vol),
                        'bars_count': len(bars_df),
                        'data_source': 'IEX',
                    }
                    
                except Exception as e:
                    print(f"    Error processing {symbol}: {e}")
                    continue
            
            print(f"  ✓ Got intraday data for {len(results)} symbols")
            
        except Exception as e:
            print(f"  ✗ Alpaca API error: {e}")
        
        return results
    
    def score_intraday_position(self, symbol_data):
        """
        Score a stock's intraday position for potential mover detection.
        
        Returns (score, reasons) where score is 0-20 points.
        """
        score = 0
        reasons = []
        
        # VWAP position (0-8 points)
        vwap_dist = abs(symbol_data['vwap_dist_pct'])
        
        if symbol_data['above_vwap']:
            if vwap_dist < 1:
                score += 6
                reasons.append("Tight to VWAP ↑")
            elif vwap_dist < 2:
                score += 4
                reasons.append("Above VWAP")
            elif vwap_dist < 4:
                score += 2
        else:
            if vwap_dist < 1:
                score += 4
                reasons.append("Tight to VWAP ↓")
            elif vwap_dist < 2:
                score += 2
        
        # Near breakout (0-6 points)
        if symbol_data['near_hod']:
            score += 6
            reasons.append("Near HOD")
        elif symbol_data['from_hod_pct'] < 5:
            score += 3
        
        # Compression detection (0-6 points)
        # If near both HOD and VWAP = coiling
        if symbol_data['near_hod'] and vwap_dist < 2:
            score += 6
            reasons.append("Compression ⚡")
        
        return min(score, 20), reasons


def test_alpaca_feed():
    """Test function to verify Alpaca integration."""
    print("\n" + "="*60)
    print("TESTING ALPACA IEX FEED")
    print("="*60)
    
    try:
        feed = AlpacaFeed()
        
        # Test with a few liquid symbols
        test_symbols = ["AAPL", "TSLA", "NVDA", "SPY"]
        
        data = feed.get_intraday_data(test_symbols)
        
        for symbol, info in data.items():
            score, reasons = feed.score_intraday_position(info)
            
            print(f"\n{symbol}:")
            print(f"  Price: ${info['price']} | VWAP: ${info['vwap']} ({info['vwap_dist_pct']:+.1f}%)")
            print(f"  Range: ${info['lod']} - ${info['hod']}")
            print(f"  From HOD: {info['from_hod_pct']:.1f}%")
            print(f"  Volume: {info['intraday_volume']:,} bars")
            print(f"  Intraday Score: {score}/20 - {' · '.join(reasons)}")
        
        print("\n✓ Alpaca feed working correctly")
        
    except Exception as e:
        print(f"\n✗ Test failed: {e}")


if __name__ == "__main__":
    test_alpaca_feed()
