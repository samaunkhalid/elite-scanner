#!/usr/bin/env python3
"""
swing_live_universe_builder.py
------------------------------

Live all-universe Swing Desk universe builder.

Purpose:
- Build the Swing Desk universe from Alpaca's active/tradable US equity assets.
- Apply conservative swing filters before the bar updater/scanner runs:
    active/tradable/common equity
    primary US exchange
    $5-$200 default price discipline
    liquidity / dollar-volume floor
    SMA50/SMA200 trend survival
    ATR / panic-risk filter
- Write swing_results/live_swing_universe.csv and a JSON audit file.
- Does not touch day-trade production files and does not route orders.

This file is intentionally independent from the day-trade scanner.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
import requests

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


VERSION = "swing_live_universe_builder_v1.0.2_alpaca_class_field_fix"

PROJECT_DIR = Path(os.getenv("ELITE_PROJECT_DIR", Path(__file__).resolve().parent)).resolve()
DEFAULT_OUTPUT = PROJECT_DIR / "swing_results" / "live_swing_universe.csv"

DATA_BASE_URL = "https://data.alpaca.markets/v2"
DEFAULT_TRADING_BASE_URL = "https://paper-api.alpaca.markets"

PRIMARY_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "NYSEARCA", "ARCA", "BATS"}

NON_COMMON_NAME_PATTERNS = [
    r"\bWARRANTS?\b",
    r"\bRIGHTS?\b",
    r"\bUNITS?\b",
    r"\bPREFERRED\b",
    r"\bPREFERENCE\b",
    r"\bDEPOSITARY\b",
    r"\bNOTES?\s+DUE\b",
    r"\bSENIOR\s+NOTES?\b",
    r"\bSUBORDINATED\s+NOTES?\b",
    r"\bBOND\b",
    r"\bETF\b",
    r"\bETN\b",
    r"\bFUND\b",
    r"\bINDEX\b",
    r"\bACQUISITION\s+CORP\b",
    r"\bSPAC\b",
]


def et_tz():
    if ZoneInfo:
        return ZoneInfo("America/New_York")
    return timezone.utc


def now_et() -> datetime:
    return datetime.now(et_tz())


def utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def clean_symbol(value: Any) -> str:
    sym = str(value or "").strip().upper()
    if not sym:
        return ""
    # Keep production file handling simple and avoid symbols that create path/API edge cases.
    if any(ch in sym for ch in ["^", "=", "/", " "]):
        return ""
    if "." in sym:
        return ""
    if len(sym) > 8:
        return ""
    if not re.match(r"^[A-Z][A-Z0-9]*$", sym):
        return ""
    return sym


def chunked(items: Sequence[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(items), max(1, size)):
        yield list(items[i : i + max(1, size)])


def credentials() -> Tuple[str, str]:
    key = (
        os.getenv("ALPACA_API_KEY")
        or os.getenv("APCA_API_KEY_ID")
        or os.getenv("ALPACA_KEY_ID")
        or ""
    ).strip()
    secret = (
        os.getenv("ALPACA_SECRET_KEY")
        or os.getenv("APCA_API_SECRET_KEY")
        or os.getenv("ALPACA_API_SECRET")
        or ""
    ).strip()
    if not key or not secret:
        raise RuntimeError("Missing Alpaca credentials. Load ALPACA_API_KEY/ALPACA_SECRET_KEY or APCA_API_KEY_ID/APCA_API_SECRET_KEY.")
    return key, secret


def headers() -> Dict[str, str]:
    key, secret = credentials()
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def trading_base_url() -> str:
    raw = (
        os.getenv("ALPACA_TRADING_BASE_URL")
        or os.getenv("APCA_API_BASE_URL")
        or os.getenv("ALPACA_BASE_URL")
        or DEFAULT_TRADING_BASE_URL
    ).strip().rstrip("/")

    # Some VPS/systemd configs store APCA_API_BASE_URL with a trailing /v2.
    # Alpaca Trading API asset endpoint is /v2/assets, so normalize here to
    # avoid accidentally calling /v2/v2/assets and getting HTTP 404.
    if raw.endswith("/v2"):
        raw = raw[:-3].rstrip("/")
    return raw


def request_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30, retries: int = 3) -> Any:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers(), params=params or {}, timeout=timeout)
            if r.status_code == 429 and attempt < retries:
                time.sleep(2.0 * attempt)
                continue
            if r.status_code >= 400:
                body = r.text[:500] if getattr(r, "text", None) else ""
                raise RuntimeError(f"HTTP {r.status_code}: {body}")
            return r.json() if r.content else {}
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
                continue
    raise RuntimeError(str(last_error) if last_error else "request failed")


def fetch_assets() -> List[Dict[str, Any]]:
    url = f"{trading_base_url()}/v2/assets"
    params = {
        "status": "active",
        "asset_class": "us_equity",
    }
    data = request_json(url, params=params, timeout=45)
    return data if isinstance(data, list) else []


def is_common_equity_asset(asset: Dict[str, Any], include_etfs: bool = False) -> Tuple[bool, str]:
    symbol = clean_symbol(asset.get("symbol"))
    if not symbol:
        return False, "bad_symbol"

    status = str(asset.get("status", "")).lower()
    asset_class = str(asset.get("asset_class") or asset.get("class") or "").lower()
    exchange = str(asset.get("exchange", "")).upper()
    tradable = bool(asset.get("tradable", False))

    if status != "active":
        return False, "inactive"
    if asset_class not in {"us_equity", "us_equities"}:
        return False, "not_us_equity"
    if not tradable:
        return False, "not_tradable"
    if exchange and exchange not in PRIMARY_EXCHANGES:
        return False, f"exchange_{exchange}"

    name = str(asset.get("name") or "").upper()
    if not include_etfs:
        for pat in NON_COMMON_NAME_PATTERNS:
            if re.search(pat, name):
                return False, "non_common_name"

    return True, "ok"


def fetch_snapshots(symbols: Sequence[str], feed: str, chunk_size: int = 200) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    snapshots: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}

    for chunk in chunked(list(symbols), chunk_size):
        params = {
            "symbols": ",".join(chunk),
            "feed": feed,
        }
        try:
            data = request_json(f"{DATA_BASE_URL}/stocks/snapshots", params=params, timeout=45)
            raw = data.get("snapshots", data) if isinstance(data, dict) else {}
            if isinstance(raw, dict):
                for sym, rec in raw.items():
                    if isinstance(rec, dict):
                        snapshots[clean_symbol(sym)] = rec
        except Exception as exc:
            errors[",".join(chunk[:5])] = repr(exc)

    return snapshots, errors


def snapshot_price_volume(snapshot: Dict[str, Any]) -> Tuple[float, float]:
    latest_trade = snapshot.get("latestTrade") or {}
    minute_bar = snapshot.get("minuteBar") or {}
    daily_bar = snapshot.get("dailyBar") or {}
    prev_daily_bar = snapshot.get("prevDailyBar") or {}

    price = safe_float(
        latest_trade.get("p")
        or minute_bar.get("c")
        or daily_bar.get("c")
        or prev_daily_bar.get("c")
    )
    volume = safe_float(daily_bar.get("v") or prev_daily_bar.get("v"))
    return price, volume


def fetch_daily_bars(
    symbols: Sequence[str],
    feed: str,
    lookback_days: int,
    chunk_size: int,
    adjustment: str = "raw",
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, str]]:
    start = datetime.now(timezone.utc) - timedelta(days=max(260, lookback_days))
    end = datetime.now(timezone.utc)

    all_bars: Dict[str, List[Dict[str, Any]]] = {s: [] for s in symbols}
    errors: Dict[str, str] = {}

    for chunk in chunked(list(symbols), chunk_size):
        page_token = None
        page_count = 0

        while True:
            params: Dict[str, Any] = {
                "symbols": ",".join(chunk),
                "timeframe": "1Day",
                "start": utc_iso(start),
                "end": utc_iso(end),
                "adjustment": adjustment,
                "feed": feed,
                "limit": 10000,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token

            try:
                data = request_json(f"{DATA_BASE_URL}/stocks/bars", params=params, timeout=60)
            except Exception as exc:
                errors[",".join(chunk[:5])] = repr(exc)
                break

            bars_by_symbol = data.get("bars", {}) if isinstance(data, dict) else {}
            if isinstance(bars_by_symbol, dict):
                for sym, rows in bars_by_symbol.items():
                    cs = clean_symbol(sym)
                    if not cs:
                        continue
                    if isinstance(rows, list):
                        all_bars.setdefault(cs, []).extend([r for r in rows if isinstance(r, dict)])

            page_count += 1
            page_token = data.get("next_page_token") if isinstance(data, dict) else None
            if not page_token:
                break
            if page_count >= 25:
                errors[",".join(chunk[:5])] = "max pagination pages reached"
                break

    return all_bars, errors


def bars_to_daily_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    mapping = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    for old, new in mapping.items():
        if old in df.columns and new not in df.columns:
            df[new] = df[old]

    if "t" in df.columns:
        ts = pd.to_datetime(df["t"], utc=True, errors="coerce")
        try:
            df["date_et"] = ts.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
        except Exception:
            df["date_et"] = ts.dt.strftime("%Y-%m-%d")
    elif "date_et" not in df.columns:
        df["date_et"] = pd.to_datetime(df.index, errors="coerce").strftime("%Y-%m-%d")

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df.get(col), errors="coerce")

    df = df.dropna(subset=["date_et", "open", "high", "low", "close"])
    if df.empty:
        return df

    return df[["date_et", "open", "high", "low", "close", "volume"]].sort_values("date_et").drop_duplicates("date_et").reset_index(drop=True)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["sma20"] = out["close"].rolling(20).mean()
    out["sma50"] = out["close"].rolling(50).mean()
    out["sma200"] = out["close"].rolling(200).mean()
    out["avg_volume_20d"] = out["volume"].rolling(20).mean()
    out["avg_dollar_volume_20d"] = (out["close"] * out["volume"]).rolling(20).mean()

    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()
    out["atr_pct"] = out["atr14"] / out["close"] * 100.0

    delta = out["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, pd.NA)
    out["rsi_14"] = 100 - (100 / (1 + rs))
    out["ret_5d_pct"] = out["close"].pct_change(5) * 100.0
    out["ret_20d_pct"] = out["close"].pct_change(20) * 100.0
    return out


def evaluate_symbol(
    symbol: str,
    asset: Dict[str, Any],
    bars: List[Dict[str, Any]],
    args: argparse.Namespace,
) -> Tuple[Optional[Dict[str, Any]], str]:
    df = add_indicators(bars_to_daily_df(bars))
    if df.empty or len(df) < int(args.min_history_days):
        return None, "insufficient_history"

    latest = df.iloc[-1]
    close = safe_float(latest.get("close"))
    volume = safe_float(latest.get("volume"))
    avg_volume = safe_float(latest.get("avg_volume_20d"))
    avg_dollar = safe_float(latest.get("avg_dollar_volume_20d"))
    atr_pct = safe_float(latest.get("atr_pct"))
    rsi = safe_float(latest.get("rsi_14"))
    sma20 = safe_float(latest.get("sma20"))
    sma50 = safe_float(latest.get("sma50"))
    sma200 = safe_float(latest.get("sma200"))
    ret5 = safe_float(latest.get("ret_5d_pct"))
    ret20 = safe_float(latest.get("ret_20d_pct"))

    if close < args.min_price or close > args.max_price:
        return None, "price_filter"
    if avg_volume < args.min_avg_volume and avg_dollar < args.min_avg_dollar_volume:
        return None, "liquidity_filter"
    if sma200 <= 0 or close < sma50 and close < sma200:
        return None, "trend_filter"
    if atr_pct <= 0 or atr_pct > args.max_atr_pct:
        return None, "atr_filter"
    if rsi and (rsi < args.min_rsi or rsi > args.max_rsi):
        return None, "rsi_filter"

    # Broken/panic names are not useful for a directional swing shortlist.
    if ret5 <= -12.0 and close < sma20:
        return None, "panic_5d_filter"
    if ret20 <= -22.0 and close < sma50:
        return None, "panic_20d_filter"

    liquidity_score = min(30.0, (avg_dollar / max(args.min_avg_dollar_volume, 1.0)) * 12.0)
    trend_score = 0.0
    if close > sma20:
        trend_score += 8.0
    if close > sma50:
        trend_score += 10.0
    if close > sma200:
        trend_score += 8.0
    if sma50 > sma200:
        trend_score += 6.0
    volatility_score = max(0.0, 18.0 - abs(atr_pct - 3.0) * 2.0)
    momentum_score = 0.0
    if ret5 > 0:
        momentum_score += 6.0
    if ret20 > 0:
        momentum_score += 6.0
    if 42 <= rsi <= 68:
        momentum_score += 6.0
    if volume >= avg_volume:
        momentum_score += 4.0

    universe_score = liquidity_score + trend_score + volatility_score + momentum_score

    return {
        "symbol": symbol,
        "name": asset.get("name", ""),
        "exchange": asset.get("exchange", ""),
        "asset_class": asset.get("asset_class") or asset.get("class", ""),
        "tradable": bool(asset.get("tradable", False)),
        "shortable": bool(asset.get("shortable", False)),
        "marginable": bool(asset.get("marginable", False)),
        "latest_date_et": str(latest.get("date_et", "")),
        "price": round(close, 4),
        "volume": round(volume, 0),
        "avg_volume_20d": round(avg_volume, 0),
        "avg_dollar_volume_20d": round(avg_dollar, 0),
        "atr_pct": round(atr_pct, 4),
        "rsi_14": round(rsi, 4),
        "sma20": round(sma20, 4),
        "sma50": round(sma50, 4),
        "sma200": round(sma200, 4),
        "ret_5d_pct": round(ret5, 4),
        "ret_20d_pct": round(ret20, 4),
        "universe_score": round(universe_score, 4),
    }, "ok"


def build_universe(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    print("=== LIVE SWING UNIVERSE BUILDER ===")
    print(f"Version: {VERSION}")
    print(f"Trading base: {trading_base_url()}")
    print(f"Data feed: {args.feed.upper()}")

    assets = fetch_assets()
    print(f"Assets fetched: {len(assets)}")

    assets_by_symbol: Dict[str, Dict[str, Any]] = {}
    asset_rejects: Dict[str, int] = {}
    for asset in assets:
        ok, reason = is_common_equity_asset(asset, include_etfs=args.include_etfs)
        if not ok:
            asset_rejects[reason] = asset_rejects.get(reason, 0) + 1
            continue
        sym = clean_symbol(asset.get("symbol"))
        if sym:
            assets_by_symbol[sym] = asset

    symbols = sorted(assets_by_symbol)
    if args.limit_assets:
        symbols = symbols[: args.limit_assets]

    print(f"Active/tradable equity symbols after asset filter: {len(symbols)}")

    snapshot_map, snapshot_errors = fetch_snapshots(symbols, feed=args.feed, chunk_size=args.snapshot_chunk_size)
    print(f"Snapshots received: {len(snapshot_map)}")

    prefiltered: List[Tuple[str, float, float, float]] = []
    snapshot_rejects: Dict[str, int] = {}

    for sym in symbols:
        snap = snapshot_map.get(sym, {})
        price, day_volume = snapshot_price_volume(snap)
        if price <= 0:
            snapshot_rejects["snapshot_missing_price"] = snapshot_rejects.get("snapshot_missing_price", 0) + 1
            continue
        if price < args.min_price or price > args.max_price:
            snapshot_rejects["snapshot_price_filter"] = snapshot_rejects.get("snapshot_price_filter", 0) + 1
            continue
        day_dollar = price * max(0.0, day_volume)
        # This is only a prefilter. The final 20-day liquidity check happens after daily bars.
        if day_volume > 0 and day_volume < args.prefilter_min_day_volume and day_dollar < args.prefilter_min_day_dollar_volume:
            snapshot_rejects["snapshot_liquidity_filter"] = snapshot_rejects.get("snapshot_liquidity_filter", 0) + 1
            continue
        prefiltered.append((sym, price, day_volume, day_dollar))

    prefiltered.sort(key=lambda x: (x[3], x[2], x[1]), reverse=True)
    if args.max_history_symbols and len(prefiltered) > args.max_history_symbols:
        prefiltered = prefiltered[: args.max_history_symbols]

    daily_symbols = [x[0] for x in prefiltered]
    print(f"Symbols selected for daily-history filter: {len(daily_symbols)}")

    bars_by_symbol, bar_errors = fetch_daily_bars(
        daily_symbols,
        feed=args.feed,
        lookback_days=args.lookback_days,
        chunk_size=args.daily_chunk_size,
        adjustment=args.adjustment,
    )

    rows: List[Dict[str, Any]] = []
    daily_rejects: Dict[str, int] = {}

    for idx, sym in enumerate(daily_symbols, start=1):
        if idx == 1 or idx % 100 == 0 or idx == len(daily_symbols):
            print(f"[{idx}/{len(daily_symbols)}] {sym}", flush=True)

        row, reason = evaluate_symbol(sym, assets_by_symbol.get(sym, {}), bars_by_symbol.get(sym, []), args)
        if row:
            rows.append(row)
        else:
            daily_rejects[reason] = daily_rejects.get(reason, 0) + 1

    rows.sort(key=lambda r: (safe_float(r.get("universe_score")), safe_float(r.get("avg_dollar_volume_20d"))), reverse=True)
    if args.max_symbols and len(rows) > args.max_symbols:
        rows = rows[: args.max_symbols]

    meta = {
        "version": VERSION,
        "generated_at_et": now_et().isoformat(timespec="seconds"),
        "mode": "alpaca_all_tradable_us_equity",
        "feed": args.feed,
        "trading_base_url": trading_base_url(),
        "assets_fetched": len(assets),
        "asset_filter_symbols": len(symbols),
        "snapshot_symbols": len(snapshot_map),
        "daily_history_symbols": len(daily_symbols),
        "final_symbols": len(rows),
        "filters": {
            "min_price": args.min_price,
            "max_price": args.max_price,
            "min_avg_volume": args.min_avg_volume,
            "min_avg_dollar_volume": args.min_avg_dollar_volume,
            "max_atr_pct": args.max_atr_pct,
            "rsi_range": [args.min_rsi, args.max_rsi],
            "min_history_days": args.min_history_days,
            "include_etfs": bool(args.include_etfs),
        },
        "rejects": {
            "asset": dict(sorted(asset_rejects.items(), key=lambda kv: kv[1], reverse=True)[:25]),
            "snapshot": dict(sorted(snapshot_rejects.items(), key=lambda kv: kv[1], reverse=True)[:25]),
            "daily": dict(sorted(daily_rejects.items(), key=lambda kv: kv[1], reverse=True)[:25]),
        },
        "errors": {
            "snapshot": dict(list(snapshot_errors.items())[:20]),
            "daily_bars": dict(list(bar_errors.items())[:20]),
        },
        "note": "S&P 500 is not used as the primary live universe. This file is built from Alpaca active/tradable US equities and then filtered for swing suitability.",
    }

    return rows, meta


def write_outputs(rows: Sequence[Dict[str, Any]], meta: Dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(list(rows))
    if df.empty:
        df = pd.DataFrame(columns=[
            "symbol", "name", "exchange", "latest_date_et", "price", "avg_volume_20d",
            "avg_dollar_volume_20d", "atr_pct", "rsi_14", "sma50", "sma200", "universe_score",
        ])
    df.to_csv(output, index=False)

    json_path = output.with_suffix(".json")
    json_path.write_text(json.dumps({"metadata": meta, "symbols": list(rows)}, indent=2), encoding="utf-8")

    print(f"Saved: {output}")
    print(f"Saved: {json_path}")
    print(f"Final symbols: {len(rows)}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build live all-universe Swing Desk universe from Alpaca tradable US equities")
    p.add_argument("--output", default=str(DEFAULT_OUTPUT))
    p.add_argument("--feed", default=os.getenv("ALPACA_DATA_FEED", os.getenv("ALPACA_FEED", "sip")).lower(), choices=["sip", "iex"])
    p.add_argument("--adjustment", default="raw", choices=["raw", "split", "dividend", "all"])
    p.add_argument("--include-etfs", action="store_true", help="Include ETF/ETN/fund-like assets. Default excludes them for stock swing scanning.")

    p.add_argument("--min-price", type=float, default=float(os.getenv("SWING_MIN_PRICE", "5")))
    p.add_argument("--max-price", type=float, default=float(os.getenv("SWING_MAX_PRICE", "200")))
    p.add_argument("--min-avg-volume", type=float, default=float(os.getenv("SWING_MIN_AVG_VOLUME", "500000")))
    p.add_argument("--min-avg-dollar-volume", type=float, default=float(os.getenv("SWING_MIN_AVG_DOLLAR_VOLUME", "20000000")))
    p.add_argument("--max-atr-pct", type=float, default=float(os.getenv("SWING_MAX_ATR_PCT", "8")))
    p.add_argument("--min-rsi", type=float, default=float(os.getenv("SWING_MIN_RSI", "35")))
    p.add_argument("--max-rsi", type=float, default=float(os.getenv("SWING_MAX_RSI", "75")))
    p.add_argument("--min-history-days", type=int, default=int(os.getenv("SWING_MIN_HISTORY_DAYS", "220")))

    p.add_argument("--prefilter-min-day-volume", type=float, default=float(os.getenv("SWING_PREFILTER_MIN_DAY_VOLUME", "200000")))
    p.add_argument("--prefilter-min-day-dollar-volume", type=float, default=float(os.getenv("SWING_PREFILTER_MIN_DAY_DOLLAR_VOLUME", "5000000")))
    p.add_argument("--lookback-days", type=int, default=int(os.getenv("SWING_UNIVERSE_LOOKBACK_DAYS", "430")))
    p.add_argument("--max-history-symbols", type=int, default=int(os.getenv("SWING_MAX_HISTORY_SYMBOLS", "1400")))
    p.add_argument("--max-symbols", type=int, default=int(os.getenv("SWING_LIVE_UNIVERSE_MAX_SYMBOLS", "900")))

    p.add_argument("--snapshot-chunk-size", type=int, default=int(os.getenv("SWING_SNAPSHOT_CHUNK_SIZE", "200")))
    p.add_argument("--daily-chunk-size", type=int, default=int(os.getenv("SWING_DAILY_CHUNK_SIZE", "25")))
    p.add_argument("--limit-assets", type=int, default=None, help="Testing only: limit symbols after asset filter.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    rows, meta = build_universe(args)
    write_outputs(rows, meta, Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
