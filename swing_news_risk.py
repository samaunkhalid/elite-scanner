#!/usr/bin/env python3
"""
swing_news_risk.py
------------------

Swing-specific news and earnings risk layer.

Purpose:
- Read Swing first-pass candidates.
- Pull real Alpaca company-specific headlines through alpaca_news.py.
- Optionally pull approximate earnings dates through yahooquery when available.
- Write:
    swing_results/swing_news_risk.json
    swing_results/swing_earnings_calendar.csv
- No fake proxy-news display. If data is unavailable, mark it unavailable/unknown.
- Does not touch day-trade production files and does not route orders.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

try:
    from alpaca_news import AlpacaNews
except Exception as exc:  # pragma: no cover
    AlpacaNews = None  # type: ignore
    _ALPACA_NEWS_IMPORT_ERROR = exc
else:
    _ALPACA_NEWS_IMPORT_ERROR = None


VERSION = "swing_news_risk_v1.0.1_alpaca_yahoo_earnings"

PROJECT_DIR = Path(os.getenv("ELITE_PROJECT_DIR", Path(__file__).resolve().parent)).resolve()

DEFAULT_CANDIDATE_FILE = PROJECT_DIR / "swing_results" / "swing_candidates_latest.csv"
DEFAULT_UNIVERSE_FILE = PROJECT_DIR / "swing_results" / "live_swing_universe.csv"
DEFAULT_OUTPUT_FILE = PROJECT_DIR / "swing_results" / "swing_news_risk.json"
DEFAULT_EARNINGS_FILE = PROJECT_DIR / "swing_results" / "swing_earnings_calendar.csv"


SEVERE_NEGATIVE_TERMS = [
    "offering",
    "registered direct",
    "atm offering",
    "shelf",
    "dilution",
    "warrant",
    "bankruptcy",
    "delisting",
    "reverse split",
    "going concern",
    "sec investigation",
    "fraud",
    "guidance cut",
    "cuts guidance",
]


def et_tz():
    if ZoneInfo:
        return ZoneInfo("America/New_York")
    return timezone.utc


def now_et() -> datetime:
    return datetime.now(et_tz())


def clean_symbol(value: Any) -> str:
    sym = str(value or "").strip().upper()
    if not sym or sym in {"SYMBOL", "TICKER"}:
        return ""
    if any(ch in sym for ch in ["^", "=", "/", " "]):
        return ""
    if "." in sym:
        return ""
    if len(sym) > 8:
        return ""
    if not re.match(r"^[A-Z][A-Z0-9]*$", sym):
        return ""
    return sym


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


def read_symbol_rows(path: Path, limit: int = 0) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    rows: List[Dict[str, Any]] = []
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            raw = data.get("symbols", data) if isinstance(data, dict) else data
            if isinstance(raw, dict):
                raw = [{"symbol": k, **(v if isinstance(v, dict) else {})} for k, v in raw.items()]
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        sym = clean_symbol(item.get("symbol"))
                        if sym:
                            item = dict(item)
                            item["symbol"] = sym
                            rows.append(item)
                    else:
                        sym = clean_symbol(item)
                        if sym:
                            rows.append({"symbol": sym})
        else:
            df = pd.read_csv(path)
            if df.empty:
                return []
            sym_col = None
            for col in ["symbol", "Symbol", "ticker", "Ticker"]:
                if col in df.columns:
                    sym_col = col
                    break
            if sym_col is None:
                sym_col = df.columns[0]
            if "score" in df.columns:
                df["_sort_score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0)
                df = df.sort_values("_sort_score", ascending=False)
            for _, row in df.iterrows():
                sym = clean_symbol(row.get(sym_col))
                if sym:
                    rec = {str(k): row.get(k) for k in df.columns if str(k) != "_sort_score"}
                    rec["symbol"] = sym
                    rows.append(rec)
    except Exception as exc:
        print(f"  ⚠️ Failed to read {path}: {exc}")
        return []

    # De-dupe preserving order.
    seen = set()
    out = []
    for row in rows:
        sym = clean_symbol(row.get("symbol"))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(row)
        if limit and len(out) >= limit:
            break
    return out


def load_company_names(paths: Sequence[Path]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for path in paths:
        for row in read_symbol_rows(path):
            sym = clean_symbol(row.get("symbol"))
            name = str(row.get("name") or row.get("company") or row.get("company_name") or "").strip()
            if sym and name and sym not in names:
                names[sym] = name
    return names


def risk_from_news_result(result: Dict[str, Any]) -> Dict[str, Any]:
    headline = str(result.get("catalyst_headline") or "").strip()
    sentiment = str(result.get("catalyst_sentiment") or "NONE").upper()
    label = str(result.get("catalyst_label") or "").strip()
    risk_flags = str(result.get("risk_flags") or "").strip()
    source = str(result.get("catalyst_source") or "").strip()
    created_at = str(result.get("catalyst_time") or "").strip()

    risk_text = " ".join([headline, label, risk_flags]).lower()
    severe = any(term in risk_text for term in SEVERE_NEGATIVE_TERMS)

    if not headline:
        risk = "UNKNOWN"
        summary = "News unavailable"
        positive = False
        negative = False
        data_status = "unavailable"
    elif severe:
        risk = "SEVERE_NEGATIVE"
        summary = headline
        positive = False
        negative = True
        data_status = "real_headline"
    elif sentiment == "NEGATIVE":
        risk = "NEGATIVE"
        summary = headline
        positive = False
        negative = True
        data_status = "real_headline"
    elif sentiment == "POSITIVE":
        risk = "POSITIVE"
        summary = headline
        positive = True
        negative = False
        data_status = "real_headline"
    else:
        risk = "NEUTRAL"
        summary = headline
        positive = False
        negative = False
        data_status = "real_headline"

    return {
        "risk": risk,
        "news_risk": risk,
        "headline": headline,
        "summary": summary,
        "source": source,
        "created_at": created_at,
        "label": label,
        "risk_flags": risk_flags,
        "positive_catalyst": positive,
        "negative_catalyst": negative,
        "data_status": data_status,
    }


def parse_earnings_date(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (list, tuple, set)):
        vals = [parse_earnings_date(v) for v in value]
        vals = [v for v in vals if v]
        return min(vals) if vals else ""
    if isinstance(value, dict):
        for key in ["earningsDate", "earnings_date", "date", "startdatetime", "start", "raw"]:
            if key in value:
                parsed = parse_earnings_date(value.get(key))
                if parsed:
                    return parsed
        return ""
    try:
        dt = pd.to_datetime(value, errors="coerce", utc=False)
        if pd.isna(dt):
            return ""
        return str(dt.date())
    except Exception:
        return ""


def fetch_yahooquery_earnings(symbols: Sequence[str], enabled: bool = True) -> Tuple[Dict[str, str], str]:
    if not enabled:
        return {}, "disabled"

    try:
        from yahooquery import Ticker  # noqa: WPS433
    except Exception as exc:
        return {}, f"unavailable: {exc}"

    out: Dict[str, str] = {}
    try:
        # Chunk to reduce single-call failure blast radius.
        for i in range(0, len(symbols), 50):
            chunk = list(symbols[i : i + 50])
            if not chunk:
                continue
            tq = Ticker(" ".join(chunk), asynchronous=True)
            events = getattr(tq, "calendar_events", {}) or {}
            if not isinstance(events, dict):
                continue
            for sym in chunk:
                rec = events.get(sym, {}) if isinstance(events, dict) else {}
                if not isinstance(rec, dict):
                    continue
                earnings = rec.get("earnings") or rec.get("earningsDate") or rec.get("earnings_date")
                dt = parse_earnings_date(earnings)
                if dt:
                    out[sym] = dt
    except Exception as exc:
        return out, f"partial_error: {exc}"

    return out, "ok"


def earnings_risk_from_date(date_s: str) -> str:
    if not date_s:
        return "UNKNOWN"
    try:
        today = now_et().date()
        ed = pd.to_datetime(date_s).date()
        delta = (ed - today).days
        if 0 <= delta <= 5:
            return "UPCOMING_5D"
        if -2 <= delta < 0:
            return "RECENT_POST_EARNINGS"
        return "CLEAR"
    except Exception:
        return "UNKNOWN"


def build_news_payload(args: argparse.Namespace) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    candidate_rows = read_symbol_rows(Path(args.candidate_file), limit=args.limit_symbols)
    if not candidate_rows:
        candidate_rows = read_symbol_rows(Path(args.symbols_file), limit=args.limit_symbols)

    symbols = [clean_symbol(r.get("symbol")) for r in candidate_rows]
    symbols = [s for s in symbols if s]
    symbols = list(dict.fromkeys(symbols))

    company_names = load_company_names([Path(args.candidate_file), Path(args.symbols_file), Path(args.universe_file)])

    print("=== SWING NEWS / EARNINGS RISK ===")
    print(f"Version: {VERSION}")
    print(f"Symbols: {len(symbols)}")
    print(f"News lookback hours: {args.lookback_hours}")

    news_errors: List[str] = []
    news_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    if AlpacaNews is None:
        news_errors.append(f"alpaca_news import failed: {_ALPACA_NEWS_IMPORT_ERROR}")
    else:
        try:
            client = AlpacaNews()
            news_by_symbol = client.fetch_news(symbols, lookback_hours=args.lookback_hours)
        except Exception as exc:
            news_errors.append(f"Alpaca news fetch failed: {exc}")

    earnings_map, earnings_status = fetch_yahooquery_earnings(symbols, enabled=not args.no_yahoo_earnings)

    out_symbols: Dict[str, Dict[str, Any]] = {}
    earnings_rows: List[Dict[str, Any]] = []

    for sym in symbols:
        if AlpacaNews is None:
            news_rec = {
                "risk": "UNKNOWN",
                "news_risk": "UNKNOWN",
                "headline": "",
                "summary": "News unavailable",
                "source": "",
                "created_at": "",
                "label": "",
                "risk_flags": "",
                "positive_catalyst": False,
                "negative_catalyst": False,
                "data_status": "unavailable",
            }
        else:
            company_name = company_names.get(sym, "")
            try:
                analyzed = AlpacaNews().analyze_symbol_news(sym, company_name, news_by_symbol.get(sym, []))
                news_rec = risk_from_news_result(analyzed)
            except Exception as exc:
                news_rec = {
                    "risk": "UNKNOWN",
                    "news_risk": "UNKNOWN",
                    "headline": "",
                    "summary": "News unavailable",
                    "source": "",
                    "created_at": "",
                    "label": "",
                    "risk_flags": "",
                    "positive_catalyst": False,
                    "negative_catalyst": False,
                    "data_status": "unavailable",
                    "error": repr(exc),
                }

        earnings_date = earnings_map.get(sym, "")
        earnings_risk = earnings_risk_from_date(earnings_date)

        rec = {
            "symbol": sym,
            **news_rec,
            "earnings_date": earnings_date,
            "earnings_risk": earnings_risk,
        }
        out_symbols[sym] = rec

        if earnings_date:
            earnings_rows.append({
                "symbol": sym,
                "earnings_date": earnings_date,
                "earnings_risk": earnings_risk,
                "source": "yahooquery_calendar_events",
            })

    payload = {
        "metadata": {
            "version": VERSION,
            "generated_at_et": now_et().isoformat(timespec="seconds"),
            "candidate_file": str(args.candidate_file),
            "symbols_file": str(args.symbols_file),
            "universe_file": str(args.universe_file),
            "symbols_requested": len(symbols),
            "symbols_with_real_headline": sum(1 for r in out_symbols.values() if r.get("data_status") == "real_headline"),
            "earnings_records": len(earnings_rows),
            "earnings_status": earnings_status,
            "news_errors": news_errors[:20],
            "note": "No proxy news is generated. Missing data is marked unavailable/unknown.",
        },
        "symbols": out_symbols,
    }

    return payload, earnings_rows


def write_outputs(payload: Dict[str, Any], earnings_rows: Sequence[Dict[str, Any]], output_file: Path, earnings_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    earnings_file.parent.mkdir(parents=True, exist_ok=True)

    output_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    df = pd.DataFrame(list(earnings_rows))
    if df.empty:
        df = pd.DataFrame(columns=["symbol", "earnings_date", "earnings_risk", "source"])
    df.to_csv(earnings_file, index=False)

    print(f"Saved: {output_file}")
    print(f"Saved: {earnings_file}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build Swing news and earnings risk files")
    p.add_argument("--candidate-file", default=str(DEFAULT_CANDIDATE_FILE))
    p.add_argument("--symbols-file", default=str(DEFAULT_CANDIDATE_FILE), help="Fallback symbols file if candidate file is empty.")
    p.add_argument("--universe-file", default=str(DEFAULT_UNIVERSE_FILE))
    p.add_argument("--output-file", default=str(DEFAULT_OUTPUT_FILE))
    p.add_argument("--earnings-file", default=str(DEFAULT_EARNINGS_FILE))
    p.add_argument("--lookback-hours", type=int, default=int(os.getenv("SWING_NEWS_LOOKBACK_HOURS", "72")))
    p.add_argument("--limit-symbols", type=int, default=int(os.getenv("SWING_NEWS_SYMBOL_LIMIT", "120")))
    p.add_argument("--no-yahoo-earnings", action="store_true", help="Disable yahooquery earnings date lookup.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    payload, earnings_rows = build_news_payload(args)
    write_outputs(payload, earnings_rows, Path(args.output_file), Path(args.earnings_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
