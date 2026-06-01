#!/usr/bin/env python3
"""
swing_smart_money_scores.py
---------------------------

Swing-specific wrapper around the existing day-trade Smart Money bars proxy.

Purpose:
- Reuse the existing smart_money_bars_proxy.py scoring engine.
- Read Swing first-pass candidates instead of day-trade watchlists.
- Write swing_smart_money_scores.json / swing_smart_money_scores_history.csv.
- Never overwrite day-trade smart_money_scores.json.
- Does not route orders.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional, Sequence


VERSION = "swing_smart_money_scores_v1.0.1_day_engine_wrapper"

PROJECT_DIR = Path(os.getenv("ELITE_PROJECT_DIR", Path(__file__).resolve().parent)).resolve()
DEFAULT_CANDIDATE_FILE = PROJECT_DIR / "swing_results" / "swing_candidates_latest.csv"
DEFAULT_OUTPUT_FILE = PROJECT_DIR / "swing_results" / "swing_smart_money_scores.json"
DEFAULT_HISTORY_FILE = PROJECT_DIR / "swing_results" / "swing_smart_money_scores_history.csv"


def write_empty_payload(output_file: Path, reason: str) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": {
            "version": VERSION,
            "phase": "SWING_SMART_MONEY_WRAPPER",
            "source": "smart_money_bars_proxy.py",
            "symbols_scored": 0,
            "errors": [reason],
            "note": "Swing wrapper did not overwrite day-trade smart_money_scores.json.",
        },
        "symbols": {},
    }
    output_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run Swing-specific smart-money score wrapper")
    p.add_argument("--candidate-file", default=str(DEFAULT_CANDIDATE_FILE))
    p.add_argument("--output-file", default=str(DEFAULT_OUTPUT_FILE))
    p.add_argument("--history-file", default=str(DEFAULT_HISTORY_FILE))
    p.add_argument("--symbol-limit", type=int, default=int(os.getenv("SWING_SMART_MONEY_SYMBOL_LIMIT", "120")))
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    candidate_file = Path(args.candidate_file)
    output_file = Path(args.output_file)
    history_file = Path(args.history_file)

    print("=== SWING SMART MONEY WRAPPER ===")
    print(f"Version: {VERSION}")
    print(f"Candidate file: {candidate_file}")
    print(f"Output file: {output_file}")

    if not candidate_file.exists():
        msg = f"Swing candidate file not found: {candidate_file}"
        print(f"  ⚠️ {msg}")
        write_empty_payload(output_file, msg)
        return 0

    output_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.parent.mkdir(parents=True, exist_ok=True)

    # Set env BEFORE importing smart_money_bars_proxy because that script binds
    # file paths and limits at import time.
    os.environ["ELITE_PROJECT_DIR"] = str(PROJECT_DIR)
    os.environ["SMART_MONEY_RAW_FILE"] = str(candidate_file)
    os.environ["SMART_MONEY_POTENTIAL_FILE"] = str(PROJECT_DIR / "swing_results" / "_no_day_potential_for_swing.csv")
    os.environ["SMART_MONEY_ACTIVE_FILE"] = str(PROJECT_DIR / "swing_results" / "_no_day_active_for_swing.csv")
    os.environ["SMART_MONEY_OUTPUT_FILE"] = str(output_file)
    os.environ["SMART_MONEY_HISTORY_FILE"] = str(history_file)
    os.environ["SMART_MONEY_SYMBOL_LIMIT"] = str(max(1, int(args.symbol_limit)))
    os.environ.setdefault("SMART_MONEY_REGULAR_SESSION_ONLY", "1")

    try:
        import smart_money_bars_proxy  # noqa: WPS433
    except Exception as exc:
        msg = f"Could not import smart_money_bars_proxy.py: {exc}"
        print(f"  ⚠️ {msg}")
        write_empty_payload(output_file, msg)
        return 0

    rc = int(smart_money_bars_proxy.run() or 0)

    # Add a Swing wrapper marker without changing the scoring content.
    try:
        payload = json.loads(output_file.read_text(encoding="utf-8"))
        meta = payload.setdefault("metadata", {})
        meta["swing_wrapper_version"] = VERSION
        meta["swing_candidate_file"] = str(candidate_file)
        meta["day_trade_output_overwritten"] = False
        output_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"  ⚠️ Could not annotate swing smart-money payload: {exc}")

    print("Done.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
