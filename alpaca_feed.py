"""
alpaca_feed.py
---------------
Alpaca SIP intraday market-data client for Elite Scanner.

Purpose:
  - Provide consolidated Alpaca SIP intraday bars for elite_scanner.py.
  - Keep the public interface expected by the scanner:
      * AlpacaFeed()
      * get_intraday_data(symbols)
      * get_intraday_snapshot(symbol)
      * score_intraday_position(snapshot)

Default feed:
  - ALPACA_DATA_FEED=sip
  - Falls back only when Alpaca/API rejects the requested feed and fallback is enabled.

Environment variables accepted:
  - ALPACA_API_KEY / ALPACA_SECRET_KEY
  - APCA_API_KEY_ID / APCA_API_SECRET_KEY
  - ALPACA_DATA_FEED=sip
"""

from __future__ import annotations

import os
import math
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


class AlpacaFeed:
    """
    Minimal, reliable Alpaca market-data client.

    This class intentionally uses raw HTTP requests instead of alpaca-py so the
    VPS dependency surface stays small and the scanner can keep running with
    only requests + pandas installed.
    """

    BASE_URL = "https://data.alpaca.markets/v2"

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        feed: Optional[str] = None,
        timeout: int = 15,
        allow_iex_fallback: Optional[bool] = None,
    ) -> None:
        self.api_key = (
            api_key
            or os.getenv("ALPACA_API_KEY")
            or os.getenv("APCA_API_KEY_ID")
            or ""
        ).strip()

        self.api_secret = (
            api_secret
            or os.getenv("ALPACA_SECRET_KEY")
            or os.getenv("APCA_API_SECRET_KEY")
            or ""
        ).strip()

        self.feed = (feed or os.getenv("ALPACA_DATA_FEED", "sip") or "sip").strip().lower()
        if self.feed not in {"sip", "iex"}:
            self.feed = "sip"

        # Default: do NOT silently downgrade premium SIP to IEX. If the user
        # explicitly wants fallback, set ALPACA_ALLOW_IEX_FALLBACK=1.
        if allow_iex_fallback is None:
            allow_iex_fallback = os.getenv("ALPACA_ALLOW_IEX_FALLBACK", "0").strip().lower() in {
                "1", "true", "yes", "y"
            }
        self.allow_iex_fallback = bool(allow_iex_fallback)

        self.timeout = timeout

        self.headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ny_now() -> datetime:
        if ZoneInfo:
            return datetime.now(ZoneInfo("America/New_York"))
        return datetime.now()

    @staticmethod
    def _to_utc_iso(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_ts(value: Any) -> Optional[datetime]:
        if not value:
            return None
        text = str(value).replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except Exception:
            return None

    @staticmethod
    def _et_label(value: Any) -> str:
        dt = AlpacaFeed._parse_ts(value)
        if not dt:
            return str(value or "")
        try:
            if ZoneInfo:
                dt = dt.astimezone(ZoneInfo("America/New_York"))
            return dt.isoformat(timespec="seconds")
        except Exception:
            return str(value or "")

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            if isinstance(value, str) and value.strip() == "":
                return default
            if pd.isna(value):
                return default
            val = float(value)
            if math.isnan(val) or math.isinf(val):
                return default
            return val
        except Exception:
            return default

    @staticmethod
    def _chunks(items: List[str], size: int) -> Iterable[List[str]]:
        for i in range(0, len(items), size):
            yield items[i : i + size]

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _get(self, path: str, params: Dict[str, Any], feed: Optional[str] = None) -> Dict[str, Any]:
        if not self._has_credentials():
            raise RuntimeError("Missing Alpaca credentials: set ALPACA_API_KEY and ALPACA_SECRET_KEY")

        params = dict(params or {})
        params["feed"] = feed or self.feed

        url = f"{self.BASE_URL}{path}"
        r = requests.get(url, headers=self.headers, params=params, timeout=self.timeout)

        if r.status_code >= 400 and self.allow_iex_fallback and params.get("feed") == "sip":
            params["feed"] = "iex"
            r = requests.get(url, headers=self.headers, params=params, timeout=self.timeout)

        if r.status_code >= 400:
            body = r.text[:500] if getattr(r, "text", None) else ""
            raise RuntimeError(f"Alpaca API error {r.status_code}: {body}")

        return r.json() if r.content else {}

    # ------------------------------------------------------------------
    # Public scanner interface
    # ------------------------------------------------------------------

    def get_intraday_data(self, symbols: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """
        Return intraday data keyed by symbol.

        Expected by elite_scanner.py:
            rt["last_price"]
            rt["vwap"]
            rt["vwap_dist_pct"]
            rt["above_vwap"]
            rt["hod"]
            rt["lod"]
            rt["from_hod_pct"]
            rt["near_hod"]
            rt["intraday_volume"]
            rt["last_bar_time"]
        """
        clean_symbols = []
        seen = set()

        for s in symbols or []:
            sym = str(s or "").strip().upper()
            if not sym:
                continue
            # Alpaca accepts class symbols such as BRK.B as BRK.B.
            if sym not in seen:
                seen.add(sym)
                clean_symbols.append(sym)

        if not clean_symbols:
            return {}

        if not self._has_credentials():
            print("  ⚠️ Missing Alpaca credentials; skipping Alpaca SIP enrichment")
            return {}

        out: Dict[str, Dict[str, Any]] = {}

        # Alpaca supports multi-symbol bars. Keep chunks conservative.
        for chunk in self._chunks(clean_symbols, 50):
            try:
                chunk_data = self._get_intraday_bars_chunk(chunk)
                out.update(chunk_data)
            except Exception as exc:
                print(f"  ⚠️ Alpaca SIP chunk failed ({','.join(chunk[:5])}...): {exc}")

        return out

    def get_intraday_snapshot(self, symbol: str) -> Dict[str, Any]:
        """
        Single-symbol compatibility helper used by older audit/tests.

        Returns one snapshot dictionary, or {} if unavailable.
        """
        sym = str(symbol or "").strip().upper()
        if not sym:
            return {}
        return self.get_intraday_data([sym]).get(sym, {})

    def _get_intraday_bars_chunk(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        now_ny = self._ny_now()

        # During premarket/open, include enough window to catch current session bars.
        session_start = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
        if now_ny.time() < dtime(4, 0):
            session_start = session_start - timedelta(days=1)

        # If weekend, still request recent 2 days so latest data can exist.
        if now_ny.weekday() >= 5:
            session_start = now_ny - timedelta(days=3)

        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Min",
            "start": self._to_utc_iso(session_start),
            "end": self._to_utc_iso(now_ny),
            "limit": 10000,
            "adjustment": "raw",
            "sort": "asc",
        }

        data = self._get("/stocks/bars", params=params)
        bars_by_symbol = data.get("bars", {}) or {}

        out: Dict[str, Dict[str, Any]] = {}

        for symbol in symbols:
            rows = bars_by_symbol.get(symbol, []) or []
            if not rows:
                continue

            # Convert Alpaca bar rows. Expected keys: t,o,h,l,c,v,vw,n
            df = pd.DataFrame(rows)
            if df.empty:
                continue

            for col in ["o", "h", "l", "c", "v", "vw"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.dropna(subset=["c"])
            if df.empty:
                continue

            latest = df.iloc[-1]
            last_price = self._safe_float(latest.get("c"))
            if last_price <= 0:
                continue

            intraday_volume = self._safe_float(df.get("v", pd.Series(dtype=float)).sum())

            if "vw" in df.columns and pd.to_numeric(df["vw"], errors="coerce").notna().any():
                # Session VWAP from per-bar VWAP weighted by bar volume.
                vol = pd.to_numeric(df.get("v", pd.Series([0] * len(df))), errors="coerce").fillna(0)
                vw = pd.to_numeric(df["vw"], errors="coerce").fillna(0)
                valid = (vol > 0) & (vw > 0)
                if valid.any() and vol[valid].sum() > 0:
                    vwap = float((vw[valid] * vol[valid]).sum() / vol[valid].sum())
                else:
                    vwap = self._safe_float(latest.get("vw"), last_price)
            else:
                # Fallback typical-price VWAP approximation.
                high = pd.to_numeric(df.get("h", pd.Series(dtype=float)), errors="coerce")
                low = pd.to_numeric(df.get("l", pd.Series(dtype=float)), errors="coerce")
                close = pd.to_numeric(df.get("c", pd.Series(dtype=float)), errors="coerce")
                vol = pd.to_numeric(df.get("v", pd.Series(dtype=float)), errors="coerce").fillna(0)
                typical = (high + low + close) / 3.0
                valid = (vol > 0) & typical.notna()
                vwap = float((typical[valid] * vol[valid]).sum() / vol[valid].sum()) if valid.any() and vol[valid].sum() > 0 else last_price

            hod = self._safe_float(pd.to_numeric(df.get("h", pd.Series(dtype=float)), errors="coerce").max(), last_price)
            lod = self._safe_float(pd.to_numeric(df.get("l", pd.Series(dtype=float)), errors="coerce").min(), last_price)

            vwap_dist_pct = ((last_price - vwap) / vwap * 100.0) if vwap > 0 else 0.0
            from_hod_pct = ((last_price - hod) / hod * 100.0) if hod > 0 else 0.0

            last_bar_time = self._et_label(latest.get("t"))

            out[symbol] = {
                "symbol": symbol,
                "feed": self.feed.upper(),
                "data_source": f"Alpaca {self.feed.upper()}",
                "price_source": f"Alpaca {self.feed.upper()}",
                "last_price": round(last_price, 4),
                "intraday_last_price": round(last_price, 4),
                "vwap": round(vwap, 4),
                "vwap_dist_pct": round(vwap_dist_pct, 2),
                "above_vwap": bool(last_price >= vwap) if vwap > 0 else False,
                "hod": round(hod, 4),
                "lod": round(lod, 4),
                "from_hod_pct": round(from_hod_pct, 2),
                "hod_distance_pct": round(from_hod_pct, 2),
                "near_hod": bool(from_hod_pct >= -1.0),
                "intraday_volume": int(intraday_volume) if intraday_volume > 0 else 0,
                "last_bar_time": last_bar_time,
                "price_updated_at": last_bar_time,
                "bar_count": int(len(df)),
            }

        return out

    def score_intraday_position(self, rt: Dict[str, Any]) -> Tuple[int, List[str]]:
        """
        Conservative intraday score overlay for elite_scanner.py.

        Returns:
          score: int 0-20
          reasons: list[str]
        """
        if not isinstance(rt, dict) or not rt:
            return 0, []

        score = 0
        reasons: List[str] = []

        last_price = self._safe_float(rt.get("last_price"), 0)
        vwap = self._safe_float(rt.get("vwap"), 0)
        vwap_dist = self._safe_float(rt.get("vwap_dist_pct"), 0)
        from_hod = self._safe_float(rt.get("from_hod_pct"), -999)
        intraday_volume = self._safe_float(rt.get("intraday_volume"), 0)

        above_vwap = bool(rt.get("above_vwap"))

        if last_price <= 0:
            return 0, []

        if above_vwap and 0 <= vwap_dist <= 3.0:
            score += 5
            reasons.append("Above VWAP")
        elif above_vwap and 3.0 < vwap_dist <= 5.0:
            score += 2
            reasons.append("Above VWAP extended")
        elif vwap > 0 and last_price < vwap:
            score -= 2
            reasons.append("Below VWAP")

        if from_hod >= -0.75:
            score += 5
            reasons.append("Near HOD")
        elif from_hod >= -1.5:
            score += 3
            reasons.append("Close to HOD")
        elif from_hod <= -3.0:
            score -= 2
            reasons.append("Fading from HOD")

        if intraday_volume >= 1_000_000:
            score += 4
            reasons.append("Strong intraday volume")
        elif intraday_volume >= 250_000:
            score += 2
            reasons.append("Usable intraday volume")

        if above_vwap and from_hod >= -1.5:
            score += 4
            reasons.append("Clean intraday structure")

        score = max(0, min(20, int(round(score))))
        return score, reasons[:6]


if __name__ == "__main__":
    # Lightweight self-test without exposing credentials.
    feed = AlpacaFeed()
    print(f"AlpacaFeed initialized | feed={feed.feed} | credentials={'yes' if feed._has_credentials() else 'no'}")
