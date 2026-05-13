#!/usr/bin/env python3
"""
Elite Scanner VPS Runner

Purpose:
- Replaces unreliable GitHub scheduled workflows.
- Runs broad scanner at fixed NY market times.
- Runs Signal Desk refresh every 60 seconds during market hours.
- Rebuilds dashboard and publishes it to Nginx web folder.

Current behavior:
- Broad scanner: 09:00, 09:45, 13:30 ET
- Signal refresh: every 60 seconds, Mon-Fri 09:30–16:05 ET
- Dashboard publish: after every scanner/signal run
- No auto-trading. Manual trading discipline remains unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


# =========================
# CONFIG
# =========================

PROJECT_DIR = Path("/opt/elite-scanner")
WEB_DIR = Path("/var/www/elite-scanner")
ENV_FILE = PROJECT_DIR / ".env"

PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"

RUNNER_LOG = PROJECT_DIR / "elite_runner.log"
RUNNER_STATE = PROJECT_DIR / "runner_state.json"
RUNNER_STATUS = PROJECT_DIR / "runner_status.json"

NY_TZ = ZoneInfo("America/New_York") if ZoneInfo else None

SIGNAL_REFRESH_SECONDS = 60

# Broad scanner schedule in NY time.
FULL_SCAN_TIMES = [
    dt_time(9, 0),
    dt_time(9, 45),
    dt_time(13, 30),
]

# Signal Desk refresh window.
# We still refresh during blackout windows because protected TRIGGER_READY /
# ACTIVE_SIGNAL names must be monitored, suppressed, expired, or invalidated.
SIGNAL_REFRESH_START = dt_time(9, 30)
SIGNAL_REFRESH_END = dt_time(16, 5)

# If runner starts a little late, allow catching scanner jobs within this window.
SCAN_GRACE_MINUTES = 20


# =========================
# UTILITIES
# =========================

def now_et() -> datetime:
    if NY_TZ:
        return datetime.now(NY_TZ)
    return datetime.now()


def timestamp() -> str:
    return now_et().isoformat(timespec="seconds")


def log(message: str) -> None:
    line = f"{timestamp()} | {message}"
    print(line, flush=True)
    try:
        with RUNNER_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5


def load_env_file() -> Dict[str, str]:
    """
    Load .env manually and pass it to subprocesses.
    This avoids requiring python-dotenv.
    """
    env = os.environ.copy()

    if not ENV_FILE.exists():
        log(f"WARNING: .env file not found at {ENV_FILE}")
        return env

    try:
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    except Exception as exc:
        log(f"ERROR loading .env: {exc}")

    return env


def load_state() -> Dict:
    if not RUNNER_STATE.exists():
        return {
            "full_scans": {},
            "last_signal_refresh": None,
            "last_publish": None,
        }

    try:
        return json.loads(RUNNER_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {
            "full_scans": {},
            "last_signal_refresh": None,
            "last_publish": None,
        }


def save_state(state: Dict) -> None:
    RUNNER_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def write_status(status: Dict) -> None:
    payload = {
        "runner_generated_at_et": timestamp(),
        "project_dir": str(PROJECT_DIR),
        "web_dir": str(WEB_DIR),
        "signal_refresh_seconds": SIGNAL_REFRESH_SECONDS,
        **status,
    }

    RUNNER_STATUS.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    try:
        WEB_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(RUNNER_STATUS, WEB_DIR / "runner_status.json")
    except Exception as exc:
        log(f"WARNING: could not publish runner_status.json: {exc}")


@dataclass
class CommandResult:
    name: str
    ok: bool
    returncode: int
    duration_seconds: float


def run_command(name: str, args: List[str], timeout_seconds: int) -> CommandResult:
    env = load_env_file()
    started = time.time()

    log(f"START {name}: {' '.join(args)}")

    try:
        completed = subprocess.run(
            args,
            cwd=str(PROJECT_DIR),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )

        duration = time.time() - started

        if completed.stdout:
            for line in completed.stdout.splitlines()[-80:]:
                log(f"{name} STDOUT | {line}")

        if completed.stderr:
            for line in completed.stderr.splitlines()[-80:]:
                log(f"{name} STDERR | {line}")

        ok = completed.returncode == 0

        if ok:
            log(f"END {name}: OK in {duration:.1f}s")
        else:
            log(f"END {name}: FAILED rc={completed.returncode} in {duration:.1f}s")

        return CommandResult(name, ok, completed.returncode, duration)

    except subprocess.TimeoutExpired:
        duration = time.time() - started
        log(f"END {name}: TIMEOUT after {duration:.1f}s")
        return CommandResult(name, False, 124, duration)

    except Exception as exc:
        duration = time.time() - started
        log(f"END {name}: ERROR {exc} after {duration:.1f}s")
        return CommandResult(name, False, 1, duration)


def publish_dashboard() -> bool:
    """
    Copy generated dashboard/data files to Nginx web directory.
    """
    WEB_DIR.mkdir(parents=True, exist_ok=True)

    copied = 0

    file_map = {
        "dashboard.html": "index.html",
        "index.html": "dashboard_original_index.html",
    }

    for src_name, dst_name in file_map.items():
        src = PROJECT_DIR / src_name
        if src.exists():
            shutil.copy2(src, WEB_DIR / dst_name)
            copied += 1

    patterns = [
        "*.json",
        "*.csv",
        "*.log",
    ]

    for pattern in patterns:
        for src in PROJECT_DIR.glob(pattern):
            if src.name == "elite_runner.log":
                # Publish it too, but keep name clear.
                pass
            shutil.copy2(src, WEB_DIR / src.name)
            copied += 1

    assets_src = PROJECT_DIR / "assets"
    assets_dst = WEB_DIR / "assets"
    if assets_src.exists() and assets_src.is_dir():
        if assets_dst.exists():
            shutil.rmtree(assets_dst)
        shutil.copytree(assets_src, assets_dst)
        copied += 1

    state = load_state()
    state["last_publish"] = timestamp()
    save_state(state)

    log(f"PUBLISH dashboard/data to {WEB_DIR} | copied={copied}")
    return True


def run_full_scan(reason: str) -> bool:
    """
    Full broad market scan + macro calendar + signal refresh + dashboard.
    """
    log(f"FULL_SCAN requested | reason={reason}")

    results = [
        run_command("elite_scanner", [str(PYTHON), "elite_scanner.py"], timeout_seconds=900),
        run_command("macro_calendar", [str(PYTHON), "macro_calendar.py"], timeout_seconds=240),
        run_command("signal_engine", [str(PYTHON), "signal_engine.py"], timeout_seconds=240),
        run_command("elite_dashboard", [str(PYTHON), "elite_dashboard.py"], timeout_seconds=240),
    ]

    publish_dashboard()

    ok = all(r.ok for r in results)

    write_status({
        "last_action": "full_scan",
        "last_action_reason": reason,
        "last_action_ok": ok,
        "last_action_results": [r.__dict__ for r in results],
    })

    return ok


def run_signal_refresh(reason: str) -> bool:
    """
    Focused signal refresh only. Does not run broad scanner.
    """
    log(f"SIGNAL_REFRESH requested | reason={reason}")

    results = [
        run_command("signal_engine", [str(PYTHON), "signal_engine.py"], timeout_seconds=240),
        run_command("elite_dashboard", [str(PYTHON), "elite_dashboard.py"], timeout_seconds=240),
    ]

    publish_dashboard()

    ok = all(r.ok for r in results)

    state = load_state()
    state["last_signal_refresh"] = timestamp()
    save_state(state)

    write_status({
        "last_action": "signal_refresh",
        "last_action_reason": reason,
        "last_action_ok": ok,
        "last_action_results": [r.__dict__ for r in results],
    })

    return ok


def should_run_signal_refresh(now: datetime, last_run_epoch: Optional[float]) -> bool:
    if not is_weekday(now):
        return False

    if not (SIGNAL_REFRESH_START <= now.time() <= SIGNAL_REFRESH_END):
        return False

    if last_run_epoch is None:
        return True

    return (time.time() - last_run_epoch) >= SIGNAL_REFRESH_SECONDS


def should_run_full_scan(now: datetime, state: Dict) -> Optional[str]:
    """
    Returns schedule label if a full scan should run, otherwise None.
    """
    if not is_weekday(now):
        return None

    full_scans = state.setdefault("full_scans", {})
    today = now.date().isoformat()

    for scan_time in FULL_SCAN_TIMES:
        label = scan_time.strftime("%H:%M")
        key = f"{today}_{label}"

        if full_scans.get(key):
            continue

        scheduled_dt = now.replace(
            hour=scan_time.hour,
            minute=scan_time.minute,
            second=0,
            microsecond=0,
        )

        grace_end = scheduled_dt + timedelta(minutes=SCAN_GRACE_MINUTES)

        if scheduled_dt <= now <= grace_end:
            return label

    return None


def mark_full_scan_done(label: str, state: Dict) -> None:
    today = now_et().date().isoformat()
    key = f"{today}_{label}"
    state.setdefault("full_scans", {})[key] = timestamp()

    # Keep state compact: remove full scan entries older than 10 days.
    cutoff = now_et().date() - timedelta(days=10)
    cleaned = {}
    for k, v in state.get("full_scans", {}).items():
        try:
            date_part = k.split("_", 1)[0]
            if datetime.fromisoformat(date_part).date() >= cutoff:
                cleaned[k] = v
        except Exception:
            cleaned[k] = v

    state["full_scans"] = cleaned
    save_state(state)


def outputs_exist() -> bool:
    required = [
        PROJECT_DIR / "potential_movers.csv",
        PROJECT_DIR / "active_momentum.csv",
        PROJECT_DIR / "signal_desk.json",
        PROJECT_DIR / "dashboard.html",
    ]
    return all(p.exists() for p in required)


def main_loop() -> None:
    log("=" * 70)
    log("Elite Scanner VPS Runner starting")
    log(f"Project dir: {PROJECT_DIR}")
    log(f"Web dir: {WEB_DIR}")
    log(f"Signal refresh: {SIGNAL_REFRESH_SECONDS}s")
    log("=" * 70)

    if not PYTHON.exists():
        log(f"FATAL: Python venv not found: {PYTHON}")
        sys.exit(1)

    WEB_DIR.mkdir(parents=True, exist_ok=True)

    # Publish existing dashboard immediately.
    try:
        publish_dashboard()
    except Exception as exc:
        log(f"WARNING: initial publish failed: {exc}")

    last_signal_epoch: Optional[float] = None

    while True:
        try:
            now = now_et()
            state = load_state()

            scan_label = should_run_full_scan(now, state)
            if scan_label:
                ok = run_full_scan(reason=f"scheduled_{scan_label}_ET")
                mark_full_scan_done(scan_label, state)
                last_signal_epoch = time.time()
                log(f"Scheduled full scan {scan_label} completed ok={ok}")

            elif should_run_signal_refresh(now, last_signal_epoch):
                ok = run_signal_refresh(reason="scheduled_60s_market_refresh")
                last_signal_epoch = time.time()
                log(f"Scheduled signal refresh completed ok={ok}")

            else:
                write_status({
                    "last_action": "idle",
                    "market_time_et": timestamp(),
                    "market_day": is_weekday(now),
                    "inside_signal_refresh_window": bool(
                        is_weekday(now) and SIGNAL_REFRESH_START <= now.time() <= SIGNAL_REFRESH_END
                    ),
                })

            time.sleep(10)

        except KeyboardInterrupt:
            log("Runner stopped by KeyboardInterrupt")
            break

        except Exception as exc:
            log(f"RUNNER_LOOP_ERROR: {exc}")
            write_status({
                "last_action": "runner_error",
                "error": str(exc),
            })
            time.sleep(30)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Elite Scanner VPS Runner")
    parser.add_argument("--once-full", action="store_true", help="Run one full scanner cycle and exit")
    parser.add_argument("--once-signal", action="store_true", help="Run one Signal Desk refresh cycle and exit")
    parser.add_argument("--publish-only", action="store_true", help="Publish current dashboard/data and exit")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.publish_only:
        publish_dashboard()
        write_status({"last_action": "publish_only", "last_action_ok": True})
        return

    if args.once_full:
        ok = run_full_scan(reason="manual_once_full")
        sys.exit(0 if ok else 1)

    if args.once_signal:
        ok = run_signal_refresh(reason="manual_once_signal")
        sys.exit(0 if ok else 1)

    main_loop()


if __name__ == "__main__":
    main()
