#!/usr/bin/env python3
"""
Elite Scanner VPS Runner

Purpose:
- Run separate pre-market monitor-only mover snapshots.
- Run regular full market scanner at scheduled ET times.
- Run Signal Desk refresh every 60 seconds during the regular monitoring window.
- Run separate after-hours monitor-only mover snapshots.
- Run dashboard-only session status refresh at 04:00, 09:30, and 20:01 ET.
- Publish dashboard files to the Nginx web directory.
- Run an isolated live all-universe Swing Desk scan on demand and at scheduled Swing times with streaming child progress.

Locked behavior:
- PREMARKET_SCAN uses extended_hours_movers.py and never runs signal_engine.py.
- AFTER_HOURS_SCAN uses extended_hours_movers.py and never runs signal_engine.py.
- PREMARKET_SCAN / AFTER_HOURS_SCAN do not run elite_scanner.py.
- FULL_SCANNER runs elite_scanner.py + sector_rotation.py + smart_money_bars_proxy.py + signal_engine.py + elite_dashboard.py.
- SIGNAL_REFRESH runs smart_money_bars_proxy.py + signal_engine.py + elite_dashboard.py.
- DASHBOARD_ONLY runs only elite_dashboard.py.
- SWING_SCAN runs only Swing Desk scripts + elite_dashboard.py and never runs signal_engine.py.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import shutil
import signal
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


# =========================
# Configuration
# =========================

PROJECT_DIR = Path(os.getenv("ELITE_PROJECT_DIR", Path(__file__).resolve().parent)).resolve()
PYTHON_BIN = os.getenv("ELITE_PYTHON_BIN", sys.executable)

WEB_DIR = Path(os.getenv("ELITE_WEB_DIR", "/var/www/elite-scanner")).resolve()
LOG_DIR = Path(os.getenv("ELITE_LOG_DIR", PROJECT_DIR / "logs")).resolve()
RUNNER_LOG = LOG_DIR / "elite_runner.log"

ET_TZ_NAME = "America/New_York"

MACRO_SCRIPT = "macro_calendar.py"
SCANNER_SCRIPT = "elite_scanner.py"
SECTOR_ROTATION_SCRIPT = "sector_rotation.py"
EXTENDED_MOVERS_SCRIPT = "extended_hours_movers.py"
SMART_MONEY_SCRIPT = "smart_money_bars_proxy.py"
SIGNAL_ENGINE_SCRIPT = "signal_engine.py"
DASHBOARD_SCRIPT = "elite_dashboard.py"

# Swing Desk scripts are isolated from the day-trade Signal Desk.
SWING_UNIVERSE_SCRIPT = "swing_live_universe_builder.py"
SWING_BAR_UPDATER_SCRIPT = "swing_live_bar_updater.py"
SWING_SCANNER_SCRIPT = "swing_scanner.py"
SWING_SMART_MONEY_SCRIPT = "swing_smart_money_scores.py"
SWING_NEWS_RISK_SCRIPT = "swing_news_risk.py"

SWING_RESULTS_DIR = PROJECT_DIR / "swing_results"
SWING_LIVE_UNIVERSE_FILE = SWING_RESULTS_DIR / "live_swing_universe.csv"
SWING_CANDIDATE_FILE = SWING_RESULTS_DIR / "swing_candidates_latest.csv"
SWING_SMART_MONEY_FILE = SWING_RESULTS_DIR / "swing_smart_money_scores.json"
SWING_NEWS_RISK_FILE = SWING_RESULTS_DIR / "swing_news_risk.json"
SWING_EARNINGS_FILE = SWING_RESULTS_DIR / "swing_earnings_calendar.csv"

# Swing protected watch retention:
# - Keeps previously valid SWING_WATCH / SWING_READY symbols in the next live universe.
# - Prevents valid swing setups from disappearing only because the universe builder
#   re-ranked/excluded them on the next run.
SWING_PROTECTED_WATCH_FILE = SWING_RESULTS_DIR / "swing_protected_watchlist.csv"
SWING_PREVIOUS_CANDIDATES_FILE = SWING_RESULTS_DIR / "swing_candidates_previous_before_scan.csv"
SWING_PROTECTED_STATUS_VALUES = {"SWING_WATCH", "SWING_READY"}


SWING_DAILY_ROOT = Path(os.getenv("SWING_LIVE_DAILY_ROOT", "/opt/strategy-discovery/data/live_swing_daily"))
SWING_HOURLY_ROOT = Path(os.getenv("SWING_LIVE_HOURLY_ROOT", "/opt/strategy-discovery/data/live_swing_1h"))
SWING_M15_ROOT = Path(os.getenv("SWING_LIVE_M15_ROOT", "/opt/strategy-discovery/data/live_swing_15m"))
SWING_M5_ROOT = Path(os.getenv("SWING_LIVE_M5_ROOT", "/opt/strategy-discovery/data/live_swing_5m"))

# Pre-market monitor-only snapshots.
PREMARKET_SCAN_TIMES_ET = {
    "07:00",
    "07:30",
    "08:00",
    "08:30",
    "08:45",
    "09:00",
    "09:15",
}

# Regular whole-market scanner schedule.
#
# Morning discovery update:
# - Full scanner starts at 09:35 ET as an early discovery scan after the first
#   5-minute opening candle closes.
# - 09:35 is discovery-oriented; Signal Desk / signal_engine still decides
#   whether a setup is actionable and blocks weak/noisy entries.
# - Then it runs every 5 minutes from 09:40 through 11:00 ET.
# - This improves early VWAP/EMA reclaim discovery without scanning the raw
#   09:30 opening noise.
# - Signal Desk still enforces its market-phase rules; opening entries remain blocked
#   until the signal engine allows valid regular-market execution.
# - After 11:00, the schedule relaxes back to the normal midday/afternoon cadence.
SCANNER_TIMES_ET = {
    "09:35",
    "09:40",
    "09:45",
    "09:50",
    "09:55",
    "10:00",
    "10:05",
    "10:10",
    "10:15",
    "10:20",
    "10:25",
    "10:30",
    "10:35",
    "10:40",
    "10:45",
    "10:50",
    "10:55",
    "11:00",
    "11:15",
    "11:30",
    "13:30",
    "14:00",
    "14:30",
    "14:45",
    "15:00",
    "15:15",
    "15:30",
}

# After-hours monitor-only snapshots.
#
# Stale-snapshot protection:
# - 16:02 gives the market data feed a short settlement buffer after the 16:00 close.
# - 16:05 and 16:10 reduce the old 16:00-16:15 stale-display window.
# - Dashboard still has a stale-file guard, so old after-hours movers are hidden until
#   a same-day 16:00+ after-hours snapshot exists.
AFTER_HOURS_SCAN_TIMES_ET = {
    "16:02",
    "16:05",
    "16:10",
    "16:15",
    "16:30",
    "17:00",
    "17:30",
    "18:00",
    "18:30",
    "19:00",
    "19:30",
}

# Signal Desk refresh:
SIGNAL_REFRESH_START_ET = "09:46"
SIGNAL_REFRESH_END_ET = "16:05"
SIGNAL_REFRESH_INTERVAL_SECONDS = 60

# Dashboard-only session boundary refreshes.
DASHBOARD_ONLY_TIMES_ET = {
    "04:00",
    "09:30",
    "20:01",
}

# Full Swing Desk scan schedule.
#
# Important:
# - This is the heavy Swing pipeline: live universe -> Daily/1H/15m/5m caches
#   -> first scanner -> swing smart-money/news -> final scanner -> dashboard.
# - It is intentionally aligned with major regular scanner checkpoints, not
#   every 5-minute day-trade scan, so Signal Desk refresh is not blocked all day.
# - The lightweight Swing live-price overlay is handled by elite_dashboard.py,
#   so visible Swing card prices still update whenever the dashboard refreshes.
# - Override without code changes:
#     ELITE_SWING_SCAN_TIMES_ET="09:35,10:30,11:30,13:30,14:30,15:30,15:55"
# - Disable auto Swing without removing --once-swing:
#     ELITE_SWING_SCAN_ENABLED=0
DEFAULT_SWING_SCAN_TIMES_ET = {
    "09:35",
    "09:45",
    "10:00",
    "10:15",
    "10:30",
    "10:45",
    "11:00",
    "11:15",
    "11:30",
    "12:00",
    "12:30",
    "13:00",
    "13:30",
    "13:45",
    "14:00",
    "14:15",
    "14:30",
    "14:45",
    "15:00",
    "15:15",
    "15:30",
    "15:45",
    "15:55",
}

def parse_schedule_env(name: str, default: set[str]) -> set[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return set(default)

    out = set()
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        if re.match(r"^\d{2}:\d{2}$", value):
            out.add(value)
    return out or set(default)

SWING_SCAN_TIMES_ET = parse_schedule_env("ELITE_SWING_SCAN_TIMES_ET", DEFAULT_SWING_SCAN_TIMES_ET)
SWING_SCAN_ENABLED = os.getenv("ELITE_SWING_SCAN_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}

# Files to publish to Nginx after dashboard rebuilds.
# dashboard.html is handled first and forced to both index.html and dashboard.html.
PUBLISH_PATTERNS = [
    "*.json",
    "*.csv",
    "*.log",
]


# =========================
# Logging / Time Helpers
# =========================

def get_et_tz():
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo is unavailable. Use Python 3.9+.")
    return ZoneInfo(ET_TZ_NAME)


def now_et() -> datetime:
    return datetime.now(get_et_tz())


def hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def ymd(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def parse_hhmm(value: str) -> dtime:
    hour, minute = value.split(":")
    return dtime(hour=int(hour), minute=int(minute))


def in_time_window(current: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    current_t = current.time().replace(second=0, microsecond=0)
    return parse_hhmm(start_hhmm) <= current_t <= parse_hhmm(end_hhmm)


def is_weekday(current: Optional[datetime] = None) -> bool:
    current = current or now_et()
    return current.weekday() < 5


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s ET | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(RUNNER_LOG, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)


# =========================
# Command Execution
# =========================

@dataclass
class RunResult:
    name: str
    ok: bool
    returncode: int
    duration_seconds: float


def script_exists(script_name: str) -> bool:
    return (PROJECT_DIR / script_name).exists()


def run_python_script(
    script_name: str,
    args: Optional[Sequence[str]] = None,
    timeout_seconds: int = int(os.getenv("ELITE_SCRIPT_TIMEOUT_SECONDS", "5400")),
    required: bool = True,
) -> RunResult:
    script_path = PROJECT_DIR / script_name
    start = time.monotonic()

    if not script_path.exists():
        if required:
            logging.error("Missing required script: %s", script_path)
            return RunResult(script_name, False, 127, 0.0)

        logging.warning("Skipping optional script because it is missing: %s", script_path)
        return RunResult(script_name, True, 0, 0.0)

    cmd = [PYTHON_BIN, str(script_path)] + list(args or [])
    logging.info("Running: %s", " ".join(cmd))

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc: Optional[subprocess.Popen[str]] = None

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            bufsize=1,
        )

        assert proc.stdout is not None

        while True:
            line = proc.stdout.readline()

            if line:
                logging.info("[%s] %s", script_name, line.rstrip())

            if line == "" and proc.poll() is not None:
                break

            if timeout_seconds and (time.monotonic() - start) > timeout_seconds:
                proc.kill()
                duration = time.monotonic() - start
                logging.error("TIMEOUT %s after %.1fs", script_name, duration)
                return RunResult(script_name, False, 124, duration)

        returncode = proc.wait()
        duration = time.monotonic() - start

        if returncode == 0:
            logging.info("Completed %s in %.1fs", script_name, duration)
            return RunResult(script_name, True, 0, duration)

        logging.error(
            "FAILED %s rc=%s duration=%.1fs",
            script_name,
            returncode,
            duration,
        )
        return RunResult(script_name, False, returncode, duration)

    except KeyboardInterrupt:
        if proc is not None and proc.poll() is None:
            proc.terminate()
        raise

    except Exception as exc:
        duration = time.monotonic() - start

        if proc is not None and proc.poll() is None:
            proc.kill()

        logging.exception("ERROR running %s after %.1fs: %s", script_name, duration, exc)
        return RunResult(script_name, False, 1, duration)


JobSpec = Tuple[str, bool, Sequence[str]]


def run_script_sequence(
    name: str,
    scripts: Sequence[JobSpec],
    publish_after: bool = True,
) -> bool:
    """
    Run a mixed critical/non-critical sequence.

    scripts:
      (script_name, required, args)
    """
    logging.info("===== START %s =====", name)

    all_ok = True

    for script, required, args in scripts:
        result = run_python_script(script, args=args, required=required)

        if not result.ok:
            all_ok = False

            if required:
                logging.error("Stopping %s because required script failed: %s", name, script)
                break

            logging.warning("Continuing %s despite optional script failure: %s", name, script)

    if publish_after:
        publish_dashboard()

    logging.info("===== END %s | ok=%s =====", name, all_ok)
    return all_ok



# =========================
# Swing Protected Watch Helpers
# =========================

def normalize_symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    symbol = symbol.replace("/", ".")
    if not symbol:
        return ""
    if not re.match(r"^[A-Z][A-Z0-9.\-]{0,14}$", symbol):
        return ""
    return symbol


def read_symbols_from_csv(path: Path, status_filter: Optional[set[str]] = None) -> set[str]:
    """
    Read symbols from a CSV file. If status_filter is provided, only keep rows
    where swing_status/status is in the allowed set.

    Supports:
    - symbol column
    - ticker column
    - first column fallback
    """
    symbols: set[str] = set()

    if not path.exists() or not path.is_file():
        return symbols

    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            sample = f.read(4096)
            f.seek(0)

            has_header = "symbol" in sample.lower() or "ticker" in sample.lower()
            if not has_header:
                reader = csv.reader(f)
                for row in reader:
                    if not row:
                        continue
                    symbol = normalize_symbol(row[0])
                    if symbol and symbol != "SYMBOL":
                        symbols.add(symbol)
                return symbols

            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return symbols

            field_map = {str(c).strip().lower(): c for c in reader.fieldnames}
            symbol_col = field_map.get("symbol") or field_map.get("ticker") or reader.fieldnames[0]
            status_col = field_map.get("swing_status") or field_map.get("status")

            for row in reader:
                if status_filter is not None and status_col:
                    status = str(row.get(status_col, "") or "").strip().upper()
                    if status not in status_filter:
                        continue

                symbol = normalize_symbol(row.get(symbol_col))
                if symbol and symbol != "SYMBOL":
                    symbols.add(symbol)

    except Exception as exc:
        logging.warning("Could not read symbols from %s: %s", path, exc)

    return symbols


def snapshot_existing_swing_candidates() -> None:
    """
    Preserve the previous production Swing candidates before the new scan rewrites
    swing_candidates_latest.csv.
    """
    try:
        if SWING_CANDIDATE_FILE.exists() and SWING_CANDIDATE_FILE.is_file():
            SWING_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SWING_CANDIDATE_FILE, SWING_PREVIOUS_CANDIDATES_FILE)
            logging.info("Saved previous Swing candidates snapshot: %s", SWING_PREVIOUS_CANDIDATES_FILE)
        else:
            logging.info("No previous Swing candidates file to snapshot")
    except Exception as exc:
        logging.warning("Failed to snapshot previous Swing candidates: %s", exc)


def protected_swing_symbols() -> set[str]:
    """
    Protected symbols include:
    - prior production SWING_WATCH / SWING_READY from the snapshot
    - manual symbols in swing_results/swing_protected_watchlist.csv

    Manual file can be one symbol per line or a CSV with symbol/ticker column.
    """
    symbols: set[str] = set()

    symbols.update(
        read_symbols_from_csv(
            SWING_PREVIOUS_CANDIDATES_FILE,
            status_filter=SWING_PROTECTED_STATUS_VALUES,
        )
    )
    symbols.update(read_symbols_from_csv(SWING_PROTECTED_WATCH_FILE, status_filter=None))

    return symbols


def merge_protected_swing_symbols_into_universe() -> int:
    """
    Merge protected Swing symbols into live_swing_universe.csv after the universe
    builder runs and before the bar updater/scanner runs.

    This does not force them to appear on the dashboard. It only ensures they
    are re-evaluated by the full Swing pipeline. If they fail the scanner, they
    still drop normally.
    """
    protected = protected_swing_symbols()

    if not protected:
        logging.info("Swing protected watch merge: no protected symbols")
        return 0

    if not SWING_LIVE_UNIVERSE_FILE.exists():
        logging.warning(
            "Swing protected watch merge skipped; universe file missing: %s",
            SWING_LIVE_UNIVERSE_FILE,
        )
        return 0

    try:
        with SWING_LIVE_UNIVERSE_FILE.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)

        if not fieldnames:
            fieldnames = ["symbol"]
            rows = []

        field_map = {str(c).strip().lower(): c for c in fieldnames}
        symbol_col = field_map.get("symbol") or field_map.get("ticker") or fieldnames[0]

        existing = {normalize_symbol(row.get(symbol_col)) for row in rows}
        existing.discard("")

        added = 0
        added_symbols: list[str] = []

        for symbol in sorted(protected):
            if symbol in existing:
                continue

            row = {field: "" for field in fieldnames}
            row[symbol_col] = symbol
            rows.append(row)
            existing.add(symbol)
            added += 1
            added_symbols.append(symbol)

        if added:
            backup_path = SWING_RESULTS_DIR / "live_swing_universe_before_protected_merge.csv"
            shutil.copy2(SWING_LIVE_UNIVERSE_FILE, backup_path)

            with SWING_LIVE_UNIVERSE_FILE.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            logging.info(
                "Swing protected watch merge: added %s symbols to universe: %s",
                added,
                ", ".join(added_symbols),
            )
        else:
            logging.info(
                "Swing protected watch merge: all protected symbols already in universe (%s checked)",
                len(protected),
            )

        return added

    except Exception as exc:
        logging.exception("Swing protected watch merge failed: %s", exc)
        return 0



# =========================
# Job Types
# =========================

def run_premarket_scan() -> bool:
    """
    Pre-market monitor-only scan.

    Runs:
    - macro_calendar.py
    - sector_rotation.py if present
    - extended_hours_movers.py --session premarket
    - elite_dashboard.py

    Does NOT run:
    - elite_scanner.py
    - smart_money_bars_proxy.py
    - signal_engine.py
    """
    return run_script_sequence(
        "PREMARKET_SCAN",
        [
            (MACRO_SCRIPT, True, ()),
            (SECTOR_ROTATION_SCRIPT, False, ()),
            (EXTENDED_MOVERS_SCRIPT, True, ("--session", "premarket")),
            (DASHBOARD_SCRIPT, True, ()),
        ],
        publish_after=True,
    )


def run_after_hours_scan() -> bool:
    """
    After-hours monitor-only scan.

    Runs:
    - macro_calendar.py
    - sector_rotation.py if present
    - extended_hours_movers.py --session afterhours
    - elite_dashboard.py

    Does NOT run:
    - elite_scanner.py
    - smart_money_bars_proxy.py
    - signal_engine.py
    """
    return run_script_sequence(
        "AFTER_HOURS_SCAN",
        [
            (MACRO_SCRIPT, True, ()),
            (SECTOR_ROTATION_SCRIPT, False, ()),
            (EXTENDED_MOVERS_SCRIPT, True, ("--session", "afterhours")),
            (DASHBOARD_SCRIPT, True, ()),
        ],
        publish_after=True,
    )


def run_full_scanner() -> bool:
    """
    Regular market whole-universe discovery scan.

    Runs:
    - macro_calendar.py
    - elite_scanner.py
    - sector_rotation.py if present
    - smart_money_bars_proxy.py
    - signal_engine.py
    - elite_dashboard.py
    """
    return run_script_sequence(
        "FULL_SCANNER",
        [
            (MACRO_SCRIPT, True, ()),
            (SCANNER_SCRIPT, True, ()),
            (SECTOR_ROTATION_SCRIPT, False, ()),
            (SMART_MONEY_SCRIPT, False, ()),
            (SIGNAL_ENGINE_SCRIPT, True, ()),
            (DASHBOARD_SCRIPT, True, ()),
        ],
        publish_after=True,
    )


def run_signal_refresh() -> bool:
    """
    Refresh already-picked scanner tickers only.

    Runs:
    - smart_money_bars_proxy.py
    - signal_engine.py
    - elite_dashboard.py

    Does NOT run:
    - elite_scanner.py
    - sector_rotation.py
    - extended_hours_movers.py
    """
    return run_script_sequence(
        "SIGNAL_REFRESH",
        [
            (SMART_MONEY_SCRIPT, False, ()),
            (SIGNAL_ENGINE_SCRIPT, True, ()),
            (DASHBOARD_SCRIPT, True, ()),
        ],
        publish_after=True,
    )


def run_dashboard_only_refresh(reason: str = "SESSION_STATUS") -> bool:
    """
    Dashboard-only refresh.

    Strict rule:
    - NO elite_scanner.py
    - NO sector_rotation.py
    - NO smart_money_bars_proxy.py
    - NO extended_hours_movers.py
    - NO signal_engine.py
    - NO Signal Desk promotion/change
    - ONLY elite_dashboard.py rebuild + publish
    """
    return run_script_sequence(
        f"DASHBOARD_ONLY_{reason}",
        [
            (DASHBOARD_SCRIPT, True, ()),
        ],
        publish_after=True,
    )



def run_swing_scan() -> bool:
    """
    Isolated live all-universe Swing Desk scan.

    This does NOT run signal_engine.py and does NOT modify day-trade production
    logic. It builds a live universe from Alpaca tradable US equities, merges
    protected prior Swing Watch/Ready names back into that universe, updates
    Daily/1H/15m/5m swing caches, runs a broad first pass, enriches candidates
    with swing-specific smart-money/news/earnings files, then runs the final
    shortlist pass and rebuilds the dashboard.
    """
    SWING_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_existing_swing_candidates()

    scanner_base_args = (
        "--source", "daily-hourly",
        "--symbols-file", str(SWING_LIVE_UNIVERSE_FILE),
        "--daily-root", str(SWING_DAILY_ROOT),
        "--hourly-root", str(SWING_HOURLY_ROOT),
        "--m15-root", str(SWING_M15_ROOT),
        "--m5-root", str(SWING_M5_ROOT),
        "--output-dir", str(SWING_RESULTS_DIR),
        "--market-context-file", str(PROJECT_DIR / "market_regime.json"),
    )

    logging.info("===== START SWING_SCAN =====")
    all_ok = True

    sequence: list[JobSpec] = [
        (SWING_UNIVERSE_SCRIPT, True, (
            "--output", str(SWING_LIVE_UNIVERSE_FILE),
        )),
    ]

    for script, required, args in sequence:
        result = run_python_script(script, args=args, required=required)
        if not result.ok:
            all_ok = False
            if required:
                logging.error("Stopping SWING_SCAN because required script failed: %s", script)
                publish_dashboard()
                logging.info("===== END SWING_SCAN | ok=%s =====", all_ok)
                return False

    merge_protected_swing_symbols_into_universe()

    remaining_sequence: list[JobSpec] = [
        (SWING_BAR_UPDATER_SCRIPT, True, (
            "--symbols-file", str(SWING_LIVE_UNIVERSE_FILE),
            "--daily-root", str(SWING_DAILY_ROOT),
            "--hourly-root", str(SWING_HOURLY_ROOT),
            "--m15-root", str(SWING_M15_ROOT),
            "--m5-root", str(SWING_M5_ROOT),
            "--hourly-chunk-size", "5",
            "--m15-chunk-size", "10",
            "--m5-chunk-size", "10",
            "--request-timeout", "45",
        )),
        # Broad first pass: enough rows for smart-money/news enrichment.
        (SWING_SCANNER_SCRIPT, True, scanner_base_args + (
            "--max-output-candidates", "120",
        )),
        (SWING_SMART_MONEY_SCRIPT, False, (
            "--candidate-file", str(SWING_CANDIDATE_FILE),
            "--output-file", str(SWING_SMART_MONEY_FILE),
            "--history-file", str(SWING_RESULTS_DIR / "swing_smart_money_scores_history.csv"),
            "--symbol-limit", "120",
        )),
        (SWING_NEWS_RISK_SCRIPT, False, (
            "--candidate-file", str(SWING_CANDIDATE_FILE),
            "--symbols-file", str(SWING_CANDIDATE_FILE),
            "--universe-file", str(SWING_LIVE_UNIVERSE_FILE),
            "--output-file", str(SWING_NEWS_RISK_FILE),
            "--earnings-file", str(SWING_EARNINGS_FILE),
            "--limit-symbols", "120",
        )),
        # Final pass: strict visible shortlist with real swing smart/news inputs.
        (SWING_SCANNER_SCRIPT, True, scanner_base_args + (
            "--smart-money-file", str(SWING_SMART_MONEY_FILE),
            "--news-risk-file", str(SWING_NEWS_RISK_FILE),
            "--earnings-csv", str(SWING_EARNINGS_FILE),
            "--max-output-candidates", "10",
        )),
        (DASHBOARD_SCRIPT, True, ()),
    ]

    for script, required, args in remaining_sequence:
        result = run_python_script(script, args=args, required=required)
        if not result.ok:
            all_ok = False
            if required:
                logging.error("Stopping SWING_SCAN because required script failed: %s", script)
                break
            logging.warning("Continuing SWING_SCAN despite optional script failure: %s", script)

    publish_dashboard()
    logging.info("===== END SWING_SCAN | ok=%s =====", all_ok)
    return all_ok



# =========================
# Publishing
# =========================

def publish_dashboard() -> None:
    """
    Copy dashboard output files into Nginx web directory.

    Important:
    elite_dashboard.py writes dashboard.html. The browser root serves index.html.
    Therefore dashboard.html is always copied to:
    - PROJECT_DIR/index.html
    - WEB_DIR/index.html
    - WEB_DIR/dashboard.html
    """
    try:
        WEB_DIR.mkdir(parents=True, exist_ok=True)

        copied = 0
        dashboard_src = PROJECT_DIR / "dashboard.html"

        if dashboard_src.exists() and dashboard_src.is_file():
            project_index = PROJECT_DIR / "index.html"
            shutil.copy2(dashboard_src, project_index)
            copied += 1

            shutil.copy2(dashboard_src, WEB_DIR / "index.html")
            copied += 1

            shutil.copy2(dashboard_src, WEB_DIR / "dashboard.html")
            copied += 1

            logging.info("Forced dashboard.html to project/web index.html and dashboard.html")
        else:
            logging.warning("dashboard.html not found; index.html cannot be refreshed")

        for pattern in PUBLISH_PATTERNS:
            for src in PROJECT_DIR.glob(pattern):
                if not src.is_file():
                    continue

                dst = WEB_DIR / src.name

                try:
                    if src.resolve() == dashboard_src.resolve():
                        # already copied explicitly above
                        continue
                    shutil.copy2(src, dst)
                    copied += 1
                except PermissionError:
                    logging.error("Permission denied publishing %s -> %s", src, dst)
                except Exception as exc:
                    logging.error("Failed publishing %s -> %s: %s", src, dst, exc)

        logging.info("Published %s files to %s", copied, WEB_DIR)

    except Exception as exc:
        logging.exception("Publish failed: %s", exc)


# =========================
# Main Scheduler
# =========================

class EliteRunner:
    def __init__(self) -> None:
        self.stop_requested = False

        self.last_premarket_key: Optional[str] = None
        self.last_scanner_key: Optional[str] = None
        self.last_after_hours_key: Optional[str] = None
        self.last_dashboard_only_key: Optional[str] = None
        self.last_swing_key: Optional[str] = None
        self.last_signal_refresh_epoch: float = 0.0
        self.active_job = False

    def request_stop(self, signum=None, frame=None) -> None:
        logging.info("Stop requested signum=%s", signum)
        self.stop_requested = True

    def run_job_locked(self, job_name: str, func) -> None:
        if self.active_job:
            logging.warning("Skipping %s because another job is active", job_name)
            return

        self.active_job = True

        try:
            logging.info("Job start: %s", job_name)
            func()
            logging.info("Job end: %s", job_name)
        finally:
            self.active_job = False

    def check_premarket_schedule(self, current: datetime) -> None:
        if not is_weekday(current):
            return

        current_hhmm = hhmm(current)
        if current_hhmm not in PREMARKET_SCAN_TIMES_ET:
            return

        key = f"{ymd(current)}:{current_hhmm}:premarket-scan"
        if self.last_premarket_key == key:
            return

        self.last_premarket_key = key
        self.run_job_locked(f"premarket-scan-{current_hhmm}", run_premarket_scan)

    def check_scanner_schedule(self, current: datetime) -> None:
        if not is_weekday(current):
            return

        current_hhmm = hhmm(current)
        if current_hhmm not in SCANNER_TIMES_ET:
            return

        key = f"{ymd(current)}:{current_hhmm}:scanner"
        if self.last_scanner_key == key:
            return

        self.last_scanner_key = key
        self.run_job_locked(f"scanner-{current_hhmm}", run_full_scanner)

    def check_after_hours_schedule(self, current: datetime) -> None:
        if not is_weekday(current):
            return

        current_hhmm = hhmm(current)
        if current_hhmm not in AFTER_HOURS_SCAN_TIMES_ET:
            return

        key = f"{ymd(current)}:{current_hhmm}:after-hours-scan"
        if self.last_after_hours_key == key:
            return

        self.last_after_hours_key = key
        self.run_job_locked(f"after-hours-scan-{current_hhmm}", run_after_hours_scan)

    def check_dashboard_only_schedule(self, current: datetime) -> None:
        if not is_weekday(current):
            return

        current_hhmm = hhmm(current)
        if current_hhmm not in DASHBOARD_ONLY_TIMES_ET:
            return

        key = f"{ymd(current)}:{current_hhmm}:dashboard-only"
        if self.last_dashboard_only_key == key:
            return

        self.last_dashboard_only_key = key
        self.run_job_locked(
            f"dashboard-only-{current_hhmm}",
            lambda: run_dashboard_only_refresh(reason=current_hhmm.replace(":", "")),
        )

    def check_swing_schedule(self, current: datetime) -> None:
        """
        Scheduled full Swing Desk scan.

        This is separate from day-trade Signal Desk refresh. It updates the
        live Swing universe and all Swing MTF caches before scanning, so the
        scanner cannot build candidates from stale cache files.
        """
        if not SWING_SCAN_ENABLED:
            return

        if not is_weekday(current):
            return

        current_hhmm = hhmm(current)
        if current_hhmm not in SWING_SCAN_TIMES_ET:
            return

        key = f"{ymd(current)}:{current_hhmm}:swing-scan"
        if self.last_swing_key == key:
            return

        self.last_swing_key = key
        self.run_job_locked(f"swing-scan-{current_hhmm}", run_swing_scan)

    def check_signal_refresh(self, current: datetime) -> None:
        if not is_weekday(current):
            return

        if not in_time_window(current, SIGNAL_REFRESH_START_ET, SIGNAL_REFRESH_END_ET):
            return

        now_epoch = time.time()
        if now_epoch - self.last_signal_refresh_epoch < SIGNAL_REFRESH_INTERVAL_SECONDS:
            return

        self.last_signal_refresh_epoch = now_epoch
        self.run_job_locked("signal-refresh", run_signal_refresh)

    def run_forever(self) -> None:
        logging.info("Elite Runner started")
        logging.info("Project dir: %s", PROJECT_DIR)
        logging.info("Python: %s", PYTHON_BIN)
        logging.info("Web dir: %s", WEB_DIR)
        logging.info("Pre-market scan times ET: %s", sorted(PREMARKET_SCAN_TIMES_ET))
        logging.info("Regular scanner times ET: %s", sorted(SCANNER_TIMES_ET))
        logging.info("After-hours scan times ET: %s", sorted(AFTER_HOURS_SCAN_TIMES_ET))
        logging.info(
            "Signal refresh ET: %s-%s every %ss",
            SIGNAL_REFRESH_START_ET,
            SIGNAL_REFRESH_END_ET,
            SIGNAL_REFRESH_INTERVAL_SECONDS,
        )
        logging.info("Dashboard-only times ET: %s", sorted(DASHBOARD_ONLY_TIMES_ET))
        logging.info("Swing scan enabled: %s", SWING_SCAN_ENABLED)
        logging.info("Swing scan times ET: %s", sorted(SWING_SCAN_TIMES_ET))
        logging.info("Extended movers script: %s", EXTENDED_MOVERS_SCRIPT)
        logging.info("Smart Money script: %s", SMART_MONEY_SCRIPT)
        logging.info("Sector rotation script: %s", SECTOR_ROTATION_SCRIPT)

        while not self.stop_requested:
            current = now_et()

            try:
                # Order matters:
                # 1. Monitor-only pre-market snapshots.
                # 2. Regular full scanner.
                # 3. Monitor-only after-hours snapshots.
                # 4. Dashboard-only boundary refresh.
                # 5. Scheduled full Swing Desk scan.
                # 6. Regular Signal Desk refresh / lightweight dashboard live-price overlay.
                self.check_premarket_schedule(current)
                self.check_scanner_schedule(current)
                self.check_after_hours_schedule(current)
                self.check_dashboard_only_schedule(current)
                self.check_swing_schedule(current)
                self.check_signal_refresh(current)

            except Exception as exc:
                logging.exception("Scheduler loop error: %s", exc)

            time.sleep(1)

        logging.info("Elite Runner stopped")


# =========================
# CLI
# =========================

def print_plan() -> None:
    print("Elite Runner Plan")
    print("=================")
    print("PREMARKET_SCAN:")
    print(f"  {MACRO_SCRIPT}")
    print(f"  {SECTOR_ROTATION_SCRIPT} if present")
    print(f"  {EXTENDED_MOVERS_SCRIPT} --session premarket")
    print(f"  {DASHBOARD_SCRIPT}")
    print("  NO elite_scanner.py / smart_money_bars_proxy.py / signal_engine.py")
    print()
    print("FULL_SCANNER:")
    print(f"  {MACRO_SCRIPT}")
    print(f"  {SCANNER_SCRIPT}")
    print(f"  {SECTOR_ROTATION_SCRIPT} if present")
    print(f"  {SMART_MONEY_SCRIPT}")
    print(f"  {SIGNAL_ENGINE_SCRIPT}")
    print(f"  {DASHBOARD_SCRIPT}")
    print()
    print("AFTER_HOURS_SCAN:")
    print(f"  {MACRO_SCRIPT}")
    print(f"  {SECTOR_ROTATION_SCRIPT} if present")
    print(f"  {EXTENDED_MOVERS_SCRIPT} --session afterhours")
    print(f"  {DASHBOARD_SCRIPT}")
    print("  NO elite_scanner.py / smart_money_bars_proxy.py / signal_engine.py")
    print()
    print("SIGNAL_REFRESH:")
    print(f"  {SMART_MONEY_SCRIPT}")
    print(f"  {SIGNAL_ENGINE_SCRIPT}")
    print(f"  {DASHBOARD_SCRIPT}")
    print()
    print("DASHBOARD_ONLY:")
    print(f"  {DASHBOARD_SCRIPT}")
    print()
    print("SWING_SCAN:")
    print(f"  {SWING_UNIVERSE_SCRIPT} -> {SWING_LIVE_UNIVERSE_FILE}")
    print("  merge protected prior Swing Watch/Ready symbols into live universe")
    print(f"  {SWING_BAR_UPDATER_SCRIPT} -> Daily/1H/15m/5m live swing caches")
    print(f"  {SWING_SCANNER_SCRIPT} first pass")
    print(f"  {SWING_SMART_MONEY_SCRIPT}")
    print(f"  {SWING_NEWS_RISK_SCRIPT}")
    print(f"  {SWING_SCANNER_SCRIPT} final pass")
    print(f"  {DASHBOARD_SCRIPT}")
    print("  NO signal_engine.py / NO broker execution")
    print()
    print(f"Pre-market times ET: {sorted(PREMARKET_SCAN_TIMES_ET)}")
    print(f"Regular scanner times ET: {sorted(SCANNER_TIMES_ET)}")
    print(f"After-hours times ET: {sorted(AFTER_HOURS_SCAN_TIMES_ET)}")
    print(f"Signal refresh ET: {SIGNAL_REFRESH_START_ET}-{SIGNAL_REFRESH_END_ET} every {SIGNAL_REFRESH_INTERVAL_SECONDS}s")
    print(f"Dashboard-only times ET: {sorted(DASHBOARD_ONLY_TIMES_ET)}")
    print(f"Swing scan enabled: {SWING_SCAN_ENABLED}")
    print(f"Swing scan times ET: {sorted(SWING_SCAN_TIMES_ET)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Elite Scanner VPS runner")
    parser.add_argument("--once-scan", action="store_true", help="Run one regular full scanner cycle and exit")
    parser.add_argument("--once-signal", action="store_true", help="Run one signal refresh cycle and exit")
    parser.add_argument("--once-dashboard", action="store_true", help="Run one dashboard-only refresh and exit")
    parser.add_argument("--once-premarket", action="store_true", help="Run one pre-market monitor-only scan and exit")
    parser.add_argument("--once-afterhours", action="store_true", help="Run one after-hours monitor-only scan and exit")
    parser.add_argument("--once-swing", action="store_true", help="Run one isolated live Swing Desk scan and exit")
    parser.add_argument("--publish-only", action="store_true", help="Publish current output files and exit")
    parser.add_argument("--print-plan", action="store_true", help="Print runner job plan and exit")
    return parser.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()

    if not PROJECT_DIR.exists():
        logging.error("Project directory does not exist: %s", PROJECT_DIR)
        return 1

    os.chdir(PROJECT_DIR)

    if args.print_plan:
        print_plan()
        return 0

    if args.publish_only:
        publish_dashboard()
        return 0

    if args.once_premarket:
        return 0 if run_premarket_scan() else 1

    if args.once_afterhours:
        return 0 if run_after_hours_scan() else 1

    if args.once_swing:
        return 0 if run_swing_scan() else 1

    if args.once_scan:
        return 0 if run_full_scanner() else 1

    if args.once_signal:
        return 0 if run_signal_refresh() else 1

    if args.once_dashboard:
        return 0 if run_dashboard_only_refresh(reason="MANUAL") else 1

    runner = EliteRunner()

    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)

    runner.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
