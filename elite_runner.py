#!/usr/bin/env python3
"""
Elite Scanner VPS Runner

Purpose:
- Run pre-market monitor-only scanner snapshots.
- Run regular full market scanner at scheduled ET times.
- Run Signal Desk refresh every 60 seconds during the regular monitoring window.
- Run after-hours monitor-only scanner snapshots.
- Run dashboard-only session status refresh at 04:00, 09:30, and 20:01 ET.
- Publish dashboard files to the Nginx web directory.

Locked behavior:
- PREMARKET_SCAN and AFTER_HOURS_SCAN never run signal_engine.py.
- PREMARKET_SCAN writes premarket_movers.csv/json and then clears regular candidate files.
- AFTER_HOURS_SCAN writes after_hours_movers.csv/json and then clears regular candidate files.
- FULL_SCANNER runs scanner + sector + Smart Money + signal engine + dashboard.
- SIGNAL_REFRESH runs Smart Money + signal engine + dashboard.
- DASHBOARD_ONLY runs only elite_dashboard.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

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
SMART_MONEY_SCRIPT = "smart_money_bars_proxy.py"
SIGNAL_ENGINE_SCRIPT = "signal_engine.py"
DASHBOARD_SCRIPT = "elite_dashboard.py"

# Pre-market monitor-only scanner snapshots.
# These are discovery scans only. They do not create Signal Desk decisions.
PREMARKET_SCAN_TIMES_ET = {
    "07:00",
    "07:30",
    "08:00",
    "08:30",
    "09:00",
    "09:15",
}

# Regular whole-market scanner schedule.
# These are discovery scans used for regular Signal Desk decision flow.
SCANNER_TIMES_ET = {
    "09:45",
    "10:30",
    "11:30",
    "13:30",
    "14:30",
}

# After-hours monitor-only scanner snapshots.
# These are discovery scans only. They do not create Signal Desk decisions.
AFTER_HOURS_SCAN_TIMES_ET = {
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
# Refresh already-picked scanner tickers every 60 seconds.
# Runs smart_money_bars_proxy.py + signal_engine.py + elite_dashboard.py.
SIGNAL_REFRESH_START_ET = "09:46"
SIGNAL_REFRESH_END_ET = "16:05"
SIGNAL_REFRESH_INTERVAL_SECONDS = 60

# Dashboard-only session boundary refreshes.
# These do NOT run scanner, sector rotation, Smart Money, or signal engine.
# 04:00 ET = show PRE-MARKET / closed boundary status
# 09:30 ET = show MARKET OPEN boundary status
# 20:01 ET = show CLOSED boundary status
DASHBOARD_ONLY_TIMES_ET = {
    "04:00",
    "09:30",
    "20:01",
}

# Regular candidate files that must not be promoted from pre-market/after-hours scans.
REGULAR_CANDIDATE_FILES = [
    "potential_movers.csv",
    "active_momentum.csv",
    "elite_watchlist.csv",
    "elite_watchlist.json",
    "elite_watchlist_raw.csv",
    "extended_movers.csv",
    "high_risk_movers.csv",
]

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
    timeout_seconds: int = 1800,
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

    cmd = [PYTHON_BIN, str(script_path)]
    logging.info("Running: %s", " ".join(cmd))

    try:
        completed = subprocess.run(
            cmd,
            cwd=str(PROJECT_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )

        duration = time.monotonic() - start
        output = (completed.stdout or "").strip()

        if output:
            for line in output.splitlines()[-100:]:
                logging.info("[%s] %s", script_name, line)

        if completed.returncode == 0:
            logging.info("Completed %s in %.1fs", script_name, duration)
            return RunResult(script_name, True, 0, duration)

        logging.error(
            "FAILED %s rc=%s duration=%.1fs",
            script_name,
            completed.returncode,
            duration,
        )
        return RunResult(script_name, False, completed.returncode, duration)

    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - start
        logging.error("TIMEOUT %s after %.1fs", script_name, duration)

        if exc.stdout:
            logging.error("Partial output: %s", str(exc.stdout)[-4000:])

        return RunResult(script_name, False, 124, duration)

    except Exception as exc:
        duration = time.monotonic() - start
        logging.exception("ERROR running %s after %.1fs: %s", script_name, duration, exc)
        return RunResult(script_name, False, 1, duration)


def run_script_sequence(
    name: str,
    scripts: Sequence[Tuple[str, bool]],
    publish_after: bool = True,
) -> bool:
    """
    Run a mixed critical/non-critical sequence.

    scripts:
      (script_name, required)

    Required script failure stops the job.
    Optional script failure logs but continues.
    """
    logging.info("===== START %s =====", name)

    all_ok = True

    for script, required in scripts:
        result = run_python_script(script, required=required)

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
# Extended Session Snapshots
# =========================

def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            return [dict(row) for row in csv.DictReader(f)]
    except Exception as exc:
        logging.error("Failed reading CSV %s: %s", path, exc)
        return []


def write_csv_rows(path: Path, rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if rows:
        fieldnames: List[str] = []
        seen = set()
        for row in rows:
            for key in row.keys():
                if key not in seen:
                    seen.add(key)
                    fieldnames.append(key)
    else:
        fieldnames = [
            "symbol",
            "monitor_session",
            "source_bucket",
            "snapshot_generated_at_et",
            "monitor_only",
        ]

    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def clear_regular_candidate_outputs(reason: str) -> None:
    """
    Prevent pre-market / after-hours scans from being promoted into regular
    Potential Movers, Active Momentum, or Signal Desk candidate files.

    Dashboard will use premarket_movers.csv / after_hours_movers.csv for
    monitor-only display. Regular full scanner repopulates these files at
    scheduled market times.
    """
    logging.info("Clearing regular candidate outputs after %s snapshot", reason)

    for filename in REGULAR_CANDIDATE_FILES:
        path = PROJECT_DIR / filename

        try:
            if filename.endswith(".json"):
                path.write_text("[]\n", encoding="utf-8")
            elif filename.endswith(".csv"):
                write_csv_rows(path, [])
        except Exception as exc:
            logging.error("Failed clearing %s: %s", filename, exc)


def save_extended_session_snapshot(session_name: str) -> None:
    """
    Save ranked monitor-only mover list from the latest scanner output.

    session_name:
      PREMARKET
      AFTER_HOURS

    Output:
      premarket_movers.csv/json
      after_hours_movers.csv/json
    """
    session_upper = session_name.upper()
    now_label = now_et().isoformat(timespec="seconds")

    output_stem = "premarket_movers" if session_upper == "PREMARKET" else "after_hours_movers"
    csv_out = PROJECT_DIR / f"{output_stem}.csv"
    json_out = PROJECT_DIR / f"{output_stem}.json"

    sources = [
        ("potential_movers.csv", "POTENTIAL_MOVER"),
        ("active_momentum.csv", "ACTIVE_MOMENTUM"),
    ]

    rows: List[Dict[str, object]] = []

    for filename, source_bucket in sources:
        for row in read_csv_rows(PROJECT_DIR / filename):
            item: Dict[str, object] = dict(row)
            item["monitor_session"] = session_upper
            item["source_bucket"] = source_bucket
            item["snapshot_generated_at_et"] = now_label
            item["monitor_only"] = "true"
            item["execution_allowed"] = "false"
            item["monitor_label"] = (
                "Monitor Only — No Entries Before Regular Market Open"
                if session_upper == "PREMARKET"
                else "Monitor Only — No After-Hours Entries"
            )
            rows.append(item)

    # Preserve scanner ranking order: potential first, active second.
    # Limit to a focused monitor list for dashboard readability.
    max_rows = int(os.getenv("EXTENDED_SESSION_MAX_MOVERS", "20"))
    rows = rows[:max_rows]

    write_csv_rows(csv_out, rows)

    payload = {
        "metadata": {
            "session": session_upper,
            "generated_at_et": now_label,
            "monitor_only": True,
            "execution_allowed": False,
            "source_files": [s[0] for s in sources],
            "rows": len(rows),
            "max_rows": max_rows,
        },
        "symbols": rows,
    }

    json_out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    logging.info(
        "Saved %s monitor snapshot: %s rows -> %s / %s",
        session_upper,
        len(rows),
        csv_out.name,
        json_out.name,
    )


# =========================
# Job Types
# =========================

def run_premarket_scan() -> bool:
    """
    Pre-market monitor-only scan.

    Runs:
    - macro_calendar.py
    - elite_scanner.py
    - sector_rotation.py if present
    - smart_money_bars_proxy.py
    - save premarket_movers.csv/json
    - clear regular candidate outputs
    - elite_dashboard.py

    Does NOT run signal_engine.py.
    """
    logging.info("===== START PREMARKET_SCAN =====")

    all_ok = True

    sequence = [
        (MACRO_SCRIPT, True),
        (SCANNER_SCRIPT, True),
        (SECTOR_ROTATION_SCRIPT, False),
        (SMART_MONEY_SCRIPT, False),
    ]

    for script, required in sequence:
        result = run_python_script(script, required=required)
        if not result.ok:
            all_ok = False
            if required:
                logging.error("Stopping PREMARKET_SCAN because required script failed: %s", script)
                break

    if all_ok:
        save_extended_session_snapshot("PREMARKET")
        clear_regular_candidate_outputs("PREMARKET")
        dash = run_python_script(DASHBOARD_SCRIPT, required=True)
        all_ok = all_ok and dash.ok

    publish_dashboard()
    logging.info("===== END PREMARKET_SCAN | ok=%s =====", all_ok)
    return all_ok


def run_after_hours_scan() -> bool:
    """
    After-hours monitor-only scan.

    Runs:
    - macro_calendar.py
    - elite_scanner.py
    - sector_rotation.py if present
    - smart_money_bars_proxy.py
    - save after_hours_movers.csv/json
    - clear regular candidate outputs
    - elite_dashboard.py

    Does NOT run signal_engine.py.
    """
    logging.info("===== START AFTER_HOURS_SCAN =====")

    all_ok = True

    sequence = [
        (MACRO_SCRIPT, True),
        (SCANNER_SCRIPT, True),
        (SECTOR_ROTATION_SCRIPT, False),
        (SMART_MONEY_SCRIPT, False),
    ]

    for script, required in sequence:
        result = run_python_script(script, required=required)
        if not result.ok:
            all_ok = False
            if required:
                logging.error("Stopping AFTER_HOURS_SCAN because required script failed: %s", script)
                break

    if all_ok:
        save_extended_session_snapshot("AFTER_HOURS")
        clear_regular_candidate_outputs("AFTER_HOURS")
        dash = run_python_script(DASHBOARD_SCRIPT, required=True)
        all_ok = all_ok and dash.ok

    publish_dashboard()
    logging.info("===== END AFTER_HOURS_SCAN | ok=%s =====", all_ok)
    return all_ok


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

    Reason:
    After scanner refreshes the candidate universe, Smart Money and Signal Desk
    should immediately evaluate the fresh list once, then dashboard rebuilds.
    """
    return run_script_sequence(
        "FULL_SCANNER",
        [
            (MACRO_SCRIPT, True),
            (SCANNER_SCRIPT, True),
            (SECTOR_ROTATION_SCRIPT, False),
            (SMART_MONEY_SCRIPT, False),
            (SIGNAL_ENGINE_SCRIPT, True),
            (DASHBOARD_SCRIPT, True),
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
    """
    return run_script_sequence(
        "SIGNAL_REFRESH",
        [
            (SMART_MONEY_SCRIPT, False),
            (SIGNAL_ENGINE_SCRIPT, True),
            (DASHBOARD_SCRIPT, True),
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
    - NO signal_engine.py
    - NO Signal Desk promotion/change
    - NO TRIGGER_READY changes
    - NO ACTIVE_SIGNAL changes
    - ONLY elite_dashboard.py rebuild + publish

    Used at:
    - 04:00 ET PRE-MARKET boundary/status label
    - 09:30 ET MARKET OPEN boundary/status label
    - 20:01 ET CLOSED boundary/status label
    """
    return run_script_sequence(
        f"DASHBOARD_ONLY_{reason}",
        [
            (DASHBOARD_SCRIPT, True),
        ],
        publish_after=True,
    )


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

    This prevents the root URL from serving stale index.html.
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
        self.last_signal_refresh_epoch: float = 0.0
        self.active_job = False

    def request_stop(self, signum=None, frame=None) -> None:
        logging.info("Stop requested signum=%s", signum)
        self.stop_requested = True

    def run_job_locked(self, job_name: str, func: Callable[[], bool]) -> None:
        if self.active_job:
            logging.warning("Skipping %s because another job is active", job_name)
            return

        self.active_job = True

        try:
            logging.info("Job start: %s", job_name)
            ok = func()
            logging.info("Job end: %s | ok=%s", job_name, ok)
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
        logging.info("Smart Money script: %s", SMART_MONEY_SCRIPT)
        logging.info("Sector rotation script: %s", SECTOR_ROTATION_SCRIPT)

        while not self.stop_requested:
            current = now_et()

            try:
                # Order matters:
                # 1. Pre-market monitor-only scans.
                # 2. Regular full scanner at scheduled discovery times.
                # 3. After-hours monitor-only scans.
                # 4. Dashboard-only boundary refresh at 04:00 / 09:30 / 20:01.
                # 5. Signal refresh during 09:46-16:05 only.
                self.check_premarket_schedule(current)
                self.check_scanner_schedule(current)
                self.check_after_hours_schedule(current)
                self.check_dashboard_only_schedule(current)
                self.check_signal_refresh(current)

            except Exception as exc:
                logging.exception("Scheduler loop error: %s", exc)

            time.sleep(1)

        logging.info("Elite Runner stopped")


# =========================
# CLI
# =========================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Elite Scanner VPS runner")
    parser.add_argument("--once-scan", action="store_true", help="Run one regular full scanner cycle and exit")
    parser.add_argument("--once-signal", action="store_true", help="Run one signal refresh cycle and exit")
    parser.add_argument("--once-dashboard", action="store_true", help="Run one dashboard-only refresh and exit")
    parser.add_argument("--once-premarket", action="store_true", help="Run one pre-market monitor-only scan and exit")
    parser.add_argument("--once-afterhours", action="store_true", help="Run one after-hours monitor-only scan and exit")
    parser.add_argument("--publish-only", action="store_true", help="Publish current output files and exit")
    return parser.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()

    if not PROJECT_DIR.exists():
        logging.error("Project directory does not exist: %s", PROJECT_DIR)
        return 1

    os.chdir(PROJECT_DIR)

    if args.publish_only:
        publish_dashboard()
        return 0

    if args.once_premarket:
        return 0 if run_premarket_scan() else 1

    if args.once_afterhours:
        return 0 if run_after_hours_scan() else 1

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
