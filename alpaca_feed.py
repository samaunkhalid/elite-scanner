"""
alpaca_feed.py
---------------
Alpaca IEX real-time / intraday enrichment for elite_scanner.py.

Uses:
- GitHub Secrets / environment variables:
  ALPACA_API_KEY
  ALPACA_SECRET_KEY

Data:
- IEX feed only
- Non-consolidated volume
- Good for live price/VWAP proxy/HOD/LOD scanning prototype
"""

import os
import time
import requests
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


class AlpacaFeed:
    def __init__(self):
        self.api_key = os.getenv("ALPACA_API_KEY")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY")

        self.base_url = "https://data.alpaca.markets/v2"

        self.headers = {
            "APCA-API-KEY-ID": self.api_key or "",
            "APCA-API-SECRET-KEY": self.secret_key or "",
        }

        if not self.api_key or not self.secret_key:
            print("  ⚠️ Alpaca credentials missing inside AlpacaFeed")

    def _ny_now(self):
        if ZoneInfo:
            return datetime.now(ZoneInfo("America/New_York"))
        return datetime.now()

    def _get_intraday_window_utc(self):
        """
        Use today's US session window from 4:00 AM ET to now.
        Works for pre-market, regular session, and after-hours.
        """
        now_ny = self._ny_now()

        # If it is very early before 4 AM ET, use prior calendar day.
        # This is simple; later you can improve weekend/holiday handling.
        session_date = now_ny.date()
        if now_ny.hour < 4:
            session_date = session_date - timedelta(days=1)

        if ZoneInfo:
            start_ny = datetime(
                session_date.year,
                session_date.month,
                session_date.day,
                4,
                0,
                tzinfo=ZoneInfo("America/New_York"),
            )
            end_ny = now_ny
            start_utc = start_ny.astimezone(timezone.utc)
            end_utc = end_ny.astimezone(timezone.utc)
        else:
            # Fallback: approximate ET as UTC-5/UTC-4 not handled perfectly.
            # This should rarely be used on GitHub runners with Python 3.11+.
            start_utc = datetime.utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
            end_utc = datetime.utcnow()

        return start_utc.isoformat().replace("+00:00", "Z"), end_utc.isoformat().replace("+00:00", "Z")

    def _chunks(self, symbols, size=20):
        for i in range(0, len(symbols), size):
            yield symbols[i:i + size]

    def _fetch_bars_batch(self, symbols):
        """
        Fetch 1-minute IEX bars for a batch of symbols.
        Handles Alpaca pagination.
        """
        if not symbols:
            return {}

        start, end = self._get_intraday_window_utc()
        url = f"{self.base_url}/stocks/bars"

        all_bars = {}
        page_token = None

        while True:
            params = {
                "symbols": ",".join(symbols),
                "timeframe": "1Min",
                "start": start,
                "end": end,
                "limit": 10000,
                "adjustment": "raw",
                "feed": "iex",
                "sort": "asc",
            }

            if page_token:
                params["page_token"] = page_token

            try:
                r = requests.get(
                    url,
                    headers=self.headers,
                    params=params,
                    timeout=20,
                )

                if r.status_code != 200:
                    print(f"  ⚠️ Alpaca bars error {r.status_code}: {r.text[:300]}")
                    return all_bars

                data = r.json()
                bars = data.get("bars", {}) or {}

                for sym, sym_bars in bars.items():
                    if sym not in all_bars:
                        all_bars[sym] = []
                    all_bars[sym].extend(sym_bars or [])

                page_token = data.get("next_page_token")
                if not page_token:
                    break

                time.sleep(0.15)

            except Exception as e:
                print(f"  ⚠️ Alpaca bars request failed: {e}")
                break

        return all_bars

    def get_intraday_data(self, symbols):
        """
        Return dict keyed by symbol with:
        vwap, above_vwap, hod/lod, from_hod_pct, near_hod, volume, etc.
        """
        if not self.api_key or not self.secret_key:
            print("  ⚠️ Alpaca keys unavailable; returning empty intraday data")
            return {}

        symbols = [s for s in symbols if isinstance(s, str) and s.strip()]
        symbols = list(dict.fromkeys(symbols))  # de-duplicate, preserve order

        print(f"  Requesting Alpaca IEX bars for {len(symbols)} symbols...")

        output = {}

        for batch in self._chunks(symbols, size=20):
            batch_bars = self._fetch_bars_batch(batch)

            for symbol, bars in batch_bars.items():
                if not bars:
                    continue

                # Sort just in case
                bars = sorted(bars, key=lambda x: x.get("t", ""))

                last = bars[-1]
                last_price = last.get("c")
                if not last_price:
                    continue

                highs = [b.get("h", 0) for b in bars if b.get("h") is not None]
                lows = [b.get("l", 0) for b in bars if b.get("l") is not None]
                vols = [b.get("v", 0) or 0 for b in bars]

                hod = max(highs) if highs else last_price
                lod = min(lows) if lows else last_price
                intraday_volume = sum(vols)

                # VWAP proxy from Alpaca bar VWAP if available; otherwise close*volume.
                vwap_num = 0.0
                vwap_den = 0.0

                for b in bars:
                    v = b.get("v", 0) or 0
                    vw = b.get("vw")
                    c = b.get("c")

                    if v > 0:
                        if vw is not None:
                            vwap_num += float(vw) * v
                        elif c is not None:
                            vwap_num += float(c) * v
                        vwap_den += v

                vwap = (vwap_num / vwap_den) if vwap_den > 0 else last_price

                vwap_dist_pct = ((last_price - vwap) / vwap * 100) if vwap else 0
                above_vwap = last_price >= vwap if vwap else False

                from_hod_pct = ((last_price - hod) / hod * 100) if hod else 0
                near_hod = from_hod_pct >= -1.0

                day_range = hod - lod
                if day_range > 0:
                    range_position = (last_price - lod) / day_range
                else:
                    range_position = 0.5

                # Simple compression proxy: last 20 bars range vs price
                recent_bars = bars[-20:] if len(bars) >= 20 else bars
                recent_high = max([b.get("h", 0) for b in recent_bars if b.get("h") is not None], default=last_price)
                recent_low = min([b.get("l", 0) for b in recent_bars if b.get("l") is not None], default=last_price)
                recent_range_pct = ((recent_high - recent_low) / last_price * 100) if last_price else 0

                output[symbol] = {
                    "last_price": round(float(last_price), 4),
                    "vwap": round(float(vwap), 4),
                    "vwap_dist_pct": round(float(vwap_dist_pct), 2),
                    "above_vwap": bool(above_vwap),
                    "hod": round(float(hod), 4),
                    "lod": round(float(lod), 4),
                    "from_hod_pct": round(float(from_hod_pct), 2),
                    "near_hod": bool(near_hod),
                    "range_position": round(float(range_position), 3),
                    "intraday_volume": int(intraday_volume),
                    "recent_range_pct": round(float(recent_range_pct), 2),
                    "bar_count": len(bars),
                    "last_bar_time": last.get("t"),
                    "source": "alpaca_iex",
                }

            time.sleep(0.2)

        print(f"  Alpaca returned intraday data for {len(output)} symbols")
        return output

    def score_intraday_position(self, rt):
        """
        Intraday score: 0-20.
        This rewards potential continuation, not just biggest movers.
        """
        score = 0
        reasons = []

        above_vwap = rt.get("above_vwap", False)
        vwap_dist = rt.get("vwap_dist_pct", 0) or 0
        range_pos = rt.get("range_position", 0.5) or 0.5
        near_hod = rt.get("near_hod", False)
        from_hod = rt.get("from_hod_pct", 0) or 0
        recent_range_pct = rt.get("recent_range_pct", 99) or 99
        bar_count = rt.get("bar_count", 0) or 0

        # Need enough bars to trust the intraday read
        if bar_count < 5:
            return 0, ["IEX data thin"]

        # VWAP control
        if above_vwap and 0 <= vwap_dist <= 4:
            score += 5
            reasons.append("Above VWAP")
        elif above_vwap:
            score += 2
            reasons.append("Above VWAP extended")

        # Range position
        if range_pos >= 0.75:
            score += 5
            reasons.append("Upper range")
        elif range_pos >= 0.60:
            score += 3

        # Near HOD, but not too far stretched
        if near_hod and from_hod >= -1.0:
            score += 4
            reasons.append("Near HOD")

        # Compression near highs
        if recent_range_pct <= 1.0 and range_pos >= 0.60:
            score += 4
            reasons.append("Tight consolidation")
        elif recent_range_pct <= 1.8 and range_pos >= 0.60:
            score += 2
            reasons.append("Consolidating")

        # Avoid huge VWAP extension
        if vwap_dist > 8:
            score -= 4
            reasons.append("Far above VWAP")

        return max(0, min(score, 20)), reasons
