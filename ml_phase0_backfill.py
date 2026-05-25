#!/usr/bin/env python3
"""
ML Phase 0.1 Historical Backfill Collector
==========================================

Purpose:
  Convert existing historical outcome/log files into a separate ML Phase 0
  historical dataset.

Safety:
  - READS existing CSV/JSON/JSONL files only.
  - WRITES only inside ml_phase0_data/.
  - Does NOT modify signal generation.
  - Does NOT modify dashboard.
  - Does NOT modify signal_engine.py outputs.
  - Does NOT make trading decisions.

Main source:
  - signal_outcomes.csv

Optional sources if present:
  - signal_desk_history.jsonl
  - signal_desk_history.csv
  - rejected_signals.csv
  - suppressed_signals.csv

Output:
  - ml_phase0_data/ml_phase0_historical_backfill.jsonl
  - ml_phase0_data/ml_phase0_historical_backfill_latest.csv
  - ml_phase0_data/ml_phase0_historical_backfill_summary.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


COLLECTOR_VERSION = "ml_phase0_backfill_v1.0.0"
DEFAULT_ENGINE_VERSION = "historical_pre_v2_8_or_unknown"
ET_OFFSET = timezone(timedelta(hours=-4))


# -----------------------------
# Safe helpers
# -----------------------------

def now_et_iso() -> str:
    return datetime.now(ET_OFFSET).replace(microsecond=0).isoformat()


def safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "nat", "null"}:
        return default
    return text


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = safe_str(value, "")
        if not text:
            return default
        text = text.replace("$", "").replace(",", "").replace("%", "")
        return float(text)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(safe_str(value, "0")))
    except Exception:
        return default


def first_value(row: Dict[str, Any], keys: Iterable[str], default: Any = "") -> Any:
    for key in keys:
        if key in row:
            value = row.get(key)
            if safe_str(value, ""):
                return value
    return default


def parse_datetime_loose(value: Any) -> Optional[datetime]:
    text = safe_str(value, "")
    if not text:
        return None

    cleaned = (
        text.replace("Z", "+00:00")
        .replace(" ET", "")
        .replace(" EDT", "")
        .replace(" EST", "")
        .strip()
    )

    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET_OFFSET)
        return dt
    except Exception:
        pass

    patterns = [
        r"(\d{4})-(\d{2})-(\d{2})[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?",
        r"(\d{1,2})/(\d{1,2})/(\d{4})[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?",
    ]

    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        try:
            if pat.startswith(r"(\d{4})"):
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                hh, mm = int(m.group(4)), int(m.group(5))
                ss = int(m.group(6) or 0)
            else:
                mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                hh, mm = int(m.group(4)), int(m.group(5))
                ss = int(m.group(6) or 0)
            return datetime(y, mo, d, hh, mm, ss, tzinfo=ET_OFFSET)
        except Exception:
            return None

    return None


def sha256_file(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


# -----------------------------
# Classification helpers
# -----------------------------

RECLAIM_KEYWORDS = (
    "RECLAIM",
    "VWAP_EMA_RECLAIM_RUNNER",
    "VWAP_RECLAIM_BREAKOUT",
    "RECLAIM_PULLBACK_HOLDING",
)

PULLBACK_KEYWORDS = (
    "VWAP_PULLBACK_CONTINUATION",
    "PULLBACK_CONTINUATION",
    "VWAP PULLBACK",
)

HOD_KEYWORDS = (
    "HOD_BASE_BREAKOUT",
    "BASE_SQUEEZE_BREAKOUT",
    "HOD",
    "SQUEEZE",
    "BASE BREAKOUT",
)

GENERIC_ACTIVE_KEYWORDS = (
    "ACTIVE_MOMENTUM",
    "GENERIC_ACTIVE_MOMENTUM",
    "MOMENTUM",
)


def classify_family(row: Dict[str, Any]) -> str:
    combined = " ".join(
        safe_str(first_value(row, keys, ""))
        for keys in [
            ["strategy_family", "family"],
            ["setup_type", "setup", "setup_name"],
            ["intraday_setup_type"],
            ["bucket", "setup_bucket"],
            ["tags"],
            ["reason", "invalidation_reason", "block_reason"],
        ]
    ).upper()

    if any(k in combined for k in RECLAIM_KEYWORDS):
        return "RECLAIMER"
    if any(k in combined for k in PULLBACK_KEYWORDS):
        return "VWAP_PULLBACK_CONTINUATION"
    if any(k in combined for k in HOD_KEYWORDS):
        return "HOD_BASE_SQUEEZE"
    if any(k in combined for k in GENERIC_ACTIVE_KEYWORDS):
        return "GENERIC_ACTIVE_MOMENTUM"
    return "OTHER"


def normalize_status(row: Dict[str, Any], source_name: str) -> str:
    raw = safe_str(first_value(row, [
        "status",
        "signal_status",
        "state",
        "outcome_status",
        "result_status",
    ], ""))

    if raw:
        return raw.upper().replace(" ", "_")

    if source_name in {"rejected_signals.csv", "suppressed_signals.csv"}:
        return "SUPPRESSED"

    outcome = normalize_outcome(row)
    if outcome in {"T1_HIT", "T2_HIT", "STOP_HIT", "INVALIDATED"}:
        return "FINALIZED"

    return "HISTORICAL"


def normalize_outcome(row: Dict[str, Any]) -> str:
    combined = " ".join(
        safe_str(first_value(row, keys, ""))
        for keys in [
            ["outcome", "result", "final_outcome"],
            ["status", "signal_status", "outcome_status"],
            ["hit_status", "exit_reason"],
            ["invalidation_reason", "reason"],
        ]
    ).upper()

    if "T2" in combined and ("HIT" in combined or "TARGET" in combined):
        return "T2_HIT"
    if "T1" in combined and ("HIT" in combined or "TARGET" in combined):
        return "T1_HIT"
    if "STOP" in combined or "SL_HIT" in combined:
        return "STOP_HIT"
    if "INVALID" in combined or "VOID" in combined:
        return "INVALIDATED"
    if "ACTIVE" in combined:
        return "ACTIVE_OBSERVED"
    if "SUPPRESS" in combined or "REJECT" in combined or "BLOCK" in combined:
        return "BLOCKED_OR_SUPPRESSED"
    return "UNKNOWN"


def categorize_reason(row: Dict[str, Any]) -> str:
    text = " ".join(
        safe_str(first_value(row, keys, ""))
        for keys in [
            ["reason", "block_reason", "rejection_reason"],
            ["invalidation_reason"],
            ["notes", "note"],
            ["tags"],
            ["setup_type"],
        ]
    ).lower()

    if not text:
        return "uncategorized"

    if any(w in text for w in ["macd", "momentum", "histogram", "curl", "bearish crossover"]):
        return "macd_momentum"
    if any(w in text for w in ["vwap", "support", "ema", "reclaim failed", "breakdown", "lost"]):
        return "vwap_support"
    if any(w in text for w in ["volume", "rvol", "liquidity", "thin"]):
        return "volume"
    if any(w in text for w in ["extended", "chase", "too far", "extension"]):
        return "extension_chase"
    if any(w in text for w in ["stale", "data", "bar", "missing", "feed", "quote"]):
        return "stale_data"
    if any(w in text for w in ["target", "resistance", "reward", "risk", "rr", "r/r"]):
        return "target_rr"
    if any(w in text for w in ["news", "event", "earnings", "macro", "halt"]):
        return "event_news"
    if any(w in text for w in ["stop", "risk", "support buffer"]):
        return "stop_risk"

    return "uncategorized"


def infer_engine_version(row: Dict[str, Any]) -> str:
    value = safe_str(first_value(row, [
        "signal_engine_strategy_version",
        "engine_version",
        "strategy_version",
        "version",
    ], ""))
    return value or DEFAULT_ENGINE_VERSION


# -----------------------------
# File readers
# -----------------------------

def read_csv_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            return [dict(row) for row in reader]
    except Exception as exc:
        print(f"WARNING: failed to read {path}: {exc}")
        return []


def read_jsonl_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    rows = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except Exception as exc:
        print(f"WARNING: failed to read {path}: {exc}")

    return rows


def row_timestamp(row: Dict[str, Any]) -> Optional[datetime]:
    for key in [
        "timestamp",
        "time",
        "created_at",
        "detected_at",
        "signal_time",
        "entry_time",
        "exit_time",
        "generated_at_et",
        "snapshot_time_et",
        "date",
    ]:
        if key in row:
            dt = parse_datetime_loose(row.get(key))
            if dt:
                return dt
    return None


def should_include_by_days(row: Dict[str, Any], days: int, now_dt: datetime) -> bool:
    if days <= 0:
        return True
    dt = row_timestamp(row)
    if not dt:
        return True
    return dt >= now_dt - timedelta(days=days)


# -----------------------------
# Normalization
# -----------------------------

def normalize_record(row: Dict[str, Any], source_path: Path, source_row_index: int, backfill_id: str) -> Dict[str, Any]:
    symbol = safe_str(first_value(row, ["symbol", "ticker"], "")).upper()
    setup_type = safe_str(first_value(row, [
        "setup_type",
        "setup",
        "setup_name",
        "intraday_setup_type",
    ], ""))

    entry = safe_float(first_value(row, ["entry", "entry_trigger", "trigger", "entry_price"], 0), 0)
    stop = safe_float(first_value(row, ["stop", "stop_loss", "sl", "stop_price"], 0), 0)
    target_1 = safe_float(first_value(row, ["target_1", "target1", "t1", "target"], 0), 0)
    target_2 = safe_float(first_value(row, ["target_2", "target2", "t2"], 0), 0)

    risk = max(entry - stop, 0.0) if entry > 0 and stop > 0 else 0.0
    t1_r = ((target_1 - entry) / risk) if risk > 0 and target_1 > 0 else 0.0
    t2_r = ((target_2 - entry) / risk) if risk > 0 and target_2 > 0 else 0.0

    best_r = safe_float(first_value(row, ["best_r", "max_r", "max_favorable_r", "mfe_r"], 0), 0)
    final_r = safe_float(first_value(row, ["final_r", "realized_r", "exit_r"], 0), 0)

    dt = row_timestamp(row)

    normalized = {
        "record_type": "historical_backfill",
        "collector_version": COLLECTOR_VERSION,
        "backfill_id": backfill_id,
        "backfill_time_et": now_et_iso(),
        "source_file": source_path.name,
        "source_path": str(source_path),
        "source_row_index": source_row_index,
        "source_file_sha256": sha256_file(source_path),
        "signal_engine_strategy_version": infer_engine_version(row),
        "symbol": symbol,
        "strategy_family": classify_family(row),
        "setup_type": setup_type,
        "signal_status": normalize_status(row, source_path.name),
        "outcome": normalize_outcome(row),
        "reason_category": categorize_reason(row),
        "reason_text": safe_str(first_value(row, [
            "reason",
            "block_reason",
            "rejection_reason",
            "invalidation_reason",
            "notes",
            "note",
        ], "")),
        "timestamp_et": dt.isoformat() if dt else "",
        "entry": entry,
        "stop": stop,
        "target_1": target_1,
        "target_2": target_2,
        "risk_per_share": round(risk, 6),
        "target_1_r": round(t1_r, 4),
        "target_2_r": round(t2_r, 4),
        "best_r": best_r,
        "final_r": final_r,
        "price": safe_float(first_value(row, ["price", "current_price", "last_price"], 0), 0),
        "change_pct": safe_float(first_value(row, ["change_pct", "pct_change"], 0), 0),
        "vwap_dist_pct": safe_float(first_value(row, ["vwap_dist_pct", "vwap_distance_pct"], 0), 0),
        "volume": safe_float(first_value(row, ["volume", "intraday_volume"], 0), 0),
        "rvol": safe_float(first_value(row, ["rvol", "relative_volume"], 0), 0),
        "confidence": safe_float(first_value(row, ["confidence", "confidence_score"], 0), 0),
        "raw_json": json.dumps(row, ensure_ascii=False, default=str),
    }

    return normalized


# -----------------------------
# Output
# -----------------------------

def write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def write_csv(path: Path, records: List[Dict[str, Any]]) -> None:
    if not records:
        path.write_text("", encoding="utf-8")
        return

    # Keep raw_json last.
    keys = sorted({k for rec in records for k in rec.keys() if k != "raw_json"})
    keys.append("raw_json")

    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def build_summary(records: List[Dict[str, Any]], source_counts: Dict[str, int], backfill_id: str) -> Dict[str, Any]:
    status_counts = Counter(rec.get("signal_status", "") for rec in records)
    outcome_counts = Counter(rec.get("outcome", "") for rec in records)
    family_counts = Counter(rec.get("strategy_family", "") for rec in records)
    reason_counts = Counter(rec.get("reason_category", "") for rec in records)
    version_counts = Counter(rec.get("signal_engine_strategy_version", "") for rec in records)

    family_outcomes: Dict[str, Counter] = defaultdict(Counter)
    for rec in records:
        family_outcomes[rec.get("strategy_family", "OTHER")][rec.get("outcome", "UNKNOWN")] += 1

    return {
        "collector_version": COLLECTOR_VERSION,
        "backfill_id": backfill_id,
        "backfill_time_et": now_et_iso(),
        "total_records": len(records),
        "source_counts": dict(source_counts),
        "status_counts": dict(status_counts),
        "outcome_counts": dict(outcome_counts),
        "family_counts": dict(family_counts),
        "reason_counts": dict(reason_counts),
        "engine_version_counts": dict(version_counts),
        "family_outcome_counts": {fam: dict(cnt) for fam, cnt in family_outcomes.items()},
        "note": (
            "Historical backfill only. Old/broken-rule data is kept separate and "
            "version-labeled. No signals modified."
        ),
    }


# -----------------------------
# Main
# -----------------------------

def collect_backfill(project_dir: Path, days: int) -> Tuple[List[Dict[str, Any]], Dict[str, int], str]:
    backfill_id = str(uuid.uuid4())
    now_dt = datetime.now(ET_OFFSET)

    # Prefer stable historical outcome file first.
    sources = [
        ("signal_outcomes.csv", "csv"),
        ("signal_desk_history.csv", "csv"),
        ("signal_desk_history.jsonl", "jsonl"),
        ("rejected_signals.csv", "csv"),
        ("suppressed_signals.csv", "csv"),
    ]

    all_records: List[Dict[str, Any]] = []
    source_counts: Dict[str, int] = {}

    seen_keys = set()

    for filename, fmt in sources:
        path = project_dir / filename
        if fmt == "csv":
            rows = read_csv_rows(path)
        else:
            rows = read_jsonl_rows(path)

        if not rows:
            continue

        used = 0
        for idx, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            if not should_include_by_days(row, days, now_dt):
                continue

            rec = normalize_record(row, path, idx, backfill_id)

            # Deduplicate best-effort across repeated files.
            dedupe_key = (
                rec.get("source_file"),
                rec.get("symbol"),
                rec.get("timestamp_et"),
                rec.get("setup_type"),
                rec.get("outcome"),
                rec.get("reason_text"),
            )
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            all_records.append(rec)
            used += 1

        source_counts[filename] = used

    return all_records, source_counts, backfill_id


def main() -> int:
    parser = argparse.ArgumentParser(description="ML Phase 0.1 historical backfill collector")
    parser.add_argument("--project-dir", default=".", help="Project directory. Default: current directory")
    parser.add_argument("--out-dir", default="ml_phase0_data", help="Output directory. Default: ml_phase0_data")
    parser.add_argument("--days", type=int, default=0, help="Backfill last N days only. Default 0 = all history")
    parser.add_argument("--dry-run", action="store_true", help="Analyze only; do not write files")
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    out_dir = (project_dir / args.out_dir).resolve()

    records, source_counts, backfill_id = collect_backfill(project_dir, args.days)
    summary = build_summary(records, source_counts, backfill_id)

    print("=== ML PHASE 0.1 HISTORICAL BACKFILL ===")
    print(f"Collector: {COLLECTOR_VERSION}")
    print(f"Backfill ID: {backfill_id}")
    print(f"Project: {project_dir}")
    print(f"Days: {'ALL' if args.days <= 0 else args.days}")
    print(f"Records: {len(records)}")
    print("")
    print("Source counts:")
    for name, count in source_counts.items():
        print(f"  {name}: {count}")
    print("")
    print("Family counts:")
    for name, count in summary["family_counts"].items():
        print(f"  {name}: {count}")
    print("")
    print("Outcome counts:")
    for name, count in summary["outcome_counts"].items():
        print(f"  {name}: {count}")
    print("")
    print("Reason counts:")
    for name, count in summary["reason_counts"].items():
        print(f"  {name}: {count}")

    if args.dry_run:
        print("")
        print("DRY RUN: no files written.")
        print("NO SIGNALS MODIFIED.")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = out_dir / "ml_phase0_historical_backfill.jsonl"
    csv_path = out_dir / "ml_phase0_historical_backfill_latest.csv"
    summary_path = out_dir / "ml_phase0_historical_backfill_summary.json"

    write_jsonl(jsonl_path, records)
    write_csv(csv_path, records)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print("")
    print(f"Saved JSONL: {jsonl_path}")
    print(f"Saved latest CSV: {csv_path}")
    print(f"Saved summary: {summary_path}")
    print("")
    print("NO SIGNALS MODIFIED.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
