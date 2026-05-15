#!/usr/bin/env python3
"""
Elite Scanner VPS Runner

Purpose:
- Run full market scanner at scheduled ET times.
- Run Signal Desk refresh every 60 seconds during the regular monitoring window.
- Run dashboard-only session status refresh at 04:00 and 20:01 ET.
- Publish dashboard files to the Nginx web directory.

Important:
- Dashboard-only refresh NEVER runs elite_scanner.py or signal_engine.py.
- Dashboard-only refresh only rebuilds/publishes dashboard status.
- Full scanner and signal refresh behavior remain separate.
"""

from __future__ import annotations

import argparse
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
from typing import Iterable, Optional

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

# Full whole-market scanner schedule.
# These are discovery scans, not 60-sec signal refreshes.
# Option B selected: no 09:00 pre-market scanner because pre-market scanner results are not shown.
SCANNER_TIMES_ET = {
    "09:45",
    "10:30",
    "11:30",
    "13:30",
    "14:30",
}

# Signal Desk refresh:
# Refresh already-picked scanner tickers every 60 seconds.
# Runs signal_engine.py + elite_dashboard.py.
SIGNAL_REFRESH_START_ET = "09:46"
SIGNAL_REFRESH_END_ET = "16:05"
SIGNAL_REFRESH_INTERVAL_SECONDS = 60

# Dashboard-only session boundary refreshes.
# These do NOT run scanner or signal engine.
# 04:00 ET = show PRE-MARKET
# 20:01 ET = show CLOSED
# No 09:30 dashboard-only refresh.
# No 16:00 dashboard-only refresh because signal refresh runs until 16:05.
DASHBOARD_ONLY_TIMES_ET = {
    "04:00",
    "09:30",
    "20:01",
}

# Files to publish to Nginx after dashboard rebuilds.
PUBLISH_PATTERNS = [
    "index.html",
    "dashboard.html",
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


def run_python_script(script_name: str, timeout_seconds: int = 1800) -> RunResult:
    script_path = PROJECT_DIR / script_name
    start = time.monotonic()

    if not script_path.exists():
        logging.error("Missing script: %s", script_path)
        return RunResult(script_name, False, 127, 0.0)

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
            for line in output.splitlines()[-80:]:
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


def run_script_sequence(name: str, scripts: Iterable[str], publish_after: bool = True) -> bool:
    logging.info("===== START %s =====", name)

    all_ok = True

    for script in scripts:
        result = run_python_script(script)

        if not result.ok:
            all_ok = False
            logging.error("Stopping %s because %s failed", name, script)
            break

    if publish_after:
        publish_dashboard()

    logging.info("===== END %s | ok=%s =====", name, all_ok)
    return all_ok


# =========================
# Job Types
# =========================

def run_full_scanner() -> bool:
    """
    Whole-market discovery scan.

    Runs:
    - macro_calendar.py
    - elite_scanner.py
    - signal_engine.py
    - elite_dashboard.py

    Reason:
    After scanner refreshes the candidate universe, Signal Desk should immediately
    evaluate the fresh list once, then dashboard rebuilds.
    """
    return run_script_sequence(
        "FULL_SCANNER",
        [
            "macro_calendar.py",
            "elite_scanner.py",
            "signal_engine.py",
            "elite_dashboard.py",
        ],
        publish_after=True,
    )


def run_signal_refresh() -> bool:
    """
    Refresh already-picked scanner tickers only.

    Runs:
    - signal_engine.py
    - elite_dashboard.py

    Does NOT run elite_scanner.py.
    """
    return run_script_sequence(
        "SIGNAL_REFRESH",
        [
            "signal_engine.py",
            "elite_dashboard.py",
        ],
        publish_after=True,
    )


def run_dashboard_only_refresh(reason: str = "SESSION_STATUS") -> bool:
    """
    Dashboard-only refresh.

    Strict rule:
    - NO elite_scanner.py
    - NO signal_engine.py
    - NO Signal Desk promotion/change
    - NO TRIGGER_READY changes
    - NO ACTIVE_SIGNAL changes
    - ONLY elite_dashboard.py rebuild + publish

    Used at:
    - 04:00 ET PRE-MARKET label
    - 20:01 ET CLOSED label
    """
    return run_script_sequence(
        f"DASHBOARD_ONLY_{reason}",
        [
            "elite_dashboard.py",
        ],
        publish_after=True,
    )


# =========================
# Publishing
# =========================

def publish_dashboard() -> None:
    """
    Copy dashboard output files into Nginx web directory.

    This does not change scanner/signal state.
    """
    try:
        WEB_DIR.mkdir(parents=True, exist_ok=True)

        copied = 0

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

        self.last_scanner_key: Optional[str] = None
        self.last_dashboard_only_key: Optional[str] = None
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
        logging.info("Scanner times ET: %s", sorted(SCANNER_TIMES_ET))
        logging.info(
            "Signal refresh ET: %s-%s every %ss",
            SIGNAL_REFRESH_START_ET,
            SIGNAL_REFRESH_END_ET,
            SIGNAL_REFRESH_INTERVAL_SECONDS,
        )
        logging.info("Dashboard-only times ET: %s", sorted(DASHBOARD_ONLY_TIMES_ET))

        while not self.stop_requested:
            current = now_et()

            try:
                # Order matters:
                # 1. Full scanner at scheduled discovery times.
                # 2. Dashboard-only boundary refresh at 04:00 / 20:01.
                # 3. Signal refresh during 09:46-16:05 only.
                self.check_scanner_schedule(current)
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
    parser.add_argument("--once-scan", action="store_true", help="Run one full scanner cycle and exit")
    parser.add_argument("--once-signal", action="store_true", help="Run one signal refresh cycle and exit")
    parser.add_argument("--once-dashboard", action="store_true", help="Run one dashboard-only refresh and exit")
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
