#!/usr/bin/env python3
"""
ML Phase 0 Data Collector — Elite Scanner

Purpose:
  Collect clean shadow-training data only.
  This script DOES NOT create signals, modify signals, trade, or change rules.

What it reads, when present:
  - signal_desk.json
  - suppressed_signals.csv
  - rejected_signals.csv
  - potential_movers.csv
  - elite_watchlist_raw.csv
  - active_momentum.csv
  - signal_outcomes.csv
  - scanner_meta.json
  - market_regime.json

What it writes:
  - ml_phase0_data/ml_phase0_snapshots.jsonl
  - ml_phase0_data/ml_phase0_snapshots_latest.csv
  - ml_phase0_data/ml_phase0_summary.json

Recommended use:
  Run after each signal refresh or dashboard refresh.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


COLLECTOR_VERSION = "ml_phase0_collector_v1.1.0_gap_fields"
DEFAULT_OUTPUT_DIR = "ml_phase0_data"

SIGNAL_DESK_FILE = "signal_desk.json"
SUPPRESSED_SIGNALS_FILE = "suppressed_signals.csv"
REJECTED_SIGNALS_FILE = "rejected_signals.csv"
POTENTIAL_MOVERS_FILE = "potential_movers.csv"
RAW_WATCHLIST_FILE = "elite_watchlist_raw.csv"
ACTIVE_MOMENTUM_FILE = "active_momentum.csv"
SIGNAL_OUTCOMES_FILE = "signal_outcomes.csv"
SCANNER_META_FILE = "scanner_meta.json"
MARKET_REGIME_FILE = "market_regime.json"
SIGNAL_ENGINE_FILE = "signal_engine.py"


CORE_CSV_COLUMNS = [
    "run_id",
    "snapshot_time_et",
    "source_file",
    "source_group",
    "symbol",
    "candidate_status",
    "strategy_family",
    "setup_type",
    "entry",
    "stop_loss",
    "target_1",
    "target_2",
    "reward_risk",
    "confidence",
    "price",
    "change_pct",
    "volume",
    "relative_volume",
    "vwap",
    "vwap_dist_pct",
    "above_vwap",
    "hod_distance_pct",
    "gap_pct",
    "gap_age_minutes",
    "gap_direction",
    "strong_gap_up",
    "previous_close",
    "session_open",
    "premarket_high",
    "opening_range_high",
    "opening_range_low",
    "opening_range_minutes",
    "opening_range_source",
    "macd_1m_histogram",
    "macd_1m_histogram_prev",
    "macd_1m_histogram_prev2",
    "macd_histogram",
    "macd_histogram_prev",
    "macd_histogram_prev2",
    "macd_1m_curling_up",
    "macd_1m_curling_down",
    "macd_5m_improving",
    "macd_5m_bearish",
    "block_reason",
    "reason_category",
    "target_source",
    "signal_engine_strategy_version",
]


RECLAIMER_KEYWORDS = [
    "RECLAIM",
    "VWAP_EMA_RECLAIM_RUNNER",
    "VWAP_RECLAIM_BREAKOUT",
    "RECLAIM_PULLBACK_HOLDING",
]

PULLBACK_KEYWORDS = [
    "VWAP_PULLBACK_CONTINUATION",
    "PULLBACK_CONTINUATION",
    "VWAP PULLBACK",
]

HOD_BASE_KEYWORDS = [
    "HOD_BASE_BREAKOUT",
    "BASE_SQUEEZE_BREAKOUT",
    "HOD",
    "SQUEEZE",
    "BASE BREAKOUT",
]


def now_et() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo("America/New_York"))
    return datetime.now(timezone.utc)


def iso_et(dt: Optional[datetime] = None) -> str:
    return (dt or now_et()).isoformat(timespec="seconds")


def safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value)
    if text.lower() in {"nan", "none", "nat"}:
        return default
    return text


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and value.strip() in {"", "—", "nan", "None", "none"}:
            return default
        return float(value)
    except Exception:
        return default


def safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = safe_str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "above", "pass", "passed"}


def normalize_symbol(value: Any) -> str:
    return re.sub(r"[^A-Z0-9.\-]+", "", safe_str(value).upper().strip())


def first_present(row: Dict[str, Any], keys: Iterable[str], default: Any = "") -> Any:
    for key in keys:
        if key in row:
            val = row.get(key)
            if val not in (None, "", "—"):
                return val
    return default


def read_json_object(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_csv_rows(path: Path, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append({safe_str(k): v for k, v in row.items()})
                if limit is not None and len(rows) >= limit:
                    break
    except Exception:
        return []
    return rows


def file_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def extract_signal_engine_version(project_dir: Path) -> str:
    path = project_dir / SIGNAL_ENGINE_FILE
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    m = re.search(r'SIGNAL_ENGINE_STRATEGY_VERSION\s*=\s*["\']([^"\']+)["\']', text)
    return m.group(1) if m else ""


def extract_signal_desk_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []

    rows: List[Dict[str, Any]] = []
    for key in ["signals", "active", "ready", "watch", "candidates", "rejected_candidates"]:
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    copied = dict(item)
                    copied["_signal_desk_bucket"] = key
                    rows.append(copied)
    return rows


def row_setup_text(row: Dict[str, Any]) -> str:
    parts = []
    for key in [
        "setup_type",
        "setup_label",
        "intraday_setup_type",
        "setup_bucket",
        "pattern",
        "strategy_family",
        "tags",
        "reason",
        "block_reason",
        "invalidation_reason",
    ]:
        val = safe_str(row.get(key), "")
        if val:
            parts.append(val)
    return " | ".join(parts).upper()


def classify_family(row: Dict[str, Any]) -> str:
    existing = safe_str(row.get("strategy_family") or row.get("family"), "").upper().strip()
    if existing in {
        "RECLAIMER",
        "VWAP_PULLBACK_CONTINUATION",
        "HOD_BASE_SQUEEZE",
        "GENERIC_ACTIVE_MOMENTUM",
        "OTHER",
    }:
        return existing

    text = row_setup_text(row)

    if any(k in text for k in RECLAIMER_KEYWORDS):
        return "RECLAIMER"

    if any(k in text for k in PULLBACK_KEYWORDS):
        return "VWAP_PULLBACK_CONTINUATION"

    if any(k in text for k in HOD_BASE_KEYWORDS):
        return "HOD_BASE_SQUEEZE"

    if "ACTIVE_MOMENTUM" in text or "MOMENTUM" in text:
        return "GENERIC_ACTIVE_MOMENTUM"

    return "OTHER"


def categorize_reason(text: str) -> str:
    t = safe_str(text, "").lower()
    if not t:
        return ""
    if any(x in t for x in ["macd", "momentum", "histogram", "curl", "crossover"]):
        return "MACD/momentum"
    if any(x in t for x in ["vwap", "support", "reclaim", "breakdown", "lost level", "level holding"]):
        return "VWAP/support"
    if any(x in t for x in ["volume", "rvol", "liquidity", "thin"]):
        return "volume"
    if any(x in t for x in ["stale", "data", "bar missing", "missing bars", "quote"]):
        return "stale/data"
    if any(x in t for x in ["extended", "chase", "far from", "extension"]):
        return "extension/chase"
    if any(x in t for x in ["risk reward", "r/r", "target", "resistance"]):
        return "risk_reward/target"
    if any(x in t for x in ["stop", "risk", "invalidated"]):
        return "stop/risk"
    if any(x in t for x in ["news", "event", "earnings", "offering", "halt"]):
        return "event/news"
    return "uncategorized"


def infer_status(row: Dict[str, Any], source_group: str) -> str:
    status = safe_str(
        first_present(
            row,
            ["signal_status", "status", "decision", "state", "actionability"],
            "",
        )
    ).upper().replace(" ", "_")

    if status:
        return status

    if source_group == "suppressed":
        return "SUPPRESSED"
    if source_group == "rejected":
        return "REJECTED"
    return "SCANNER_ONLY"


def build_snapshot_record(
    row: Dict[str, Any],
    *,
    run_id: str,
    snapshot_time: str,
    source_file: str,
    source_group: str,
    scanner_meta: Dict[str, Any],
    market_regime: Dict[str, Any],
    signal_engine_strategy_version: str,
) -> Dict[str, Any]:
    symbol = normalize_symbol(first_present(row, ["symbol", "ticker"], ""))

    reason = safe_str(
        first_present(
            row,
            [
                "block_reason",
                "reason",
                "invalidation_reason",
                "suppression_reason",
                "reject_reason",
                "active_block_reason",
                "setup_reason",
                "trigger_reason",
            ],
            "",
        )
    )

    setup_type = safe_str(
        first_present(
            row,
            ["setup_type", "setup_label", "intraday_setup_type", "pattern"],
            "",
        )
    )

    entry = first_present(row, ["entry", "entry_trigger", "trigger_price", "planned_entry"], "")
    stop_loss = first_present(row, ["stop_loss", "stop", "planned_stop"], "")
    target_1 = first_present(row, ["target_1", "target1", "t1", "planned_target_1"], "")
    target_2 = first_present(row, ["target_2", "target2", "t2", "planned_target_2"], "")

    status = infer_status(row, source_group)
    family = classify_family(row)

    price = first_present(row, ["price", "current_price", "intraday_last_price", "latest_price"], "")
    vwap = first_present(row, ["vwap", "session_vwap"], "")

    macd_1m_hist = first_present(row, ["macd_1m_histogram", "macd_1m_hist", "macd_hist_1m"], "")
    macd_1m_hist_prev = first_present(row, ["macd_1m_histogram_prev", "macd_1m_hist_prev"], "")
    macd_1m_hist_prev2 = first_present(row, ["macd_1m_histogram_prev2", "macd_1m_hist_prev2"], "")
    macd_5m_hist = first_present(row, ["macd_histogram", "macd_5m_histogram", "macd_hist_5m"], "")
    macd_5m_hist_prev = first_present(row, ["macd_histogram_prev", "macd_5m_histogram_prev"], "")
    macd_5m_hist_prev2 = first_present(row, ["macd_histogram_prev2", "macd_5m_histogram_prev2"], "")

    # Derive basic MACD flags for ML features; these are observations only, not signal rules.
    m1 = safe_float(macd_1m_hist, 0.0)
    m1p = safe_float(macd_1m_hist_prev, 0.0)
    m1p2 = safe_float(macd_1m_hist_prev2, 0.0)
    m5 = safe_float(macd_5m_hist, 0.0)
    m5p = safe_float(macd_5m_hist_prev, 0.0)
    m5p2 = safe_float(macd_5m_hist_prev2, 0.0)

    macd_1m_curling_up = bool(m1 > m1p or (m1 > m1p and m1p >= m1p2))
    macd_1m_curling_down = bool(m1 < m1p and (m1p2 == 0.0 or m1p <= m1p2))
    macd_5m_improving = bool(m5 > m5p or (m5 > m5p and m5p >= m5p2))
    macd_5m_bearish = bool(m5 < m5p and m5p < m5p2) if m5p2 != 0.0 else bool(m5 < m5p)

    record = {
        "collector_version": COLLECTOR_VERSION,
        "run_id": run_id,
        "snapshot_time_et": snapshot_time,
        "source_file": source_file,
        "source_group": source_group,
        "symbol": symbol,
        "candidate_status": status,
        "strategy_family": family,
        "setup_type": setup_type,
        "entry": safe_float(entry, 0.0),
        "stop_loss": safe_float(stop_loss, 0.0),
        "target_1": safe_float(target_1, 0.0),
        "target_2": safe_float(target_2, 0.0),
        "reward_risk": safe_float(first_present(row, ["reward_risk", "rr", "risk_reward"], 0.0), 0.0),
        "confidence": safe_float(first_present(row, ["confidence", "confidence_score"], 0.0), 0.0),
        "price": safe_float(price, 0.0),
        "change_pct": safe_float(first_present(row, ["change_pct", "pct_change"], 0.0), 0.0),
        "volume": safe_float(first_present(row, ["volume", "intraday_volume", "latest_volume"], 0.0), 0.0),
        "relative_volume": safe_float(first_present(row, ["relative_volume", "rvol", "rel_volume"], 0.0), 0.0),
        "vwap": safe_float(vwap, 0.0),
        "vwap_dist_pct": safe_float(first_present(row, ["vwap_dist_pct", "vwap_distance_pct"], 0.0), 0.0),
        "above_vwap": safe_bool(first_present(row, ["above_vwap", "is_above_vwap"], False)),
        "hod_distance_pct": safe_float(first_present(row, ["hod_distance_pct", "from_hod_pct"], 0.0), 0.0),
        "gap_pct": safe_float(first_present(row, ["gap_pct", "gap_percent"], 0.0), 0.0),
        "gap_age_minutes": safe_float(first_present(row, ["gap_age_minutes", "gap_age_min"], 0.0), 0.0),
        "gap_direction": safe_str(first_present(row, ["gap_direction"], "")),
        "strong_gap_up": safe_bool(first_present(row, ["strong_gap_up", "is_strong_gap_up"], False)),
        "previous_close": safe_float(first_present(row, ["previous_close", "prev_close"], 0.0), 0.0),
        "session_open": safe_float(first_present(row, ["session_open", "regular_open", "open"], 0.0), 0.0),
        "premarket_high": safe_float(first_present(row, ["premarket_high", "pre_market_high"], 0.0), 0.0),
        "opening_range_high": safe_float(first_present(row, ["opening_range_high", "or_high"], 0.0), 0.0),
        "opening_range_low": safe_float(first_present(row, ["opening_range_low", "or_low"], 0.0), 0.0),
        "opening_range_minutes": safe_float(first_present(row, ["opening_range_minutes", "or_minutes"], 0.0), 0.0),
        "opening_range_source": safe_str(first_present(row, ["opening_range_source", "or_source"], "")),
        "macd_1m_histogram": m1,
        "macd_1m_histogram_prev": m1p,
        "macd_1m_histogram_prev2": m1p2,
        "macd_histogram": m5,
        "macd_histogram_prev": m5p,
        "macd_histogram_prev2": m5p2,
        "macd_1m_curling_up": macd_1m_curling_up,
        "macd_1m_curling_down": macd_1m_curling_down,
        "macd_5m_improving": macd_5m_improving,
        "macd_5m_bearish": macd_5m_bearish,
        "block_reason": reason,
        "reason_category": categorize_reason(reason),
        "target_source": safe_str(first_present(row, ["target_source", "target_method"], "")),
        "raw_row": row,
        "scanner_meta": scanner_meta,
        "market_regime": market_regime,
        "signal_engine_strategy_version": signal_engine_strategy_version,
    }

    # Stable observation key for dedup/trace.
    identity = "|".join(
        [
            record["snapshot_time_et"][:16],
            record["source_file"],
            record["source_group"],
            record["symbol"],
            record["candidate_status"],
            record["strategy_family"],
            record["setup_type"],
            safe_str(record["entry"]),
            safe_str(record["target_1"]),
        ]
    )
    record["observation_id"] = hashlib.sha1(identity.encode("utf-8")).hexdigest()

    return record


@contextmanager
def simple_lock(lock_dir: Path, timeout_seconds: int = 10):
    start = time.time()
    while True:
        try:
            lock_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            if time.time() - start > timeout_seconds:
                raise RuntimeError(f"Could not acquire lock: {lock_dir}")
            time.sleep(0.25)
    try:
        yield
    finally:
        try:
            lock_dir.rmdir()
        except Exception:
            pass


def write_jsonl_append(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_latest_csv(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CORE_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({col: record.get(col, "") for col in CORE_CSV_COLUMNS})


def write_summary(path: Path, summary: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)


def collect_records(project_dir: Path, limit_per_file: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    run_id = str(uuid.uuid4())
    snapshot_time = iso_et()

    scanner_meta = read_json_object(project_dir / SCANNER_META_FILE, {})
    market_regime = read_json_object(project_dir / MARKET_REGIME_FILE, {})
    signal_engine_strategy_version = extract_signal_engine_version(project_dir)
    signal_engine_hash = file_sha256(project_dir / SIGNAL_ENGINE_FILE)

    sources: List[Tuple[str, str, List[Dict[str, Any]]]] = []

    signal_payload = read_json_object(project_dir / SIGNAL_DESK_FILE, {})
    signal_rows = extract_signal_desk_rows(signal_payload)
    sources.append((SIGNAL_DESK_FILE, "signal_desk", signal_rows))

    sources.append((SUPPRESSED_SIGNALS_FILE, "suppressed", read_csv_rows(project_dir / SUPPRESSED_SIGNALS_FILE, limit_per_file)))
    sources.append((REJECTED_SIGNALS_FILE, "rejected", read_csv_rows(project_dir / REJECTED_SIGNALS_FILE, limit_per_file)))
    sources.append((POTENTIAL_MOVERS_FILE, "scanner_potential", read_csv_rows(project_dir / POTENTIAL_MOVERS_FILE, limit_per_file)))
    sources.append((RAW_WATCHLIST_FILE, "scanner_raw", read_csv_rows(project_dir / RAW_WATCHLIST_FILE, limit_per_file)))
    sources.append((ACTIVE_MOMENTUM_FILE, "scanner_active_momentum_shadow", read_csv_rows(project_dir / ACTIVE_MOMENTUM_FILE, limit_per_file)))

    records: List[Dict[str, Any]] = []
    seen_ids = set()

    for source_file, source_group, rows in sources:
        for row in rows:
            if not isinstance(row, dict):
                continue
            record = build_snapshot_record(
                row,
                run_id=run_id,
                snapshot_time=snapshot_time,
                source_file=source_file,
                source_group=source_group,
                scanner_meta=scanner_meta if isinstance(scanner_meta, dict) else {},
                market_regime=market_regime if isinstance(market_regime, dict) else {},
                signal_engine_strategy_version=signal_engine_strategy_version,
            )
            if not record["symbol"]:
                continue
            # Avoid exact duplicate observations in the same run.
            if record["observation_id"] in seen_ids:
                continue
            seen_ids.add(record["observation_id"])
            records.append(record)

    summary = build_summary(
        records,
        run_id=run_id,
        snapshot_time=snapshot_time,
        project_dir=project_dir,
        signal_engine_strategy_version=signal_engine_strategy_version,
        signal_engine_hash=signal_engine_hash,
    )
    return records, summary


def build_summary(
    records: List[Dict[str, Any]],
    *,
    run_id: str,
    snapshot_time: str,
    project_dir: Path,
    signal_engine_strategy_version: str,
    signal_engine_hash: str,
) -> Dict[str, Any]:
    status_counts = Counter(r.get("candidate_status", "") for r in records)
    family_counts = Counter(r.get("strategy_family", "") for r in records)
    source_counts = Counter(r.get("source_group", "") for r in records)
    reason_counts = Counter(r.get("reason_category", "") for r in records if r.get("reason_category"))

    active_records = [r for r in records if r.get("candidate_status") in {"ACTIVE", "ACTIVE_SIGNAL"}]
    macd_block_records = [
        r for r in records
        if r.get("reason_category") == "MACD/momentum"
        or "macd" in safe_str(r.get("block_reason", "")).lower()
    ]

    return {
        "collector_version": COLLECTOR_VERSION,
        "run_id": run_id,
        "snapshot_time_et": snapshot_time,
        "project_dir": str(project_dir),
        "signal_engine_strategy_version": signal_engine_strategy_version,
        "signal_engine_sha256": signal_engine_hash,
        "total_records": len(records),
        "active_records": len(active_records),
        "macd_block_records": len(macd_block_records),
        "status_counts": dict(status_counts),
        "family_counts": dict(family_counts),
        "source_counts": dict(source_counts),
        "reason_counts": dict(reason_counts),
        "note": "ML Phase 0 only: data collection/shadow logging. No signals modified.",
    }


def print_summary(summary: Dict[str, Any], output_dir: Path, dry_run: bool) -> None:
    print("=== ML PHASE 0 DATA COLLECTION ===")
    print(f"Collector: {summary.get('collector_version')}")
    print(f"Run ID: {summary.get('run_id')}")
    print(f"Time ET: {summary.get('snapshot_time_et')}")
    print(f"Signal Engine: {summary.get('signal_engine_strategy_version')}")
    print(f"Records: {summary.get('total_records')}")
    print(f"Active records: {summary.get('active_records')}")
    print(f"MACD block records: {summary.get('macd_block_records')}")
    print("")
    print("Status counts:")
    for key, val in sorted(summary.get("status_counts", {}).items()):
        print(f"  {key or 'UNKNOWN'}: {val}")
    print("")
    print("Family counts:")
    for key, val in sorted(summary.get("family_counts", {}).items()):
        print(f"  {key or 'UNKNOWN'}: {val}")
    print("")
    print("Reason counts:")
    for key, val in sorted(summary.get("reason_counts", {}).items()):
        print(f"  {key}: {val}")

    if dry_run:
        print("")
        print("DRY RUN: no files written.")
    else:
        print("")
        print(f"Saved JSONL: {output_dir / 'ml_phase0_snapshots.jsonl'}")
        print(f"Saved latest CSV: {output_dir / 'ml_phase0_snapshots_latest.csv'}")
        print(f"Saved summary: {output_dir / 'ml_phase0_summary.json'}")
    print("")
    print("NO SIGNALS MODIFIED.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ML Phase 0 data collector for Elite Scanner.")
    parser.add_argument("--project-dir", default=".", help="Project directory. Default: current directory.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Output directory. Default: ml_phase0_data")
    parser.add_argument("--limit-per-file", type=int, default=None, help="Optional max rows per CSV source.")
    parser.add_argument("--dry-run", action="store_true", help="Collect and print summary without writing files.")
    args = parser.parse_args(argv)

    project_dir = Path(args.project_dir).expanduser().resolve()
    output_dir = (project_dir / args.output_dir).resolve() if not Path(args.output_dir).is_absolute() else Path(args.output_dir).resolve()

    if not project_dir.exists():
        print(f"ERROR: project directory not found: {project_dir}", file=sys.stderr)
        return 2

    try:
        records, summary = collect_records(project_dir, limit_per_file=args.limit_per_file)

        if not args.dry_run:
            lock_dir = output_dir / ".collector_lock"
            with simple_lock(lock_dir):
                write_jsonl_append(output_dir / "ml_phase0_snapshots.jsonl", records)
                write_latest_csv(output_dir / "ml_phase0_snapshots_latest.csv", records)
                write_summary(output_dir / "ml_phase0_summary.json", summary)

        print_summary(summary, output_dir, args.dry_run)
        return 0

    except Exception as exc:
        print(f"ERROR: ML Phase 0 collection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
