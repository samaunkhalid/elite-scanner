"""
signal_engine.py
----------------
Signal Desk v1 engine for the Elite Scanner project.

Purpose:
  - Read the scanner's focused candidates:
      * Top 12 potential_movers.csv
      * Top 8 active_momentum.csv
  - Monitor those names with Alpaca intraday bars/quotes.
  - Preserve protected TRIGGER_READY / ACTIVE_SIGNAL states across refreshes.
  - Write:
      * signal_desk.json
      * signal_state.json
      * suppressed_signals.csv

Core design:
  - 1-minute bars = execution timing and live invalidation.
  - 5-minute bars = setup structure, stop/target construction, and pattern quality.
  - Long-side only.
  - Red/risk-off market can still allow RELATIVE-STRENGTH WATCH candidates.
  - Invalid trade plans must NOT appear as WATCH.
  - Late-day setups require stronger volume, no bearish divergence, and EMA9 confirmation.
  - VWAP touch logic is setup-relative: opening VWAP noise is ignored and counts reset after bullish reclaim.

Important:
  - This engine generates dashboard signals only.
  - It does NOT place orders.
  - Manual chart confirmation is still required before any trade.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import requests

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


# ==============================================================
# CONFIG
# ==============================================================

POTENTIAL_LIMIT = 12
ACTIVE_LIMIT = 8

DATA_FEED = os.getenv("ALPACA_DATA_FEED", "sip").strip().lower() or "sip"
ALPACA_BASE_URL = "https://data.alpaca.markets/v2"

POTENTIAL_FILE = "potential_movers.csv"
ACTIVE_FILE = "active_momentum.csv"
RAW_WATCHLIST_FILE = "elite_watchlist_raw.csv"
MARKET_REGIME_FILE = "market_regime.json"

# Optional Phase 1 Smart Money Proxy output.
# Written by smart_money_bars_proxy.py. If this file is missing, stale, or
# malformed, the signal engine falls back to the original scanner scores.
SMART_MONEY_FILE = os.getenv("SMART_MONEY_OUTPUT_FILE", "smart_money_scores.json")
SMART_MONEY_MAX_AGE_SECONDS = int(os.getenv("SMART_MONEY_MAX_AGE_SECONDS", "180"))
SMART_MONEY_ADJUSTMENT_CAP = float(os.getenv("SMART_MONEY_ADJUSTMENT_CAP", "5"))

SIGNAL_DESK_FILE = "signal_desk.json"
SIGNAL_STATE_FILE = "signal_state.json"
SUPPRESSED_SIGNALS_FILE = "suppressed_signals.csv"
SIGNAL_OUTCOMES_FILE = "signal_outcomes.csv"
SIGNAL_OUTCOMES_SUMMARY_FILE = "signal_outcomes_summary.json"
SIGNAL_ENGINE_STRATEGY_VERSION = "v2.5_trigger_ready_hold_confirmation"

# State retention.
ACTIVE_STALE_MINUTES = 10
TRIGGER_READY_STALE_MINUTES = 45
RECENT_INVALIDATED_KEEP_MINUTES = 30

# Quality thresholds.
MIN_AVG_DOLLAR_VOL_M = 25.0
MAX_VWAP_EXTENSION_PCT = 5.0
ACTIVE_HOD_MAX_DISTANCE = -2.5
POTENTIAL_HOD_MAX_DISTANCE = -4.0
HOD_BREAKOUT_READY_DISTANCE = -0.75

# HOD breakout discipline.
# A valid HOD breakout is NOT a vertical push into the high. It must first form
# a controlled near-HOD base/flag with higher lows / tight range, then break.
HOD_BASE_MAX_DISTANCE_FROM_HOD_PCT = -0.85
HOD_BASE_MAX_LOW_FROM_HOD_PCT = -2.50
HOD_BASE_MAX_RANGE_PCT = 2.00
HOD_BASE_MIN_STRUCTURE_BARS = 3
HOD_BASE_MAX_VWAP_EXTENSION_PCT = 4.00

MIN_RR_WATCH = 0.75
MIN_RR = 1.5
MIN_CONF_WATCH = 60.0
MIN_CONF_READY = 75.0
MIN_CONF_READY_LATE_DAY = 80.0
MIN_CONF_ACTIVE = 80.0

# Trigger-touch confirmation prevents noisy same-candle Active -> Invalidated events.
# A Trigger Ready setup first becomes TRIGGER_TOUCHED when price reaches entry.
# It must then hold/confirm on a later refresh before it becomes ACTIVE_SIGNAL.
TRIGGER_TOUCH_CONFIRM_MIN_SECONDS = 45
TRIGGER_TOUCH_MAX_MINUTES = 4
TRIGGER_REJECT_BUFFER_PCT = 0.25
TRIGGER_WICK_REJECTION_PCT = 0.75
# Reclaim-runner confirmation and invalidation discipline.
# NU/CIFR showed that a reclaim trigger touch alone is not enough: the latest
# 1-minute close must still hold VWAP + EMA9 before ACTIVE, while ACTIVE reclaim
# signals should not be invalidated on a single VWAP wick if reclaim support holds.
RECLAIM_ACTIVE_CONFIRM_MIN_SECONDS = int(os.getenv("RECLAIM_ACTIVE_CONFIRM_MIN_SECONDS", "60"))
RECLAIM_ACTIVE_REQUIRE_CLOSE_ABOVE_VWAP = os.getenv("RECLAIM_ACTIVE_REQUIRE_CLOSE_ABOVE_VWAP", "1").strip() != "0"
RECLAIM_ACTIVE_REQUIRE_CLOSE_ABOVE_EMA9 = os.getenv("RECLAIM_ACTIVE_REQUIRE_CLOSE_ABOVE_EMA9", "1").strip() != "0"
RECLAIM_ACTIVE_MAX_VWAP_UNDERCUT_PCT = float(os.getenv("RECLAIM_ACTIVE_MAX_VWAP_UNDERCUT_PCT", "0.20"))
RECLAIM_ACTIVE_VWAP_LOSS_CLOSES = int(os.getenv("RECLAIM_ACTIVE_VWAP_LOSS_CLOSES", "2"))
RECLAIM_ACTIVE_SUPPORT_BREAK_BUFFER_PCT = float(os.getenv("RECLAIM_ACTIVE_SUPPORT_BREAK_BUFFER_PCT", "0.25"))
# If an entry has already traded and price falls away, do not recycle it as a fresh Trigger Ready.
TRIGGER_TOUCH_EPS_PCT = 0.03
TRIGGER_PULLBACK_REJECT_PCT = 0.20
MAX_QUOTE_SPREAD_FOR_PRICE_PCT = 1.00

# Setup-specific confirmation/invalidation grace.
# HOD breakouts must prove themselves quickly; VWAP reclaim/pullback and base
# setups get more room because normal retests can dip near EMA9/VWAP before continuing.
HOD_BREAKOUT_GRACE_MINUTES = 1.5
VWAP_RECLAIM_GRACE_MINUTES = 3.0
VWAP_PULLBACK_GRACE_MINUTES = 3.0
BASE_SQUEEZE_GRACE_MINUTES = 2.0

# Live participation / VWAP reclaim filters.
# Alpaca SIP is consolidated; thresholds remain intentionally conservative.
MIN_LIVE_1M_AVG_VOL_WATCH = 500.0
MIN_LIVE_5M_DOLLAR_VOL_WATCH = 25_000.0
MIN_LIVE_1M_AVG_VOL_READY = 1_000.0
MIN_LIVE_5M_DOLLAR_VOL_READY = 50_000.0
MIN_CONF_RECLAIM_READY = 68.0
VWAP_RECLAIM_LOOKBACK_MINUTES = 6
VWAP_RECLAIM_LIFECYCLE_MINUTES = 30
VWAP_RECLAIM_MIN_VOLUME_RATIO = 1.35
VWAP_RECLAIM_MAX_EXTENSION_PCT = 2.0
RECLAIM_PULLBACK_MAX_EXTENSION_PCT = 3.0
RECLAIM_PULLBACK_SUPPORT_BUFFER_PCT = 0.50
MIN_CONF_RECLAIM_PULLBACK_READY = 68.0

# Trigger Ready discipline for VWAP/reclaim lifecycle setups.
# These settings prevent a ticker from being tagged TRIGGER_READY while it is
# still merely pulling back toward VWAP/EMA support and the breakout trigger is
# far above current price. UMC/FIG exposed this issue: the plan was a breakout
# trigger, but the card looked like a VWAP pullback entry.
READY_TRIGGER_MAX_DISTANCE_PCT = float(os.getenv("READY_TRIGGER_MAX_DISTANCE_PCT", "0.75"))
READY_TRIGGER_MAX_DISTANCE_STRONG_VOLUME_PCT = float(os.getenv("READY_TRIGGER_MAX_DISTANCE_STRONG_VOLUME_PCT", "1.00"))
VWAP_READY_MIN_HOLD_BARS = int(os.getenv("VWAP_READY_MIN_HOLD_BARS", "2"))
VWAP_READY_MIN_GREEN_OR_FLAT_BARS = int(os.getenv("VWAP_READY_MIN_GREEN_OR_FLAT_BARS", "1"))
VWAP_READY_VWAP_LOW_BUFFER_PCT = float(os.getenv("VWAP_READY_VWAP_LOW_BUFFER_PCT", "0.20"))
RECLAIM_READY_MIN_HOLD_MINUTES = float(os.getenv("RECLAIM_READY_MIN_HOLD_MINUTES", "1.0"))

# Earnings-day reaction handling.
# We do not treat earnings-day moves as normal setups. They are allowed only as
# higher-risk reaction trades with stricter R/R + live participation requirements.
MIN_RR_EARNINGS_REACTION = 1.8
MIN_CONF_EARNINGS_READY = 75.0
MIN_EARNINGS_5M_DOLLAR_VOL_READY = 100_000.0
MIN_EARNINGS_1M_AVG_VOL_READY = 2_000.0

# VWAP pullback / reclaim MACD lifecycle tuning.
# 1-minute MACD is used for early reclaim timing/curl; 5-minute MACD is used for
# broader confirmation and hard risk warnings.
VWAP_LIFECYCLE_MACD_HARD_FAIL_PRICE_BELOW_EMA9 = True

# Lunch-time handling.
# Lunch is noisy and lower-liquidity, so no automatic ACTIVE_SIGNAL is allowed.
# Strong VWAP reclaim / pullback setups may still become TRIGGER_READY with a
# red caution flag so the user can manually inspect the chart instead of missing
# the ticker entirely.
LUNCH_CAUTION_PHASE = "LUNCH_BLACKOUT"
LUNCH_CAUTION_WARNING = (
    "Lunch blackout: setup detected during lower-liquidity lunch period. "
    "No automatic Active Signal; manual chart confirmation required. "
    "Fakeout risk is higher."
)


# Late-day / quality filters.
LATE_DAY_READY_START = dtime(14, 30)
VOLUME_FADE_RATIO = 0.60          # Recent 5x5m volume < 60% of morning reference = fading.
VOLUME_EXPANSION_RATIO = 1.05     # Recent 5x5m volume must be > prior 5x5m by 5% for late-day ready.
MIN_LATE_DAY_MORNING_VOL_RATIO = 0.60
BEARISH_DIVERGENCE_PENALTY = 10
VOLUME_FADE_PENALTY = 8
LATE_DAY_NO_VOLUME_EXPANSION_PENALTY = 5

# EMA 9 confirmation.
EMA_SPAN = 9
EMA9_BULLISH_BONUS_MAX = 10
EMA9_BELOW_PRICE_PENALTY = 5
EMA9_FALLING_PENALTY = 4
EMA9_BELOW_VWAP_PENALTY = 4

# MACD confirmation.
MACD_FAST_SPAN = 12
MACD_SLOW_SPAN = 26
MACD_SIGNAL_SPAN = 9
MACD_BULLISH_BONUS_MAX = 8
MACD_BEARISH_CROSSOVER_PENALTY = 8
MACD_HISTOGRAM_WEAKENING_PENALTY = 4

MAX_STOP_DIST_NORMAL = 3.0
MAX_STOP_DIST_HOD = 3.5

EXECUTION_TIMEFRAME = "1Min"
STRUCTURE_TIMEFRAME = "5Min"

DEBUG = os.getenv("SIGNAL_DEBUG", "0").strip() == "1"

SIGNAL_DECISION_LOG_FILE = "signal_decisions.log"
SIGNAL_DECISION_LOG_MAX_LINES = 2000


# ==============================================================
# SAFE HELPERS
# ==============================================================

def log(msg: str) -> None:
    print(msg)


def debug(msg: str) -> None:
    if DEBUG:
        print(msg)


def _decision_value(value: Any) -> str:
    """Compact one value for the lightweight Signal Desk decision log."""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list, tuple, set)):
        try:
            return json.dumps(value, separators=(",", ":"), default=str)[:500]
        except Exception:
            return str(value)[:500]
    text = str(value)
    return text.replace("\n", " ").replace("|", "/")[:500]


def decision_log(symbol: str, event: str, **details: Any) -> None:
    """
    Append one human-readable Signal Desk decision line.

    This is NOT a performance journal. It is a debugging/audit trail showing why
    a ticker became TRIGGER_READY, promoted to ACTIVE_SIGNAL, was suppressed, or
    was invalidated.
    """
    symbol = normalize_symbol(symbol) if "normalize_symbol" in globals() else str(symbol).upper()
    parts = [iso_now_et(), symbol or "-", event]
    for key, value in details.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={_decision_value(value)}")

    line = " | ".join(parts)

    try:
        with open(SIGNAL_DECISION_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:
        debug(f"Could not write {SIGNAL_DECISION_LOG_FILE}: {exc}")


def prune_decision_log(max_lines: int = SIGNAL_DECISION_LOG_MAX_LINES) -> None:
    """Keep the debug log useful without letting the repo file grow forever."""
    path = Path(SIGNAL_DECISION_LOG_FILE)
    if not path.exists():
        return

    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
    except Exception as exc:
        debug(f"Could not prune {SIGNAL_DECISION_LOG_FILE}: {exc}")


def safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        if isinstance(value, float) and pd.isna(value):
            return default
    except Exception:
        pass
    text = str(value)
    if text.lower() in {"nan", "none", "nat"}:
        return default
    return text


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and value.strip() in {"", "—", "nan", "None"}:
            return default
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pct(part: float, whole: float) -> float:
    if whole == 0:
        return 0.0
    return (part / whole) * 100.0


def pct_change(current: float, base: float) -> float:
    if base <= 0:
        return 0.0
    return ((current - base) / base) * 100.0


def normalize_status(status: Any) -> str:
    return safe_str(status, "WAIT").upper().replace(" ", "_")


def normalize_symbol(symbol: Any) -> str:
    return re.sub(r"[^A-Z0-9._-]", "", safe_str(symbol, "").upper().strip())


# ==============================================================
# EXPLICIT STATE TRANSITION GUARD
# ==============================================================

PROTECTED_STATE_TRANSITIONS = {
    "TRIGGER_READY": {
        "TRIGGER_READY",
        "TRIGGER_TOUCHED",
        "ACTIVE_SIGNAL",
        "INVALIDATED",
        "EXPIRED",
        "MISSED_WINDOW",
        "REJECTED_TRIGGER",
        "NEW_BASE_REQUIRED",
    },
    "TRIGGER_TOUCHED": {
        "TRIGGER_TOUCHED",
        "ACTIVE_SIGNAL",
        "INVALIDATED",
        "MISSED_WINDOW",
        "REJECTED_TRIGGER",
        "NEW_BASE_REQUIRED",
    },
    "ACTIVE_SIGNAL": {
        "ACTIVE_SIGNAL",
        "INVALIDATED",
        "TARGET_1_HIT",
        "TARGET_2_HIT",
        "STOPPED_OUT",
        "EXPIRED",
        "MISSED_WINDOW",
    },
}


def enforce_state_transition(
    symbol: str,
    old_status: Any,
    new_status: Any,
    reason: str = "",
) -> Tuple[bool, str, str]:
    """
    Hard guard for protected Signal Desk states.

    A protected setup must never silently fall back into WATCH. It must either
    advance through the protected lifecycle or exit with an explicit terminal
    reason. This prevents polluted outcomes like:

        TRIGGER_READY -> WATCH
        TRIGGER_TOUCHED -> WATCH
        ACTIVE_SIGNAL -> WATCH

    Returns:
        (allowed, final_status, log_message)
    """
    old = normalize_status(old_status)
    new = normalize_status(new_status)

    if not old:
        old = "WAIT"
    if not new:
        new = "WAIT"

    # Same-state refresh is always allowed.
    if old == new:
        return True, new, ""

    valid_next = PROTECTED_STATE_TRANSITIONS.get(old)
    if valid_next is None:
        return True, new, ""

    if new not in valid_next:
        msg = f"STATE_LOCK: Blocked {old} -> {new}"
        if reason:
            msg += f" | reason: {reason}"

        decision_log(
            normalize_symbol(symbol),
            "STATE_TRANSITION_BLOCKED",
            old_status=old,
            attempted_status=new,
            kept_status=old,
            reason=reason,
        )
        return False, old, msg

    return True, new, ""


def retain_locked_signal_after_block(
    existing: Dict[str, Any],
    metrics: Any,
    phase: str,
    block_message: str,
) -> Dict[str, Any]:
    """
    Keep a protected state visible when a bad transition is blocked.

    This updates live price/context, but preserves the locked lifecycle status,
    entry, stop, targets, confidence, and timestamps.
    """
    now_text = iso_now_et()
    out = dict(existing)

    out.update({
        "last_checked": now_text,
        "updated_at": now_text,
        "market_phase": phase,
        "state_lock_active": True,
        "state_lock_message": block_message,
        "entry_warning": combine_warning(
            out.get("entry_warning", ""),
            "Protected state retained. This setup cannot demote to WATCH; it must confirm, reject, expire, or invalidate.",
        ),
    })

    if getattr(metrics, "has_data", False):
        out.update({
            "price": round(safe_float(getattr(metrics, "price", 0), 0), 4),
            "price_source": safe_str(getattr(metrics, "price_source", ""), out.get("price_source", "")),
            "price_updated_at": safe_str(getattr(metrics, "price_updated_at", ""), out.get("price_updated_at", "")),
            "latest_bar_time": timestamp_to_et_iso(getattr(metrics, "latest_bar_time", None)) or out.get("latest_bar_time", ""),
            "bid": round(safe_float(getattr(metrics, "bid", 0), 0), 4),
            "ask": round(safe_float(getattr(metrics, "ask", 0), 0), 4),
            "quote_mid": round(safe_float(getattr(metrics, "quote_mid", 0), 0), 4),
            "quote_time": safe_str(getattr(metrics, "quote_time", ""), out.get("quote_time", "")),
            "trade_price": round(safe_float(getattr(metrics, "trade_price", 0), 0), 4),
            "trade_time": safe_str(getattr(metrics, "trade_time", ""), out.get("trade_time", "")),
            "vwap": round(safe_float(getattr(metrics, "vwap", 0), 0), 4),
            "hod": round(safe_float(getattr(metrics, "hod", 0), 0), 4),
            "vwap_dist_pct": round(safe_float(getattr(metrics, "vwap_dist_pct", 0), 0), 2),
            "hod_distance_pct": round(safe_float(getattr(metrics, "hod_distance_pct", 0), 0), 2),
            "ema9": round(safe_float(getattr(metrics, "ema9", 0), 0), 4),
            "price_above_ema9": bool(getattr(metrics, "price_above_ema9", False)),
            "ema9_status": safe_str(getattr(metrics, "ema9_status", ""), out.get("ema9_status", "")),
            "macd_value": round(safe_float(getattr(metrics, "macd_value", 0), 0), 4),
            "macd_signal": round(safe_float(getattr(metrics, "macd_signal", 0), 0), 4),
            "macd_histogram": round(safe_float(getattr(metrics, "macd_histogram", 0), 0), 4),
            "macd_status": safe_str(getattr(metrics, "macd_status", ""), out.get("macd_status", "")),
            "momentum_status": safe_str(getattr(metrics, "momentum_status", ""), out.get("momentum_status", "")),
        })

    reason = safe_str(out.get("reason"), "")
    if "State lock retained" not in reason:
        out["reason"] = f"State lock retained. {reason}".strip()

    return out


def apply_state_transition_guard(
    symbol: str,
    existing: Dict[str, Any],
    candidate: Dict[str, Any],
    metrics: Any,
    phase: str,
    reason: str = "",
) -> Dict[str, Any]:
    """
    Apply hard transition rules to any candidate state before it is written.

    If a protected state tries to demote to WATCH or WAIT, keep the protected
    state and log the blocked transition.
    """
    if not existing or not candidate:
        return candidate

    old_status = normalize_status(existing.get("signal_status"))
    new_status = normalize_status(candidate.get("signal_status"))

    allowed, final_status, message = enforce_state_transition(
        symbol,
        old_status,
        new_status,
        reason=reason,
    )

    if allowed:
        return candidate

    return retain_locked_signal_after_block(existing, metrics, phase, message)


def parse_iso_dt(text: Any) -> Optional[datetime]:
    s = safe_str(text, "").strip()
    if not s:
        return None

    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass

    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def ny_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo("America/New_York"))
    return datetime.now()


def iso_now_et() -> str:
    return ny_now().isoformat(timespec="seconds")


def minutes_since(dt_text: Any, now: Optional[datetime] = None) -> Optional[float]:
    dt = parse_iso_dt(dt_text)
    if not dt:
        return None

    if now is None:
        now = ny_now()

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    try:
        if ZoneInfo:
            dt = dt.astimezone(ZoneInfo("America/New_York"))
        return (now - dt).total_seconds() / 60.0
    except Exception:
        return None


# ==============================================================
# TIME HELPERS
# ==============================================================

def get_market_phase(now: Optional[datetime] = None) -> str:
    """
    Returns:
      CLOSED
      PREMARKET
      OPENING_BLACKOUT
      VALID_MORNING
      LUNCH_BLACKOUT
      VALID_AFTERNOON
      FINAL_BLACKOUT
      AFTERHOURS
    """
    now = now or ny_now()

    if now.weekday() >= 5:
        return "CLOSED"

    t = now.time()

    if t < dtime(4, 0):
        return "CLOSED"
    if t < dtime(9, 30):
        return "PREMARKET"
    if t < dtime(9, 45):
        return "OPENING_BLACKOUT"
    if t < dtime(11, 30):
        return "VALID_MORNING"
    if t < dtime(13, 30):
        return "LUNCH_BLACKOUT"
    if t < dtime(15, 45):
        return "VALID_AFTERNOON"
    if t < dtime(16, 0):
        return "FINAL_BLACKOUT"
    if t < dtime(20, 0):
        return "AFTERHOURS"
    return "CLOSED"



def is_late_day(now: Optional[datetime] = None) -> bool:
    """
    Late-day setups need stronger proof because breakouts have less time
    and failed breakouts are more common after 2:30 PM ET.
    """
    now = now or ny_now()
    return now.time() >= LATE_DAY_READY_START and now.time() < dtime(16, 0)


def ready_confidence_required(phase: str) -> float:
    if phase == "VALID_AFTERNOON" and is_late_day():
        return MIN_CONF_READY_LATE_DAY
    return MIN_CONF_READY


def volume_fade_label(metrics: "IntradayMetrics") -> str:
    label = safe_str(getattr(metrics, "volume_reference_label", ""), "adaptive reference")
    ref = safe_float(getattr(metrics, "volume_reference_avg_5m", 0), 0)
    if ref > 0:
        return f"Volume fading vs {label} ({metrics.avg_volume_5:.0f} < {VOLUME_FADE_RATIO:.0%} of {ref:.0f})"
    return f"Volume fading vs {label}"


def late_day_volume_confirmed(metrics: "IntradayMetrics") -> bool:
    if not is_late_day():
        return True

    # After 2:30 PM, stable volume is not enough for a trigger-ready signal.
    # We require recent expansion and no severe fade versus the adaptive reference.
    return (
        metrics.recent_volume_expanding
        and not metrics.volume_fading_vs_morning
        and (
            metrics.volume_reference_avg_5m <= 0
            or metrics.avg_volume_5 >= MIN_LATE_DAY_MORNING_VOL_RATIO * metrics.volume_reference_avg_5m
        )
    )


def setup_grace_minutes(setup_type: str) -> float:
    setup = safe_str(setup_type, "").upper()
    if "HOD" in setup:
        return HOD_BREAKOUT_GRACE_MINUTES
    if "RECLAIM" in setup:
        return VWAP_RECLAIM_GRACE_MINUTES
    if "VWAP_PULLBACK" in setup or "PULLBACK" in setup:
        return VWAP_PULLBACK_GRACE_MINUTES
    if "BASE" in setup or "SQUEEZE" in setup or "FLAG" in setup:
        return BASE_SQUEEZE_GRACE_MINUTES
    return VWAP_PULLBACK_GRACE_MINUTES


def within_setup_grace(signal: Dict[str, Any]) -> bool:
    """
    Return True when a protected setup is still inside its setup-specific
    grace window. During grace, soft failures such as EMA9/MACD/volume cooling
    should warn but not immediately invalidate. Hard failures like VWAP/support
    loss still invalidate.
    """
    start = (
        signal.get("trigger_touched_at")
        or signal.get("ready_since")
        or signal.get("detected_at")
        or signal.get("updated_at")
    )
    age = minutes_since(start)
    if age is None:
        return False
    return age <= setup_grace_minutes(safe_str(signal.get("setup_type"), ""))
def is_market_open_phase(phase: str) -> bool:
    return phase in {
        "OPENING_BLACKOUT",
        "VALID_MORNING",
        "LUNCH_BLACKOUT",
        "VALID_AFTERNOON",
        "FINAL_BLACKOUT",
    }


def is_valid_signal_phase(phase: str) -> bool:
    return phase in {"VALID_MORNING", "VALID_AFTERNOON"}


def is_lunch_blackout_phase(phase: str) -> bool:
    return safe_str(phase).upper() == LUNCH_CAUTION_PHASE


def is_lunch_trigger_ready_allowed_setup(setup_type: str) -> bool:
    """
    Lunch can surface TRIGGER_READY only for the user's core manual setups:
    VWAP reclaim, reclaim pullback holding, and VWAP pullback continuation.

    It does not create automatic ACTIVE_SIGNAL during lunch.
    """
    text = safe_str(setup_type).upper()
    return any(
        token in text
        for token in (
            "VWAP_RECLAIM",
            "RECLAIM_PULLBACK",
            "VWAP_PULLBACK",
        )
    )


def combine_warning(existing: str, addition: str) -> str:
    existing = safe_str(existing).strip()
    addition = safe_str(addition).strip()
    if existing and addition and addition not in existing:
        return f"{existing} | {addition}"
    return existing or addition


def apply_lunch_caution_fields(signal: Dict[str, Any], phase: str) -> Dict[str, Any]:
    """
    Mark a TRIGGER_READY signal created/fired during lunch as manual-only.

    The status remains TRIGGER_READY so it is visible in the Ready column, but
    actionability is LUNCH_CAUTION and the dashboard shows a red warning.
    """
    if not is_lunch_blackout_phase(phase):
        return signal

    out = dict(signal)
    reason = safe_str(out.get("reason"), "")
    if LUNCH_CAUTION_WARNING not in reason:
        reason = f"{reason} {LUNCH_CAUTION_WARNING}".strip()

    out.update({
        "signal_status": "TRIGGER_READY",
        "actionable": False,
        "actionability": "LUNCH_CAUTION",
        "lunch_caution": True,
        "lunch_blackout_ready": True,
        "suppression_reason": "LUNCH_BLACKOUT_TRIGGER_READY_ONLY",
        "entry_warning": LUNCH_CAUTION_WARNING,
        "event_risk_warning": combine_warning(out.get("event_risk_warning", ""), LUNCH_CAUTION_WARNING),
        "reason": reason,
    })
    return out


def session_date_str(now: Optional[datetime] = None) -> str:
    now = now or ny_now()
    return now.date().isoformat()


def session_start_end_utc(now: Optional[datetime] = None) -> Tuple[str, str]:
    """
    Regular-session window for signal logic.
    Uses 9:30 AM ET to now.
    """
    now = now or ny_now()

    if ZoneInfo:
        start_ny = datetime(now.year, now.month, now.day, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        end_ny = now
        start_utc = start_ny.astimezone(timezone.utc)
        end_utc = end_ny.astimezone(timezone.utc)
    else:
        start_utc = datetime.utcnow().replace(hour=13, minute=30, second=0, microsecond=0)
        end_utc = datetime.utcnow().replace(tzinfo=timezone.utc)

    return (
        start_utc.isoformat().replace("+00:00", "Z"),
        end_utc.isoformat().replace("+00:00", "Z"),
    )


# ==============================================================
# FILE LOADERS
# ==============================================================

def load_json(path: str, default: Any) -> Any:
    p = Path(path)
    if not p.exists():
        return default

    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"  ⚠ Failed to load {path}: {e}")
        return default


def write_json(path: str, data: Any) -> None:
    with Path(path).open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_csv(path: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []

    try:
        df = pd.read_csv(p).fillna("")
        if limit is not None:
            df = df.head(limit)
        return df.to_dict("records")
    except Exception as e:
        log(f"  ⚠ Failed to load {path}: {e}")
        return []


def _smart_money_age_seconds(payload: Dict[str, Any]) -> Optional[float]:
    """
    Return smart_money_scores.json age in seconds using metadata.generated_at_et.
    Falls back to the file modification time if the timestamp is unavailable.
    """
    meta = payload.get("metadata", {}) if isinstance(payload, dict) else {}
    generated_at = meta.get("generated_at_et") or meta.get("generated_at") or meta.get("updated_at_et")
    dt = parse_iso_dt(generated_at)
    if dt is not None:
        now = ny_now()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now.tzinfo)
        elif now.tzinfo is not None:
            dt = dt.astimezone(now.tzinfo)
        return max(0.0, (now - dt).total_seconds())

    try:
        return max(0.0, time.time() - Path(SMART_MONEY_FILE).stat().st_mtime)
    except Exception:
        return None


def load_smart_money_scores() -> Dict[str, Dict[str, Any]]:
    """
    Load optional Phase 1 Smart Money Proxy scores.

    Expected format from smart_money_bars_proxy.py:
      {
        "metadata": {...},
        "symbols": {
          "AAPL": {
            "raw_score": 82,
            "score_adjustment": 3,
            "label": "Moderate Proxy",
            "bias": "BULLISH",
            "signals": [...]
          }
        }
      }

    If missing, stale, or malformed, return {} so the engine behaves exactly
    like the original scanner.
    """
    path = Path(SMART_MONEY_FILE)
    if not path.exists():
        return {}

    payload = load_json(SMART_MONEY_FILE, {})
    if not isinstance(payload, dict):
        log(f"  ⚠ Smart money file malformed: {SMART_MONEY_FILE}")
        return {}

    age = _smart_money_age_seconds(payload)
    if age is not None and age > SMART_MONEY_MAX_AGE_SECONDS:
        log(
            f"  ⚠ Smart money scores stale: age={age:.0f}s "
            f"> max={SMART_MONEY_MAX_AGE_SECONDS}s; skipping adjustment"
        )
        return {}

    raw_symbols = payload.get("symbols", payload)
    if not isinstance(raw_symbols, dict):
        log(f"  ⚠ Smart money symbols payload malformed: {SMART_MONEY_FILE}")
        return {}

    scores: Dict[str, Dict[str, Any]] = {}
    for symbol, record in raw_symbols.items():
        sym = normalize_symbol(symbol)
        if not sym or not isinstance(record, dict):
            continue
        scores[sym] = record

    if scores:
        log(f"  Smart money scores loaded: {len(scores)} symbols")
    return scores


def _smart_money_signals_text(record: Dict[str, Any]) -> str:
    signals = record.get("signals", [])
    if isinstance(signals, list):
        return " | ".join(safe_str(x, "") for x in signals if safe_str(x, ""))
    return safe_str(signals, "")


def apply_smart_money_to_row(row: Dict[str, Any], smart_scores: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Attach smart-money fields and adjust row['score'] in memory only.
    Original CSV files are never rewritten.
    """
    row = dict(row)
    sym = normalize_symbol(row.get("symbol"))
    base_score = safe_float(row.get("score"), 0.0)
    record = smart_scores.get(sym, {}) if sym else {}

    raw_score = safe_float(record.get("raw_score"), 0.0) if record else 0.0
    adjustment = safe_float(record.get("score_adjustment"), 0.0) if record else 0.0
    adjustment = clamp(adjustment, -SMART_MONEY_ADJUSTMENT_CAP, SMART_MONEY_ADJUSTMENT_CAP)

    row["symbol"] = sym
    row["base_scanner_score"] = base_score
    row["smart_money_score"] = raw_score
    row["smart_money_adjustment"] = adjustment
    row["smart_money_label"] = safe_str(record.get("label"), "") if record else ""
    row["smart_money_bias"] = safe_str(record.get("bias"), "") if record else ""
    row["smart_money_signals"] = _smart_money_signals_text(record) if record else ""
    row["smart_money_volume_ratio"] = safe_float(record.get("volume_ratio"), 0.0) if record else 0.0
    row["smart_money_vwap_distance_pct"] = safe_float(record.get("vwap_distance_pct"), 0.0) if record else 0.0
    row["smart_money_vwap_touch_count"] = safe_int(record.get("vwap_touch_count"), 0) if record else 0
    row["smart_money_range_ratio"] = safe_float(record.get("range_ratio"), 0.0) if record else 0.0
    row["score"] = round(clamp(base_score + adjustment, 0, 100), 2)

    return row


def load_focus_candidates() -> Dict[str, Dict[str, Any]]:
    """
    Load the focus universe and apply optional Smart Money Proxy adjustments
    in memory only.

    Preferred path:
      1. Read elite_watchlist_raw.csv.
      2. Apply smart_money_scores.json adjustments.
      3. Re-rank POTENTIAL_MOVER and ACTIVE_MOMENTUM buckets in memory.
      4. Select Top 12 Potential + Top 8 Active.

    Fallback:
      If the raw file is missing/empty or has no usable decision buckets, read
      potential_movers.csv and active_momentum.csv exactly like the original
      engine, then apply smart-money adjustments inside those existing buckets.

    Original scanner CSV files are never rewritten.
    """
    focus: Dict[str, Dict[str, Any]] = {}
    smart_scores = load_smart_money_scores()

    def bucket_kind(row: Dict[str, Any]) -> str:
        bucket = safe_str(row.get("setup_bucket"), "").upper()
        if "POTENTIAL" in bucket:
            return "POTENTIAL"
        if "ACTIVE" in bucket:
            return "ACTIVE"
        return ""

    def prepare_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        prepared: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw in rows:
            row = apply_smart_money_to_row(raw, smart_scores)
            sym = normalize_symbol(row.get("symbol"))
            if not sym or sym in seen:
                continue
            row["symbol"] = sym
            prepared.append(row)
            seen.add(sym)
        return prepared

    used_raw_universe = False

    raw_rows = load_csv(RAW_WATCHLIST_FILE, None)
    if raw_rows:
        prepared = prepare_rows(raw_rows)
        potential = [r for r in prepared if bucket_kind(r) == "POTENTIAL"]
        active = [r for r in prepared if bucket_kind(r) == "ACTIVE"]

        if potential or active:
            used_raw_universe = True
            potential = sorted(potential, key=lambda r: safe_float(r.get("score"), 0), reverse=True)[:POTENTIAL_LIMIT]
            active = sorted(active, key=lambda r: safe_float(r.get("score"), 0), reverse=True)[:ACTIVE_LIMIT]

            log(
                f"  Focus ranked from {RAW_WATCHLIST_FILE}: "
                f"potential={len(potential)}, active={len(active)}"
            )
        else:
            potential = []
            active = []
    else:
        potential = []
        active = []

    if not used_raw_universe:
        # Original behavior fallback: use already-focused scanner CSVs, but still
        # apply smart-money adjustments and re-rank within each existing bucket.
        potential = prepare_rows(load_csv(POTENTIAL_FILE, None))
        active = prepare_rows(load_csv(ACTIVE_FILE, None))

        potential = sorted(potential, key=lambda r: safe_float(r.get("score"), 0), reverse=True)[:POTENTIAL_LIMIT]
        active = sorted(active, key=lambda r: safe_float(r.get("score"), 0), reverse=True)[:ACTIVE_LIMIT]

        log(
            f"  Focus ranked from focused CSV fallback: "
            f"potential={len(potential)}, active={len(active)}"
        )

    for idx, row in enumerate(potential):
        sym = normalize_symbol(row.get("symbol"))
        if not sym:
            continue
        row = dict(row)
        row["symbol"] = sym
        row["signal_source_bucket"] = "POTENTIAL"
        row["signal_rank"] = idx + 1
        focus[sym] = row

    for idx, row in enumerate(active):
        sym = normalize_symbol(row.get("symbol"))
        if not sym:
            continue

        if sym in focus:
            focus[sym]["signal_source_bucket"] = "POTENTIAL+ACTIVE"
            focus[sym]["active_signal_rank"] = idx + 1

            # Preserve the strongest adjusted record while keeping the duplicate
            # bucket marker visible.
            if safe_float(row.get("score"), 0) > safe_float(focus[sym].get("score"), 0):
                for key, value in row.items():
                    focus[sym][key] = value
                focus[sym]["signal_source_bucket"] = "POTENTIAL+ACTIVE"
                focus[sym]["active_signal_rank"] = idx + 1
            continue

        row = dict(row)
        row["symbol"] = sym
        row["signal_source_bucket"] = "ACTIVE"
        row["signal_rank"] = idx + 1
        focus[sym] = row

    return focus


def load_signal_state() -> Dict[str, Dict[str, Any]]:
    data = load_json(SIGNAL_STATE_FILE, {})
    if isinstance(data, dict):
        if isinstance(data.get("signals"), dict):
            return data.get("signals", {})
        return data
    return {}


def write_signal_state(state: Dict[str, Dict[str, Any]], meta: Dict[str, Any]) -> None:
    write_json(
        SIGNAL_STATE_FILE,
        {
            "generated_at_et": iso_now_et(),
            "meta": meta,
            "signals": state,
        },
    )


def append_suppressed_signal(row: Dict[str, Any]) -> None:
    path = Path(SUPPRESSED_SIGNALS_FILE)
    exists = path.exists()

    fieldnames = [
        "timestamp_et",
        "symbol",
        "setup_type",
        "scanner_score",
        "live_signal_score",
        "confidence",
        "entry_trigger",
        "stop_loss",
        "target_1",
        "target_2",
        "reward_risk",
        "suppression_reason",
        "price_at_trigger",
        "vwap",
        "hod_distance_pct",
        "vwap_distance_pct",
        "sector_status",
        "market_regime",
        "notes",
    ]

    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not exists:
            writer.writeheader()

        writer.writerow({k: row.get(k, "") for k in fieldnames})


# ==============================================================
# SIGNAL OUTCOME TRACKING
# ==============================================================

OUTCOME_FINAL_STATUSES = {
    "T2_HIT",
    "T1_HIT",
    "STOP_HIT",
    "INVALIDATED_BEFORE_ENTRY",
    "INVALIDATED_AFTER_ENTRY",
    "WATCH_REMOVED",
    "MISSED_WINDOW",
    "EXPIRED",
}

OUTCOME_FIELDNAMES = [
    "signal_id",
    "session_date",
    "symbol",
    "company_name",
    "setup_type",
    "setup_label",
    "setup_bucket",
    "strategy_version",
    "first_seen_time",
    "watch_time",
    "trigger_ready_time",
    "trigger_touched_time",
    "active_time",
    "invalidated_time",
    "completed_time",
    "last_checked",
    "latest_signal_status",
    "outcome_status",
    "outcome_detail",
    "invalidation_reason",
    "invalidation_category",
    "entry",
    "stop",
    "target_1",
    "target_2",
    "reward_risk",
    "confidence",
    "scanner_score",
    "base_scanner_score",
    "smart_money_score",
    "smart_money_adjustment",
    "smart_money_label",
    "smart_money_bias",
    "smart_money_signals",
    "live_signal_score",
    "market_phase",
    "event_context",
    "earnings_reaction_trade",
    "lunch_caution",
    "current_price",
    "vwap",
    "ema9",
    "hod",
    "highest_price_after_ready",
    "lowest_price_after_ready",
    "highest_price_after_active",
    "lowest_price_after_active",
    "hit_entry",
    "hit_t1",
    "hit_t2",
    "hit_stop",
    "entry_touched_before_active",
    "target_1_hit_before_active",
    "target_2_hit_before_active",
    "success_without_active",
    "max_profit_pct",
    "max_loss_pct",
    "best_r_multiple",
    "final_r_multiple",
    "notes",
]


def _csv_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return safe_str(value, "").strip().lower() in {"1", "true", "yes", "y"}


def _outcome_float(row: Dict[str, Any], key: str, default: float = 0.0) -> float:
    return safe_float(row.get(key), default)


def _outcome_status_is_final(status: Any) -> bool:
    return safe_str(status, "").upper() in OUTCOME_FINAL_STATUSES


def load_signal_outcomes() -> List[Dict[str, Any]]:
    path = Path(SIGNAL_OUTCOMES_FILE)
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except Exception as exc:
        decision_log("-", "OUTCOME_LOAD_ERROR", error=str(exc))
        return []


def write_signal_outcomes(rows: List[Dict[str, Any]]) -> None:
    path = Path(SIGNAL_OUTCOMES_FILE)
    tmp = path.with_suffix(".csv.tmp")

    cleaned_rows = []
    for row in rows:
        cleaned_rows.append({key: row.get(key, "") for key in OUTCOME_FIELDNAMES})

    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTCOME_FIELDNAMES)
        writer.writeheader()
        writer.writerows(cleaned_rows)

    tmp.replace(path)


def _signal_time_for_id(signal: Dict[str, Any]) -> str:
    for key in ["ready_since", "triggered_at", "detected_at", "invalidated_at", "updated_at", "last_checked"]:
        value = safe_str(signal.get(key), "")
        if value:
            dt = parse_iso_dt(value)
            if dt is not None:
                return dt.astimezone(ZoneInfo("America/New_York")).strftime("%H%M") if ZoneInfo else dt.strftime("%H%M")
            return re.sub(r"[^0-9]", "", value)[-4:] or "0000"
    return ny_now().strftime("%H%M")


def make_signal_id(signal: Dict[str, Any], existing_rows: Optional[List[Dict[str, Any]]] = None) -> str:
    symbol = normalize_symbol(signal.get("symbol"))
    setup = safe_str(signal.get("setup_type"), "SETUP").upper().replace(" ", "_")
    sess = safe_str(signal.get("session_date"), session_date_str())
    base_time = _signal_time_for_id(signal)
    base = f"{sess}_{symbol}_{base_time}_{setup}"

    if not existing_rows:
        return base

    existing_ids = {safe_str(r.get("signal_id"), "") for r in existing_rows}
    if base not in existing_ids:
        return base

    # If the same base exists but is still open, reuse it.
    for row in existing_rows:
        if safe_str(row.get("signal_id"), "") == base and not _outcome_status_is_final(row.get("outcome_status")):
            return base

    # Otherwise create a new repeat suffix.
    n = 2
    while f"{base}_{n}" in existing_ids:
        n += 1
    return f"{base}_{n}"


def find_open_outcome_row(symbol: str, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    symbol = normalize_symbol(symbol)
    candidates = [
        row for row in rows
        if normalize_symbol(row.get("symbol")) == symbol
        and not _outcome_status_is_final(row.get("outcome_status"))
    ]

    if not candidates:
        return None

    def sort_key(row: Dict[str, Any]) -> str:
        return safe_str(row.get("last_checked") or row.get("first_seen_time"), "")

    candidates.sort(key=sort_key, reverse=True)
    return candidates[0]


def initial_outcome_row(signal: Dict[str, Any], existing_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    symbol = normalize_symbol(signal.get("symbol"))
    signal_id = safe_str(signal.get("signal_id"), "") or make_signal_id(signal, existing_rows)
    now_text = iso_now_et()
    first_seen = (
        safe_str(signal.get("detected_at"), "")
        or safe_str(signal.get("ready_since"), "")
        or safe_str(signal.get("triggered_at"), "")
        or safe_str(signal.get("updated_at"), "")
        or now_text
    )

    return {
        "signal_id": signal_id,
        "session_date": safe_str(signal.get("session_date"), session_date_str()),
        "symbol": symbol,
        "company_name": safe_str(signal.get("company_name"), ""),
        "setup_type": safe_str(signal.get("setup_type"), ""),
        "setup_label": safe_str(signal.get("setup_label"), ""),
        "setup_bucket": safe_str(signal.get("setup_bucket"), ""),
        "strategy_version": SIGNAL_ENGINE_STRATEGY_VERSION,
        "first_seen_time": first_seen,
        "watch_time": "",
        "trigger_ready_time": "",
        "trigger_touched_time": "",
        "active_time": "",
        "invalidated_time": "",
        "completed_time": "",
        "last_checked": now_text,
        "latest_signal_status": "",
        "outcome_status": "OPEN",
        "outcome_detail": "",
        "invalidation_reason": "",
        "invalidation_category": "",
        "entry": "",
        "stop": "",
        "target_1": "",
        "target_2": "",
        "reward_risk": "",
        "confidence": "",
        "scanner_score": "",
        "live_signal_score": "",
        "market_phase": "",
        "event_context": "",
        "earnings_reaction_trade": "",
        "lunch_caution": "",
        "current_price": "",
        "vwap": "",
        "ema9": "",
        "hod": "",
        "highest_price_after_ready": "",
        "lowest_price_after_ready": "",
        "highest_price_after_active": "",
        "lowest_price_after_active": "",
        "hit_entry": "",
        "hit_t1": "",
        "hit_t2": "",
        "hit_stop": "",
        "entry_touched_before_active": "",
        "target_1_hit_before_active": "",
        "target_2_hit_before_active": "",
        "success_without_active": "",
        "max_profit_pct": "",
        "max_loss_pct": "",
        "best_r_multiple": "",
        "final_r_multiple": "",
        "notes": "",
    }


def update_one_outcome(row: Dict[str, Any], signal: Dict[str, Any]) -> Dict[str, Any]:
    now_text = iso_now_et()
    status = normalize_status(signal.get("signal_status"))
    price = safe_float(signal.get("price"), 0.0)
    entry = safe_float(signal.get("entry_trigger"), _outcome_float(row, "entry", 0.0))
    stop = safe_float(signal.get("stop_loss"), _outcome_float(row, "stop", 0.0))
    target_1 = safe_float(signal.get("target_1"), _outcome_float(row, "target_1", 0.0))
    target_2 = safe_float(signal.get("target_2"), _outcome_float(row, "target_2", 0.0))
    risk = entry - stop if entry > 0 and stop > 0 and entry > stop else 0.0

    if signal.get("signal_id"):
        row["signal_id"] = safe_str(signal.get("signal_id"), row.get("signal_id", ""))

    row.update({
        "session_date": safe_str(signal.get("session_date"), row.get("session_date", session_date_str())),
        "symbol": normalize_symbol(signal.get("symbol") or row.get("symbol")),
        "company_name": safe_str(signal.get("company_name"), row.get("company_name", "")),
        "setup_type": safe_str(signal.get("setup_type"), row.get("setup_type", "")),
        "setup_label": safe_str(signal.get("setup_label"), row.get("setup_label", "")),
        "setup_bucket": safe_str(signal.get("setup_bucket"), row.get("setup_bucket", "")),
        "strategy_version": SIGNAL_ENGINE_STRATEGY_VERSION,
        "last_checked": now_text,
        "latest_signal_status": status,
        "entry": round(entry, 4) if entry > 0 else row.get("entry", ""),
        "stop": round(stop, 4) if stop > 0 else row.get("stop", ""),
        "target_1": round(target_1, 4) if target_1 > 0 else row.get("target_1", ""),
        "target_2": round(target_2, 4) if target_2 > 0 else row.get("target_2", ""),
        "reward_risk": safe_float(signal.get("reward_risk"), _outcome_float(row, "reward_risk", 0.0)),
        "confidence": safe_float(signal.get("confidence"), _outcome_float(row, "confidence", 0.0)),
        "scanner_score": safe_float(signal.get("scanner_score"), _outcome_float(row, "scanner_score", 0.0)),
        "base_scanner_score": safe_float(signal.get("base_scanner_score"), _outcome_float(row, "base_scanner_score", 0.0)),
        "smart_money_score": safe_float(signal.get("smart_money_score"), _outcome_float(row, "smart_money_score", 0.0)),
        "smart_money_adjustment": safe_float(signal.get("smart_money_adjustment"), _outcome_float(row, "smart_money_adjustment", 0.0)),
        "smart_money_label": safe_str(signal.get("smart_money_label"), row.get("smart_money_label", "")),
        "smart_money_bias": safe_str(signal.get("smart_money_bias"), row.get("smart_money_bias", "")),
        "smart_money_signals": safe_str(signal.get("smart_money_signals"), row.get("smart_money_signals", "")),
        "live_signal_score": safe_float(signal.get("live_signal_score"), _outcome_float(row, "live_signal_score", 0.0)),
        "market_phase": safe_str(signal.get("market_phase"), ""),
        "event_context": safe_str(signal.get("event_context"), row.get("event_context", "")),
        "earnings_reaction_trade": bool(signal.get("earnings_reaction_trade")),
        "lunch_caution": bool(signal.get("lunch_caution")) or safe_str(signal.get("actionability"), "").upper() == "LUNCH_CAUTION",
        "current_price": round(price, 4) if price > 0 else row.get("current_price", ""),
        "price_source": safe_str(signal.get("price_source"), row.get("price_source", "")),
        "price_updated_at": safe_str(signal.get("price_updated_at"), row.get("price_updated_at", "")),
        "latest_bar_time": safe_str(signal.get("latest_bar_time"), row.get("latest_bar_time", "")),
        "quote_time": safe_str(signal.get("quote_time"), row.get("quote_time", "")),
        "trade_time": safe_str(signal.get("trade_time"), row.get("trade_time", "")),
        "vwap": safe_float(signal.get("vwap"), _outcome_float(row, "vwap", 0.0)),
        "ema9": safe_float(signal.get("ema9"), _outcome_float(row, "ema9", 0.0)),
        "hod": safe_float(signal.get("hod"), _outcome_float(row, "hod", 0.0)),
        "invalidation_reason": safe_str(signal.get("invalidation_reason"), row.get("invalidation_reason", "")),
        "invalidation_category": safe_str(signal.get("invalidation_category"), row.get("invalidation_category", "")),
    })

    if status == "WATCH" and not row.get("watch_time"):
        row["watch_time"] = safe_str(signal.get("detected_at") or signal.get("updated_at") or now_text)
    if status in {"TRIGGER_READY", "TRIGGER_TOUCHED", "ACTIVE_SIGNAL", "INVALIDATED"} and not row.get("trigger_ready_time"):
        ready_time = safe_str(signal.get("ready_since") or signal.get("detected_at") or signal.get("updated_at") or now_text)
        row["trigger_ready_time"] = ready_time
    if status == "TRIGGER_TOUCHED" and not row.get("trigger_touched_time"):
        row["trigger_touched_time"] = safe_str(signal.get("trigger_touched_at") or signal.get("updated_at") or now_text)
    if status == "ACTIVE_SIGNAL" and not row.get("active_time"):
        row["active_time"] = safe_str(signal.get("triggered_at") or signal.get("updated_at") or now_text)
    if status == "INVALIDATED":
        row["invalidated_time"] = safe_str(signal.get("invalidated_at") or signal.get("updated_at") or now_text)

    # Track high/low after Trigger Ready and after Active using every 60-second Signal Desk check.
    if price > 0 and row.get("trigger_ready_time"):
        prior_high_ready = _outcome_float(row, "highest_price_after_ready", price)
        prior_low_ready = _outcome_float(row, "lowest_price_after_ready", price)
        row["highest_price_after_ready"] = round(max(prior_high_ready, price), 4)
        row["lowest_price_after_ready"] = round(min(prior_low_ready, price), 4)

    if price > 0 and (row.get("active_time") or status == "ACTIVE_SIGNAL"):
        prior_high_active = _outcome_float(row, "highest_price_after_active", price)
        prior_low_active = _outcome_float(row, "lowest_price_after_active", price)
        row["highest_price_after_active"] = round(max(prior_high_active, price), 4)
        row["lowest_price_after_active"] = round(min(prior_low_active, price), 4)

    highest_ready = _outcome_float(row, "highest_price_after_ready", 0.0)
    lowest_ready = _outcome_float(row, "lowest_price_after_ready", 0.0)
    highest_active = _outcome_float(row, "highest_price_after_active", 0.0)
    lowest_active = _outcome_float(row, "lowest_price_after_active", 0.0)

    high_candidates = [v for v in [highest_ready, highest_active] if v > 0]
    low_candidates = [v for v in [lowest_ready, lowest_active] if v > 0]
    high_ref = max(high_candidates) if high_candidates else 0.0
    low_ref = min(low_candidates) if low_candidates else 0.0

    had_active_before_now = bool(row.get("active_time")) or status == "ACTIVE_SIGNAL"

    hit_entry = _csv_bool(row.get("hit_entry")) or (entry > 0 and high_ref >= entry)
    hit_t1 = _csv_bool(row.get("hit_t1")) or (target_1 > 0 and high_ref >= target_1)
    hit_t2 = _csv_bool(row.get("hit_t2")) or (target_2 > 0 and high_ref >= target_2)
    hit_stop = _csv_bool(row.get("hit_stop")) or (hit_entry and stop > 0 and low_ref > 0 and low_ref <= stop)

    # Ready-only success:
    # If a setup becomes TRIGGER_READY and price reaches Entry/T1/T2 before the
    # engine promotes it to ACTIVE_SIGNAL, count the signal result anyway. This
    # measures whether the scanner setup worked, not whether the user entered.
    entry_touched_before_active = _csv_bool(row.get("entry_touched_before_active")) or (
        bool(row.get("trigger_ready_time")) and not had_active_before_now and entry > 0 and high_ref >= entry
    )
    target_1_hit_before_active = _csv_bool(row.get("target_1_hit_before_active")) or (
        bool(row.get("trigger_ready_time")) and not had_active_before_now and target_1 > 0 and high_ref >= target_1
    )
    target_2_hit_before_active = _csv_bool(row.get("target_2_hit_before_active")) or (
        bool(row.get("trigger_ready_time")) and not had_active_before_now and target_2 > 0 and high_ref >= target_2
    )
    success_without_active = _csv_bool(row.get("success_without_active")) or target_1_hit_before_active or target_2_hit_before_active

    row["hit_entry"] = hit_entry
    row["hit_t1"] = hit_t1
    row["hit_t2"] = hit_t2
    row["hit_stop"] = hit_stop
    row["entry_touched_before_active"] = entry_touched_before_active
    row["target_1_hit_before_active"] = target_1_hit_before_active
    row["target_2_hit_before_active"] = target_2_hit_before_active
    row["success_without_active"] = success_without_active

    if entry > 0 and high_ref > 0:
        row["max_profit_pct"] = round(pct_change(high_ref, entry), 2)
    if entry > 0 and low_ref > 0:
        row["max_loss_pct"] = round((entry - low_ref) / entry * 100.0, 2)

    best_r = 0.0
    if risk > 0 and high_ref > 0:
        best_r = max(0.0, (high_ref - entry) / risk)
        row["best_r_multiple"] = round(best_r, 2)

    # Outcome priority.
    outcome_status = safe_str(row.get("outcome_status"), "OPEN").upper() or "OPEN"
    outcome_detail = safe_str(row.get("outcome_detail"), "")

    if hit_t2:
        outcome_status = "T2_HIT"
        outcome_detail = (
            "Target 2 hit from Trigger Ready before Active Signal."
            if target_2_hit_before_active
            else "Target 2 hit after signal tracking began."
        )
        row["completed_time"] = row.get("completed_time") or now_text
        row["final_r_multiple"] = round((target_2 - entry) / risk, 2) if risk > 0 else ""
    elif hit_t1:
        outcome_status = "T1_HIT"
        outcome_detail = (
            "Target 1 hit from Trigger Ready before Active Signal."
            if target_1_hit_before_active
            else "Target 1 hit after signal tracking began."
        )
        row["completed_time"] = row.get("completed_time") or now_text
        row["final_r_multiple"] = round((target_1 - entry) / risk, 2) if risk > 0 else ""
    elif hit_stop:
        outcome_status = "STOP_HIT"
        outcome_detail = "Stop level touched after entry trigger was considered hit."
        row["completed_time"] = row.get("completed_time") or now_text
        row["final_r_multiple"] = -1.0
    elif status == "INVALIDATED":
        if hit_entry:
            outcome_status = "INVALIDATED_AFTER_ENTRY"
            outcome_detail = safe_str(signal.get("invalidation_reason"), "Signal invalidated after entry trigger was touched.")
            row["final_r_multiple"] = round(best_r, 2) if best_r > 0 else 0.0
        else:
            outcome_status = "INVALIDATED_BEFORE_ENTRY"
            outcome_detail = safe_str(signal.get("invalidation_reason"), "Signal invalidated before entry trigger.")
            row["final_r_multiple"] = 0.0
        row["completed_time"] = row.get("completed_time") or row.get("invalidated_time") or now_text
    elif status == "ACTIVE_SIGNAL":
        outcome_status = "ACTIVE_SIGNAL"
        outcome_detail = "Active signal being monitored."
    elif status == "TRIGGER_TOUCHED":
        outcome_status = "TRIGGER_TOUCHED"
        outcome_detail = "Trigger touched; waiting for hold/volume confirmation before Active Signal."
    elif status == "TRIGGER_READY":
        outcome_status = "TRIGGER_READY"
        outcome_detail = "Trigger Ready signal being monitored."
    elif status == "WATCH":
        outcome_status = "WATCH"
        outcome_detail = "Watch candidate being monitored."

    row["outcome_status"] = outcome_status
    row["outcome_detail"] = outcome_detail

    # Compact notes for later review.
    notes = []
    if bool(signal.get("lunch_caution")) or safe_str(signal.get("actionability"), "").upper() == "LUNCH_CAUTION":
        notes.append("Lunch caution/manual review only.")
    if success_without_active:
        notes.append("Target reached from Trigger Ready before Active Signal.")
    if entry_touched_before_active and not had_active_before_now:
        notes.append("Entry trigger touched before Active Signal.")
    if bool(signal.get("earnings_reaction_trade")):
        notes.append("Earnings reaction only.")
    event_warning = safe_str(signal.get("event_risk_warning"), "")
    if event_warning:
        notes.append(event_warning)
    row["notes"] = " | ".join(dict.fromkeys([n for n in notes if n]))

    return row


def update_signal_outcomes(new_state: Dict[str, Dict[str, Any]], prior_state: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Persistent signal performance tracker.

    This tracks signal behavior, not the user's manual trades. It keeps history
    across strategy changes and tags every row with SIGNAL_ENGINE_STRATEGY_VERSION.
    """
    rows = load_signal_outcomes()
    rows_by_id = {safe_str(row.get("signal_id"), ""): row for row in rows if safe_str(row.get("signal_id"), "")}
    changed = 0
    current_symbols = set()

    for symbol, signal in new_state.items():
        if not signal:
            continue

        status = normalize_status(signal.get("signal_status"))
        if status in {"", "WAIT"}:
            continue

        symbol = normalize_symbol(symbol or signal.get("symbol"))
        current_symbols.add(symbol)

        existing_row = None
        signal_id = safe_str(signal.get("signal_id"), "")
        if signal_id and signal_id in rows_by_id:
            existing_row = rows_by_id[signal_id]

        if existing_row is None:
            existing_row = find_open_outcome_row(symbol, rows)

        if existing_row is None:
            existing_row = initial_outcome_row(signal, rows)
            rows.append(existing_row)
            rows_by_id[safe_str(existing_row.get("signal_id"), "")] = existing_row
        else:
            # Ensure current signal payload carries the persistent id into signal_state.json.
            signal["signal_id"] = safe_str(existing_row.get("signal_id"), "")

        signal["signal_id"] = safe_str(existing_row.get("signal_id"), "") or make_signal_id(signal, rows)

        before = json.dumps(existing_row, sort_keys=True, default=str)
        updated = update_one_outcome(existing_row, signal)
        after = json.dumps(updated, sort_keys=True, default=str)
        if before != after:
            changed += 1

    # Mark WATCH rows that disappeared from Signal Desk as removed. Protected Ready/Active
    # should normally become INVALIDATED in new_state instead of silently disappearing.
    now_text = iso_now_et()
    prior_watch_symbols = {
        normalize_symbol(sym)
        for sym, signal in prior_state.items()
        if normalize_status(signal.get("signal_status")) == "WATCH"
    }
    for row in rows:
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol or symbol in current_symbols:
            continue
        if symbol in prior_watch_symbols and safe_str(row.get("outcome_status"), "").upper() == "WATCH":
            row["last_checked"] = now_text
            row["completed_time"] = row.get("completed_time") or now_text
            row["outcome_status"] = "WATCH_REMOVED"
            row["outcome_detail"] = "WATCH candidate removed because it no longer passed live criteria or fell out of focus."
            changed += 1

    # Keep history but stable ordering.
    rows.sort(key=lambda r: safe_str(r.get("first_seen_time"), ""))

    try:
        write_signal_outcomes(rows)
    except Exception as exc:
        decision_log("-", "OUTCOME_WRITE_ERROR", error=str(exc))

    summary = summarize_signal_outcomes(rows)
    try:
        write_json(SIGNAL_OUTCOMES_SUMMARY_FILE, summary)
    except Exception as exc:
        decision_log("-", "OUTCOME_SUMMARY_WRITE_ERROR", error=str(exc))

    decision_log(
        "-",
        "OUTCOMES_UPDATED",
        total=len(rows),
        changed=changed,
        today=summary.get("today", {}).get("total", 0),
        active=summary.get("today", {}).get("active_signal", 0),
        ready=summary.get("today", {}).get("trigger_ready", 0),
        t1=summary.get("today", {}).get("t1_hit", 0),
        stop=summary.get("today", {}).get("stop_hit", 0),
    )

    return summary


def summarize_signal_outcomes(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    today = session_date_str()
    now_dt = ny_now()
    cutoff_7d = now_dt.date().toordinal() - 6

    def row_session_ordinal(row: Dict[str, Any]) -> int:
        session = safe_str(row.get("session_date"), "")
        try:
            return datetime.fromisoformat(session).date().toordinal()
        except Exception:
            return 0

    today_rows = [r for r in rows if safe_str(r.get("session_date"), "") == today]
    last_7d_rows = [r for r in rows if row_session_ordinal(r) >= cutoff_7d]

    def build_counts(selected_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        counts = {
            "total": len(selected_rows),
            "watch": 0,
            "trigger_ready": 0,
            "trigger_touched": 0,
            "active_signal": 0,
            "t1_hit": 0,
            "t2_hit": 0,
            "stop_hit": 0,
            "invalidated_before_entry": 0,
            "invalidated_after_entry": 0,
            "watch_removed": 0,
            "ready_only_success": 0,
            "open": 0,
            "completed": 0,
            "ready_events": 0,
            "touched_events": 0,
            "active_events": 0,
            "ready_to_touched_pct": 0.0,
            "touched_to_active_pct": 0.0,
            "ready_to_t1_pct": 0.0,
            "most_common_invalidation_reason": "",
        }

        invalidation_reasons: Dict[str, int] = {}

        for row in selected_rows:
            status = safe_str(row.get("outcome_status"), "").upper()

            ready_event = bool(safe_str(row.get("trigger_ready_time"), "")) or status in {
                "TRIGGER_READY", "TRIGGER_TOUCHED", "ACTIVE_SIGNAL", "T1_HIT", "T2_HIT", "STOP_HIT",
                "INVALIDATED_BEFORE_ENTRY", "INVALIDATED_AFTER_ENTRY",
            }
            touched_event = (
                bool(safe_str(row.get("trigger_touched_time"), ""))
                or _csv_bool(row.get("hit_entry"))
                or _csv_bool(row.get("entry_touched_before_active"))
                or status in {"TRIGGER_TOUCHED", "ACTIVE_SIGNAL", "T1_HIT", "T2_HIT", "STOP_HIT", "INVALIDATED_AFTER_ENTRY"}
            )
            active_event = bool(safe_str(row.get("active_time"), "")) or status in {"ACTIVE_SIGNAL", "T1_HIT", "T2_HIT", "STOP_HIT"}
            t1_event = _csv_bool(row.get("hit_t1")) or status in {"T1_HIT", "T2_HIT"}
            t2_event = _csv_bool(row.get("hit_t2")) or status == "T2_HIT"
            stop_event = _csv_bool(row.get("hit_stop")) or status == "STOP_HIT"

            if ready_event:
                counts["ready_events"] += 1
            if touched_event:
                counts["touched_events"] += 1
            if active_event:
                counts["active_events"] += 1
            if t1_event and status not in {"T1_HIT", "T2_HIT"}:
                counts["t1_hit"] += 1
            if t2_event and status != "T2_HIT":
                counts["t2_hit"] += 1
            if stop_event and status != "STOP_HIT":
                counts["stop_hit"] += 1

            if _csv_bool(row.get("success_without_active")):
                counts["ready_only_success"] += 1

            if status == "WATCH":
                counts["watch"] += 1
                counts["open"] += 1
            elif status == "TRIGGER_READY":
                counts["trigger_ready"] += 1
                counts["open"] += 1
            elif status == "TRIGGER_TOUCHED":
                counts["trigger_touched"] += 1
                counts["open"] += 1
            elif status == "ACTIVE_SIGNAL":
                counts["active_signal"] += 1
                counts["open"] += 1
            elif status == "T1_HIT":
                counts["t1_hit"] += 1
                counts["completed"] += 1
            elif status == "T2_HIT":
                counts["t2_hit"] += 1
                counts["completed"] += 1
            elif status == "STOP_HIT":
                counts["stop_hit"] += 1
                counts["completed"] += 1
            elif status == "INVALIDATED_BEFORE_ENTRY":
                counts["invalidated_before_entry"] += 1
                counts["completed"] += 1
            elif status == "INVALIDATED_AFTER_ENTRY":
                counts["invalidated_after_entry"] += 1
                counts["completed"] += 1
            elif status == "WATCH_REMOVED":
                counts["watch_removed"] += 1
                counts["completed"] += 1

            if status.startswith("INVALIDATED") or "INVALID" in status:
                reason = safe_str(row.get("invalidation_reason") or row.get("outcome_detail"), "")
                if reason:
                    reason = reason[:120]
                    invalidation_reasons[reason] = invalidation_reasons.get(reason, 0) + 1

        if counts["ready_events"] > 0:
            counts["ready_to_touched_pct"] = round(counts["touched_events"] / counts["ready_events"] * 100.0, 1)
            counts["ready_to_t1_pct"] = round(counts["t1_hit"] / counts["ready_events"] * 100.0, 1)
        if counts["touched_events"] > 0:
            counts["touched_to_active_pct"] = round(counts["active_events"] / counts["touched_events"] * 100.0, 1)

        if invalidation_reasons:
            counts["most_common_invalidation_reason"] = sorted(
                invalidation_reasons.items(),
                key=lambda kv: kv[1],
                reverse=True,
            )[0][0]

        return counts

    def build_setup_counts(selected_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
        setup_counts: Dict[str, Dict[str, int]] = {}
        for row in selected_rows:
            status = safe_str(row.get("outcome_status"), "").upper()
            setup = safe_str(row.get("setup_type"), "UNKNOWN") or "UNKNOWN"
            setup_counts.setdefault(setup, {
                "total": 0,
                "touched": 0,
                "active": 0,
                "t1_hit": 0,
                "t2_hit": 0,
                "stop_hit": 0,
                "invalidated": 0,
                "ready_only_success": 0,
            })
            setup_counts[setup]["total"] += 1
            if _csv_bool(row.get("hit_entry")) or _csv_bool(row.get("entry_touched_before_active")):
                setup_counts[setup]["touched"] += 1
            if bool(safe_str(row.get("active_time"), "")) or status in {"ACTIVE_SIGNAL", "T1_HIT", "T2_HIT", "STOP_HIT"}:
                setup_counts[setup]["active"] += 1
            if _csv_bool(row.get("success_without_active")):
                setup_counts[setup]["ready_only_success"] += 1
            if status == "T1_HIT" or _csv_bool(row.get("hit_t1")):
                setup_counts[setup]["t1_hit"] += 1
            if status == "T2_HIT" or _csv_bool(row.get("hit_t2")):
                setup_counts[setup]["t2_hit"] += 1
            if status == "STOP_HIT" or _csv_bool(row.get("hit_stop")):
                setup_counts[setup]["stop_hit"] += 1
            if status.startswith("INVALIDATED"):
                setup_counts[setup]["invalidated"] += 1
        return setup_counts

    recent = sorted(today_rows, key=lambda r: safe_str(r.get("last_checked"), ""), reverse=True)[:12]

    return {
        "generated_at_et": iso_now_et(),
        "strategy_version": SIGNAL_ENGINE_STRATEGY_VERSION,
        "today_session": today,
        "today": build_counts(today_rows),
        "last_7_days": build_counts(last_7d_rows),
        "by_setup_type": build_setup_counts(today_rows),
        "by_setup_type_7d": build_setup_counts(last_7d_rows),
        "recent": recent,
        "file": SIGNAL_OUTCOMES_FILE,
    }


# ==============================================================
# ALPACA MARKET DATA
# ==============================================================

class AlpacaMarketData:
    def __init__(self, feed: str = DATA_FEED):
        # Support both common Alpaca credential environment variable styles.
        # Do not hardcode API keys in source files.
        self.api_key = (
            os.getenv("ALPACA_API_KEY")
            or os.getenv("APCA_API_KEY_ID")
            or ""
        ).strip()
        self.secret_key = (
            os.getenv("ALPACA_SECRET_KEY")
            or os.getenv("APCA_API_SECRET_KEY")
            or ""
        ).strip()
        self.feed = (feed or DATA_FEED or "sip").strip().lower()

        self.headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        }

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def _chunks(self, symbols: List[str], size: int = 20) -> Iterable[List[str]]:
        for i in range(0, len(symbols), size):
            yield symbols[i:i + size]

    def fetch_bars(self, symbols: List[str], timeframe: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch today's regular-session bars.
        """
        if not self.available:
            log("  ⚠ Alpaca credentials missing; signal engine cannot fetch intraday bars.")
            return {}

        symbols = [normalize_symbol(s) for s in symbols if normalize_symbol(s)]
        symbols = list(dict.fromkeys(symbols))

        if not symbols:
            return {}

        start, end = session_start_end_utc()
        url = f"{ALPACA_BASE_URL}/stocks/bars"
        output: Dict[str, List[Dict[str, Any]]] = {}

        for batch in self._chunks(symbols, 20):
            page_token = None

            while True:
                params = {
                    "symbols": ",".join(batch),
                    "timeframe": timeframe,
                    "start": start,
                    "end": end,
                    "limit": 10000,
                    "adjustment": "raw",
                    "feed": self.feed,
                    "sort": "asc",
                }

                if page_token:
                    params["page_token"] = page_token

                try:
                    r = requests.get(url, headers=self.headers, params=params, timeout=25)

                    if r.status_code != 200:
                        log(f"  ⚠ Alpaca bars error {r.status_code}: {r.text[:300]}")
                        break

                    data = r.json()
                    bars = data.get("bars", {}) or {}

                    for sym, sym_bars in bars.items():
                        output.setdefault(sym, []).extend(sym_bars or [])

                    page_token = data.get("next_page_token")
                    if not page_token:
                        break

                    time.sleep(0.12)

                except Exception as e:
                    log(f"  ⚠ Alpaca bars request failed: {e}")
                    break

        return output

    def fetch_latest_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        if not self.available:
            return {}

        symbols = [normalize_symbol(s) for s in symbols if normalize_symbol(s)]
        symbols = list(dict.fromkeys(symbols))

        if not symbols:
            return {}

        url = f"{ALPACA_BASE_URL}/stocks/quotes/latest"
        output: Dict[str, Dict[str, Any]] = {}

        for batch in self._chunks(symbols, 50):
            params = {
                "symbols": ",".join(batch),
                "feed": self.feed,
            }

            try:
                r = requests.get(url, headers=self.headers, params=params, timeout=20)

                if r.status_code != 200:
                    log(f"  ⚠ Alpaca quotes error {r.status_code}: {r.text[:300]}")
                    continue

                data = r.json()
                quotes = data.get("quotes", {}) or {}

                for sym, quote in quotes.items():
                    output[sym] = quote or {}

            except Exception as e:
                log(f"  ⚠ Alpaca quotes request failed: {e}")

        return output

    def fetch_latest_trades(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Fetch latest Alpaca trade for each symbol.

        This is used only as a price recency fallback. VWAP/HOD/structure still
        come from the intraday bars.
        """
        if not self.available:
            return {}

        symbols = [normalize_symbol(s) for s in symbols if normalize_symbol(s)]
        symbols = list(dict.fromkeys(symbols))

        if not symbols:
            return {}

        url = f"{ALPACA_BASE_URL}/stocks/trades/latest"
        output: Dict[str, Dict[str, Any]] = {}

        for batch in self._chunks(symbols, 50):
            params = {
                "symbols": ",".join(batch),
                "feed": self.feed,
            }

            try:
                r = requests.get(url, headers=self.headers, params=params, timeout=20)

                if r.status_code != 200:
                    log(f"  ⚠ Alpaca trades error {r.status_code}: {r.text[:300]}")
                    continue

                data = r.json()
                trades = data.get("trades", {}) or {}

                for sym, trade in trades.items():
                    output[sym] = trade or {}

            except Exception as e:
                log(f"  ⚠ Alpaca trades request failed: {e}")

        return output


# ==============================================================
# BAR ANALYSIS
# ==============================================================

@dataclass
class IntradayMetrics:
    symbol: str
    has_data: bool = False
    price: float = 0.0
    session_open: float = 0.0
    day_change_pct: float = 0.0
    vwap: float = 0.0
    above_vwap: bool = False
    vwap_dist_pct: float = 0.0
    hod: float = 0.0
    lod: float = 0.0
    hod_distance_pct: float = 0.0
    day_volume: float = 0.0
    latest_bar_time: str = ""
    price_source: str = "Alpaca bar close"
    price_updated_at: str = ""
    bid: float = 0.0
    ask: float = 0.0
    quote_mid: float = 0.0
    quote_time: str = ""
    trade_price: float = 0.0
    trade_time: str = ""
    execution_bars: List[Dict[str, Any]] = field(default_factory=list)

    recent_1m_volume: float = 0.0
    avg_volume_1m_5: float = 0.0
    avg_volume_1m_20: float = 0.0
    recent_5m_dollar_volume: float = 0.0
    live_participation_ok: bool = True
    live_participation_ready_ok: bool = True
    live_participation_reason: str = "OK"

    vwap_reclaim_recent: bool = False
    vwap_reclaim_ready: bool = False
    vwap_reclaim_bar_high: float = 0.0
    vwap_reclaim_bar_low: float = 0.0
    vwap_reclaim_bar_close: float = 0.0
    vwap_reclaim_bar_time: str = ""
    vwap_reclaim_age_minutes: float = 999.0
    vwap_reclaim_volume_ratio: float = 0.0
    vwap_reclaim_reason: str = "No recent VWAP reclaim"
    vwap_reclaim_lifecycle_active: bool = False
    vwap_reclaim_support_level: float = 0.0
    reclaim_pullback_holding: bool = False
    reclaim_pullback_reason: str = "No reclaim pullback hold"

    avg_volume_5: float = 0.0
    avg_volume_prev_5: float = 0.0
    morning_avg_volume_5m: float = 0.0
    recent_to_morning_volume_ratio: float = 0.0
    volume_stable_or_increasing: bool = False
    volume_drying: bool = False
    volume_fading_vs_morning: bool = False
    volume_reference_avg_5m: float = 0.0
    volume_reference_label: str = "morning reference"
    volume_fade_reason: str = ""
    recent_volume_expanding: bool = False

    bearish_momentum_divergence: bool = False
    macd_prior_high: float = 0.0
    macd_recent_high: float = 0.0
    price_prior_high: float = 0.0
    price_recent_high: float = 0.0

    macd_value: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    macd_histogram_prev: float = 0.0
    macd_above_signal: bool = False
    macd_above_zero: bool = False
    macd_bullish_crossover_recent: bool = False
    macd_bearish_crossover_recent: bool = False
    macd_histogram_rising: bool = False
    macd_histogram_falling: bool = False
    macd_status: str = "UNKNOWN"
    momentum_status: str = "CLEAN"

    # Faster 1-minute MACD used for VWAP reclaim / immediate pullback lifecycle.
    macd_1m_value: float = 0.0
    macd_1m_signal: float = 0.0
    macd_1m_histogram: float = 0.0
    macd_1m_histogram_prev: float = 0.0
    macd_1m_above_signal: bool = False
    macd_1m_bullish_crossover_recent: bool = False
    macd_1m_bearish_crossover_recent: bool = False
    macd_1m_histogram_rising: bool = False
    macd_1m_histogram_falling: bool = False
    macd_1m_curling_up: bool = False
    macd_1m_curling_down: bool = False
    macd_1m_status: str = "UNKNOWN"

    ema9: float = 0.0
    ema9_prev: float = 0.0
    ema9_slope_pct: float = 0.0
    price_above_ema9: bool = False
    ema9_rising: bool = False
    ema9_falling: bool = False
    ema9_above_vwap: bool = False
    ema9_crossed_above_vwap_recent: bool = False
    ema9_status: str = "UNKNOWN"

    pullback_holding_vwap: bool = False
    pullback_high: float = 0.0
    pullback_low: float = 0.0
    recent_swing_low: float = 0.0
    opening_range_low: float = 0.0
    vwap_touch_count: int = 0
    vwap_opening_touches_ignored: int = 0
    vwap_recent_touch_count: int = 0
    vwap_clean_hold_count: int = 0
    vwap_failed_touch_count: int = 0
    vwap_touch_status: str = "UNKNOWN"
    bullish_structure_start_et: str = ""
    consolidating_near_high: bool = False

    base_compression: bool = False
    base_range_pct: float = 0.0
    base_high: float = 0.0
    base_low: float = 0.0
    base_volume_constructive: bool = False
    higher_low_or_flat_base: bool = False
    price_near_base_breakout: bool = False
    structure_bar_count: int = 0


def typical_price(bar: Dict[str, Any]) -> float:
    h = safe_float(bar.get("h"), 0)
    l = safe_float(bar.get("l"), 0)
    c = safe_float(bar.get("c"), 0)
    if h > 0 and l > 0 and c > 0:
        return (h + l + c) / 3.0
    return c


def clean_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for b in bars:
        c = safe_float(b.get("c"), 0)
        if c > 0:
            out.append(b)
    return out


def bar_time_et(bar: Dict[str, Any]) -> Optional[datetime]:
    """Return an Alpaca bar timestamp in New York time when available."""
    dt = parse_iso_dt(bar.get("t"))
    if not dt:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    if ZoneInfo:
        return dt.astimezone(ZoneInfo("America/New_York"))

    return dt


def is_opening_noise_bar(bar: Dict[str, Any]) -> bool:
    """Opening VWAP crosses before 9:45 ET are ignored for VWAP-touch quality."""
    dt = bar_time_et(bar)
    if not dt:
        return False
    return dt.time() < dtime(9, 45)


def bar_age_minutes_from_now(bar: Dict[str, Any], now: Optional[datetime] = None) -> float:
    dt = bar_time_et(bar)
    if not dt:
        return 999.0
    now = now or ny_now()
    try:
        return max(0.0, (now - dt).total_seconds() / 60.0)
    except Exception:
        return 999.0


def rolling_average(values: List[float]) -> float:
    vals = [safe_float(v, 0) for v in values if safe_float(v, 0) > 0]
    return sum(vals) / len(vals) if vals else 0.0


def ema_at_index(values: List[float], idx: int) -> float:
    if not values:
        return 0.0
    if idx < 0:
        return values[0]
    if idx >= len(values):
        return values[-1]
    return values[idx]


def ema_series(values: List[float], span: int) -> List[float]:
    if not values:
        return []

    alpha = 2.0 / (span + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1.0 - alpha) * out[-1])
    return out


def macd_line(values: List[float]) -> List[float]:
    if len(values) < MACD_SLOW_SPAN:
        return []

    fast = ema_series(values, MACD_FAST_SPAN)
    slow = ema_series(values, MACD_SLOW_SPAN)
    return [f - s for f, s in zip(fast, slow)]


def macd_components(values: List[float]) -> Tuple[List[float], List[float], List[float]]:
    """
    Returns MACD line, signal line, and histogram.
    Uses 5-minute closes in this engine.
    """
    macd = macd_line(values)
    if not macd:
        return [], [], []

    signal = ema_series(macd, MACD_SIGNAL_SPAN)
    hist = [m - s for m, s in zip(macd, signal)]
    return macd, signal, hist


def analyze_execution_bars(symbol: str, bars: List[Dict[str, Any]]) -> IntradayMetrics:
    metrics = IntradayMetrics(symbol=symbol)

    clean = clean_bars(bars)
    if not clean:
        return metrics

    metrics.has_data = True
    metrics.execution_bars = clean[-60:]
    metrics.price = safe_float(clean[-1].get("c"), 0)
    metrics.price_source = f"Alpaca {DATA_FEED.upper()} 1Min bar close"
    metrics.session_open = safe_float(clean[0].get("o"), safe_float(clean[0].get("c"), 0))
    metrics.day_change_pct = pct_change(metrics.price, metrics.session_open) if metrics.session_open > 0 else 0
    metrics.hod = max(safe_float(b.get("h"), 0) for b in clean)
    metrics.lod = min(safe_float(b.get("l"), metrics.price) for b in clean if safe_float(b.get("l"), 0) > 0)
    metrics.latest_bar_time = safe_str(clean[-1].get("t"), "")
    metrics.price_updated_at = metrics.latest_bar_time

    pv = 0.0
    vol = 0.0
    for b in clean:
        bar_vol = safe_float(b.get("v"), 0)
        pv += typical_price(b) * bar_vol
        vol += bar_vol

    metrics.day_volume = vol
    metrics.vwap = pv / vol if vol > 0 else metrics.price
    metrics.above_vwap = metrics.price >= metrics.vwap if metrics.vwap > 0 else False
    metrics.vwap_dist_pct = pct_change(metrics.price, metrics.vwap) if metrics.vwap > 0 else 0
    metrics.hod_distance_pct = pct_change(metrics.price, metrics.hod) if metrics.hod > 0 else 0

    # 1-minute MACD for early VWAP reclaim / pullback timing.
    closes_1m = [safe_float(b.get("c"), 0) for b in clean]
    macd_1m, macd_1m_signal, macd_1m_hist = macd_components(closes_1m)
    if len(macd_1m) >= 4 and len(macd_1m_signal) >= 4 and len(macd_1m_hist) >= 4:
        metrics.macd_1m_value = macd_1m[-1]
        metrics.macd_1m_signal = macd_1m_signal[-1]
        metrics.macd_1m_histogram = macd_1m_hist[-1]
        metrics.macd_1m_histogram_prev = macd_1m_hist[-2]
        metrics.macd_1m_above_signal = metrics.macd_1m_value >= metrics.macd_1m_signal
        metrics.macd_1m_histogram_rising = macd_1m_hist[-1] > macd_1m_hist[-2]
        metrics.macd_1m_histogram_falling = macd_1m_hist[-1] < macd_1m_hist[-2]
        metrics.macd_1m_curling_up = (
            macd_1m_hist[-1] > macd_1m_hist[-2]
            and (len(macd_1m_hist) < 5 or macd_1m_hist[-2] >= macd_1m_hist[-3] * 0.95)
        )
        metrics.macd_1m_curling_down = (
            macd_1m_hist[-1] < macd_1m_hist[-2]
            and (len(macd_1m_hist) < 5 or macd_1m_hist[-2] <= macd_1m_hist[-3] * 1.05)
        )

        recent_pairs_1m = list(zip(macd_1m[-8:], macd_1m_signal[-8:]))
        for i in range(1, len(recent_pairs_1m)):
            prev_macd, prev_sig = recent_pairs_1m[i - 1]
            cur_macd, cur_sig = recent_pairs_1m[i]
            if prev_macd <= prev_sig and cur_macd > cur_sig:
                metrics.macd_1m_bullish_crossover_recent = True
            if prev_macd >= prev_sig and cur_macd < cur_sig:
                metrics.macd_1m_bearish_crossover_recent = True

        if metrics.macd_1m_bullish_crossover_recent:
            metrics.macd_1m_status = "BULLISH_CROSSOVER"
        elif metrics.macd_1m_curling_up and metrics.macd_1m_above_signal:
            metrics.macd_1m_status = "CURLING_UP"
        elif metrics.macd_1m_bearish_crossover_recent:
            metrics.macd_1m_status = "BEARISH_CROSSOVER"
        elif metrics.macd_1m_curling_down and not metrics.macd_1m_above_signal:
            metrics.macd_1m_status = "CURLING_DOWN"
        elif metrics.macd_1m_above_signal:
            metrics.macd_1m_status = "ABOVE_SIGNAL"
        else:
            metrics.macd_1m_status = "NEUTRAL"

    first15 = clean[:15]
    if first15:
        lows = [safe_float(b.get("l"), 0) for b in first15 if safe_float(b.get("l"), 0) > 0]
        metrics.opening_range_low = min(lows) if lows else metrics.lod

    # Live participation from recent 1-minute bars.
    recent5_1m = clean[-5:] if len(clean) >= 5 else clean
    recent20_1m = clean[-20:] if len(clean) >= 20 else clean
    metrics.recent_1m_volume = safe_float(clean[-1].get("v"), 0)
    metrics.avg_volume_1m_5 = rolling_average([safe_float(b.get("v"), 0) for b in recent5_1m])
    metrics.avg_volume_1m_20 = rolling_average([safe_float(b.get("v"), 0) for b in recent20_1m])
    recent5_dollars = 0.0
    for b in recent5_1m:
        recent5_dollars += safe_float(b.get("v"), 0) * typical_price(b)
    metrics.recent_5m_dollar_volume = recent5_dollars

    if metrics.avg_volume_1m_5 < MIN_LIVE_1M_AVG_VOL_WATCH and metrics.recent_5m_dollar_volume < MIN_LIVE_5M_DOLLAR_VOL_WATCH:
        metrics.live_participation_ok = False
        metrics.live_participation_reason = (
            f"Low live participation: 5m avg vol {metrics.avg_volume_1m_5:.0f}, "
            f"5m $vol ${metrics.recent_5m_dollar_volume:,.0f}"
        )
    else:
        metrics.live_participation_ok = True
        metrics.live_participation_reason = "OK"

    metrics.live_participation_ready_ok = (
        metrics.avg_volume_1m_5 >= MIN_LIVE_1M_AVG_VOL_READY
        or metrics.recent_5m_dollar_volume >= MIN_LIVE_5M_DOLLAR_VOL_READY
    )

    # VWAP reclaim breakout detection from 1-minute bars.
    #
    # This catches setups like:
    #   below VWAP -> high-volume reclaim -> hold/continue above VWAP
    # before they turn into chased/extended moves. Opening noise is ignored.
    if metrics.vwap > 0 and len(clean) >= 6:
        search = clean[-max(12, VWAP_RECLAIM_LIFECYCLE_MINUTES + 3):]
        reclaim_candidates: List[Tuple[int, Dict[str, Any], float]] = []

        for local_idx, bar in enumerate(search):
            global_idx = len(clean) - len(search) + local_idx
            dt = bar_time_et(bar)
            if dt and dt.time() < dtime(9, 45):
                continue

            close = safe_float(bar.get("c"), 0)
            open_ = safe_float(bar.get("o"), close)
            low = safe_float(bar.get("l"), 0)
            high = safe_float(bar.get("h"), 0)
            vol_bar = safe_float(bar.get("v"), 0)

            prior_window = clean[max(0, global_idx - 20):global_idx]
            prior_avg_vol = rolling_average([safe_float(x.get("v"), 0) for x in prior_window])
            vol_ratio = vol_bar / prior_avg_vol if prior_avg_vol > 0 else 0.0

            prev_close = safe_float(clean[global_idx - 1].get("c"), 0) if global_idx > 0 else 0
            was_below = (
                prev_close > 0 and prev_close < metrics.vwap * 0.998
            ) or low <= metrics.vwap * 0.998 or open_ < metrics.vwap * 0.998
            reclaimed = close >= metrics.vwap * 1.001 and high >= metrics.vwap
            green_or_strong = close >= open_ or close >= ((high + low) / 2.0 if high > low else close)

            if was_below and reclaimed and green_or_strong:
                reclaim_candidates.append((global_idx, bar, vol_ratio))

        if reclaim_candidates:
            _, reclaim_bar, vol_ratio = reclaim_candidates[-1]
            age_min = bar_age_minutes_from_now(reclaim_bar)
            bar_high = safe_float(reclaim_bar.get("h"), 0)
            bar_low = safe_float(reclaim_bar.get("l"), 0)
            bar_close = safe_float(reclaim_bar.get("c"), 0)
            metrics.vwap_reclaim_recent = age_min <= VWAP_RECLAIM_LOOKBACK_MINUTES
            metrics.vwap_reclaim_lifecycle_active = age_min <= VWAP_RECLAIM_LIFECYCLE_MINUTES
            metrics.vwap_reclaim_bar_high = bar_high
            metrics.vwap_reclaim_bar_low = bar_low
            metrics.vwap_reclaim_bar_close = bar_close
            metrics.vwap_reclaim_bar_time = timestamp_to_et_iso(reclaim_bar.get("t"))
            metrics.vwap_reclaim_age_minutes = round(age_min, 2)
            metrics.vwap_reclaim_volume_ratio = round(vol_ratio, 2)
            metrics.vwap_reclaim_support_level = max(
                metrics.vwap * (1.0 - RECLAIM_PULLBACK_SUPPORT_BUFFER_PCT / 100.0),
                bar_low,
            ) if metrics.vwap > 0 and bar_low > 0 else metrics.vwap

            fresh_extension_ok = metrics.vwap_dist_pct <= VWAP_RECLAIM_MAX_EXTENSION_PCT
            pullback_extension_ok = metrics.vwap_dist_pct <= RECLAIM_PULLBACK_MAX_EXTENSION_PCT
            volume_ok = vol_ratio >= VWAP_RECLAIM_MIN_VOLUME_RATIO or metrics.recent_5m_dollar_volume >= MIN_LIVE_5M_DOLLAR_VOL_READY
            hold_ok = metrics.price >= metrics.vwap and bar_close >= metrics.vwap
            support_hold_ok = (
                metrics.vwap > 0
                and metrics.price >= metrics.vwap * (1.0 - RECLAIM_PULLBACK_SUPPORT_BUFFER_PCT / 100.0)
                and (
                    metrics.vwap_reclaim_support_level <= 0
                    or metrics.price >= metrics.vwap_reclaim_support_level * 0.995
                )
            )
            participation_ok = metrics.live_participation_ready_ok

            if metrics.vwap_reclaim_recent and fresh_extension_ok and volume_ok and hold_ok and participation_ok:
                metrics.vwap_reclaim_ready = True
                metrics.vwap_reclaim_reason = (
                    f"Recent high-volume VWAP reclaim; age {age_min:.1f}m, "
                    f"vol ratio {vol_ratio:.2f}x, extension {metrics.vwap_dist_pct:.2f}%"
                )
            else:
                fails = []
                if not metrics.vwap_reclaim_recent:
                    fails.append(f"fresh reclaim age {age_min:.1f}m > {VWAP_RECLAIM_LOOKBACK_MINUTES}m")
                if not fresh_extension_ok:
                    fails.append(f"VWAP extension {metrics.vwap_dist_pct:.2f}% > {VWAP_RECLAIM_MAX_EXTENSION_PCT:.1f}%")
                if not volume_ok:
                    fails.append(f"reclaim volume ratio {vol_ratio:.2f}x < {VWAP_RECLAIM_MIN_VOLUME_RATIO:.2f}x")
                if not hold_ok:
                    fails.append("price did not hold above VWAP")
                if not participation_ok:
                    fails.append(metrics.live_participation_reason)
                metrics.vwap_reclaim_reason = "; ".join(fails) if fails else "VWAP reclaim not ready"

            if metrics.vwap_reclaim_lifecycle_active and support_hold_ok and pullback_extension_ok and participation_ok:
                metrics.reclaim_pullback_holding = True
                metrics.reclaim_pullback_reason = (
                    f"VWAP reclaim lifecycle holding; reclaim age {age_min:.1f}m, "
                    f"support {metrics.vwap_reclaim_support_level:.2f}, extension {metrics.vwap_dist_pct:.2f}%"
                )
            elif metrics.vwap_reclaim_lifecycle_active:
                fails = []
                if not support_hold_ok:
                    fails.append("reclaim pullback lost VWAP/reclaim support")
                if not pullback_extension_ok:
                    fails.append(f"VWAP extension {metrics.vwap_dist_pct:.2f}% > {RECLAIM_PULLBACK_MAX_EXTENSION_PCT:.1f}%")
                if not participation_ok:
                    fails.append(metrics.live_participation_reason)
                metrics.reclaim_pullback_reason = "; ".join(fails) if fails else "VWAP reclaim pullback not holding"

    return metrics


def enrich_structure_from_5min(metrics: IntradayMetrics, bars_5m: List[Dict[str, Any]]) -> IntradayMetrics:
    clean = clean_bars(bars_5m)
    metrics.structure_bar_count = len(clean)

    if not clean:
        return metrics

    recent3 = clean[-3:] if len(clean) >= 3 else clean
    recent5 = clean[-5:] if len(clean) >= 5 else clean

    # Structure volume.
    prev5 = clean[-10:-5] if len(clean) >= 10 else clean[:-5]
    recent_vols = [safe_float(b.get("v"), 0) for b in recent5]
    prev_vols = [safe_float(b.get("v"), 0) for b in prev5]

    # Adaptive volume reference.
    #
    # Old behavior compared every setup to the opening burst, which incorrectly
    # rejected valid 10:00 VWAP-reclaim setups as "volume fading." The new
    # behavior uses a time-aware reference:
    #   - Before 11:00 ET: compare recent volume to the previous 20–30 minutes.
    #   - After 11:00 ET: compare to max(previous 20–30 minutes, 50% of morning reference).
    if len(clean) >= 30:
        morning_ref = clean[6:30]  # avoids the most distorted first six 5m bars
    elif len(clean) >= 16:
        morning_ref = clean[3:16]
    else:
        morning_ref = clean[: max(1, len(clean) // 2)]

    morning_vols = [safe_float(b.get("v"), 0) for b in morning_ref]
    metrics.avg_volume_5 = sum(recent_vols) / len(recent_vols) if recent_vols else 0
    metrics.avg_volume_prev_5 = sum(prev_vols) / len(prev_vols) if prev_vols else 0
    metrics.morning_avg_volume_5m = sum(morning_vols) / len(morning_vols) if morning_vols else 0

    now_time = ny_now().time()
    if now_time < dtime(11, 0):
        adaptive_ref = metrics.avg_volume_prev_5
        adaptive_label = "previous 20–30m"
    else:
        half_morning = 0.50 * metrics.morning_avg_volume_5m if metrics.morning_avg_volume_5m > 0 else 0
        adaptive_ref = max(metrics.avg_volume_prev_5, half_morning)
        adaptive_label = "adaptive late-session reference"

    metrics.volume_reference_avg_5m = adaptive_ref
    metrics.volume_reference_label = adaptive_label

    metrics.recent_to_morning_volume_ratio = (
        metrics.avg_volume_5 / metrics.morning_avg_volume_5m
        if metrics.morning_avg_volume_5m > 0
        else 0
    )

    metrics.recent_volume_expanding = (
        metrics.avg_volume_prev_5 > 0
        and metrics.avg_volume_5 >= VOLUME_EXPANSION_RATIO * metrics.avg_volume_prev_5
    )
    metrics.volume_stable_or_increasing = (
        metrics.avg_volume_prev_5 <= 0
        or metrics.avg_volume_5 >= 0.85 * metrics.avg_volume_prev_5
    )
    metrics.volume_drying = (
        metrics.avg_volume_prev_5 > 0 and metrics.avg_volume_5 <= 0.80 * metrics.avg_volume_prev_5
    )
    metrics.volume_fading_vs_morning = (
        adaptive_ref > 0
        and metrics.avg_volume_5 < VOLUME_FADE_RATIO * adaptive_ref
    )
    metrics.volume_fade_reason = volume_fade_label(metrics) if metrics.volume_fading_vs_morning else ""
    metrics.base_volume_constructive = (
        metrics.avg_volume_prev_5 <= 0
        or metrics.avg_volume_5 <= 1.20 * metrics.avg_volume_prev_5
        or metrics.volume_drying
    )

    # EMA 9 confirmation from 5-minute structure.
    closes = [safe_float(b.get("c"), 0) for b in clean]
    ema9_values = ema_series(closes, EMA_SPAN)
    if ema9_values:
        metrics.ema9 = ema9_values[-1]
        metrics.ema9_prev = ema9_values[-2] if len(ema9_values) >= 2 else ema9_values[-1]
        metrics.ema9_slope_pct = pct_change(metrics.ema9, metrics.ema9_prev) if metrics.ema9_prev > 0 else 0
        metrics.price_above_ema9 = metrics.price >= metrics.ema9 if metrics.ema9 > 0 else False
        metrics.ema9_rising = metrics.ema9 >= metrics.ema9_prev * 0.999
        metrics.ema9_falling = metrics.ema9 < metrics.ema9_prev * 0.999
        metrics.ema9_above_vwap = metrics.ema9 >= metrics.vwap if metrics.vwap > 0 else False

        # EMA9 crossing VWAP from below is treated as a bullish confirmation.
        # Uses current session VWAP as the reference line to avoid noisy per-bar VWAP math.
        recent_ema = ema9_values[-6:] if len(ema9_values) >= 6 else ema9_values
        if metrics.vwap > 0 and len(recent_ema) >= 2:
            was_below = min(recent_ema[:-1]) <= metrics.vwap
            now_above = recent_ema[-1] >= metrics.vwap
            metrics.ema9_crossed_above_vwap_recent = bool(was_below and now_above)

        if metrics.price_above_ema9 and metrics.ema9_above_vwap and not metrics.ema9_falling:
            metrics.ema9_status = "BULLISH_ALIGNMENT"
        elif metrics.ema9_crossed_above_vwap_recent and metrics.price_above_ema9:
            metrics.ema9_status = "RECENT_BULLISH_CROSS"
        elif not metrics.price_above_ema9:
            metrics.ema9_status = "PRICE_BELOW_EMA9"
        elif metrics.ema9_falling:
            metrics.ema9_status = "EMA9_FALLING"
        elif not metrics.ema9_above_vwap:
            metrics.ema9_status = "EMA9_BELOW_VWAP"
        else:
            metrics.ema9_status = "NEUTRAL"

    # MACD / momentum confirmation using 5-minute closes.
    # MACD is used as a confirmation booster and a risk filter.
    macd, macd_signal, macd_hist = macd_components(closes)
    if len(macd) >= 3 and len(macd_signal) >= 3 and len(macd_hist) >= 3:
        metrics.macd_value = macd[-1]
        metrics.macd_signal = macd_signal[-1]
        metrics.macd_histogram = macd_hist[-1]
        metrics.macd_histogram_prev = macd_hist[-2]

        metrics.macd_above_signal = metrics.macd_value >= metrics.macd_signal
        metrics.macd_above_zero = metrics.macd_value >= 0
        metrics.macd_histogram_rising = (
            macd_hist[-1] > macd_hist[-2]
            and (len(macd_hist) < 4 or macd_hist[-2] >= macd_hist[-3] * 0.98)
        )
        metrics.macd_histogram_falling = (
            macd_hist[-1] < macd_hist[-2]
            and (len(macd_hist) < 4 or macd_hist[-2] <= macd_hist[-3] * 1.02)
        )

        # Recent MACD crossovers in the last 5 completed 5-minute bars.
        recent_pairs = list(zip(macd[-6:], macd_signal[-6:]))
        for i in range(1, len(recent_pairs)):
            prev_macd, prev_sig = recent_pairs[i - 1]
            cur_macd, cur_sig = recent_pairs[i]

            if prev_macd <= prev_sig and cur_macd > cur_sig:
                metrics.macd_bullish_crossover_recent = True

            if prev_macd >= prev_sig and cur_macd < cur_sig:
                metrics.macd_bearish_crossover_recent = True

    # Bearish divergence: price makes a higher high while MACD makes a lower high.
    if len(clean) >= 30 and len(macd) >= 12:
        recent_window = clean[-12:]
        recent_macd = macd[-12:]
        first_half_bars = recent_window[:6]
        second_half_bars = recent_window[6:]
        first_half_macd = recent_macd[:6]
        second_half_macd = recent_macd[6:]

        metrics.price_prior_high = max(safe_float(b.get("h"), 0) for b in first_half_bars)
        metrics.price_recent_high = max(safe_float(b.get("h"), 0) for b in second_half_bars)
        metrics.macd_prior_high = max(first_half_macd) if first_half_macd else 0
        metrics.macd_recent_high = max(second_half_macd) if second_half_macd else 0

        price_higher_high = metrics.price_recent_high > metrics.price_prior_high * 1.0005
        macd_lower_high = metrics.macd_recent_high < metrics.macd_prior_high * 0.995

        if price_higher_high and macd_lower_high:
            metrics.bearish_momentum_divergence = True

    if metrics.bearish_momentum_divergence:
        metrics.momentum_status = "BEARISH_DIVERGENCE"
        metrics.macd_status = "BEARISH_DIVERGENCE"
    elif metrics.macd_bearish_crossover_recent:
        metrics.momentum_status = "BEARISH_CROSSOVER"
        metrics.macd_status = "BEARISH_CROSSOVER"
    elif metrics.macd_bullish_crossover_recent:
        metrics.momentum_status = "BULLISH_CROSSOVER"
        metrics.macd_status = "BULLISH_CROSSOVER"
    elif metrics.macd_above_signal and metrics.macd_histogram_rising:
        metrics.momentum_status = "BULLISH_MOMENTUM"
        metrics.macd_status = "BULLISH_MOMENTUM"
    elif metrics.macd_above_signal:
        metrics.momentum_status = "MACD_ABOVE_SIGNAL"
        metrics.macd_status = "MACD_ABOVE_SIGNAL"
    elif metrics.macd_histogram_falling:
        metrics.momentum_status = "HISTOGRAM_WEAKENING"
        metrics.macd_status = "HISTOGRAM_WEAKENING"
    elif metrics.macd_value or metrics.macd_signal:
        metrics.momentum_status = "MACD_NEUTRAL"
        metrics.macd_status = "NEUTRAL"

    # Pullback structure from last 3x 5-min bars.
    metrics.pullback_high = max(safe_float(b.get("h"), 0) for b in recent3)
    lows3 = [safe_float(b.get("l"), 0) for b in recent3 if safe_float(b.get("l"), 0) > 0]
    metrics.pullback_low = min(lows3) if lows3 else metrics.price
    metrics.recent_swing_low = metrics.pullback_low

    # VWAP hold: 2 of last 3 structure bars should close above VWAP and not undercut it heavily.
    hold_count = 0
    for b in recent3:
        low = safe_float(b.get("l"), 0)
        close = safe_float(b.get("c"), 0)
        if metrics.vwap > 0 and low >= metrics.vwap * 0.995 and close >= metrics.vwap:
            hold_count += 1
    metrics.pullback_holding_vwap = hold_count >= min(2, len(recent3))

    # VWAP reclaim lifecycle:
    # After a valid reclaim, a normal pullback to EMA9/VWAP should stay alive.
    # Do not treat a small EMA9 dip as failure if VWAP/reclaim support is holding.
    if metrics.vwap_reclaim_lifecycle_active and metrics.above_vwap:
        support_level = metrics.vwap_reclaim_support_level or metrics.vwap
        support_holding = metrics.price >= support_level * 0.995 if support_level > 0 else metrics.above_vwap
        if support_holding and (
            metrics.pullback_holding_vwap
            or metrics.price >= metrics.vwap
            or metrics.price >= support_level * 1.002
        ):
            metrics.reclaim_pullback_holding = True
            metrics.reclaim_pullback_reason = (
                f"Reclaim pullback holding above VWAP/support; support {support_level:.2f}, "
                f"VWAP dist {metrics.vwap_dist_pct:.2f}%"
            )

    # VWAP touch quality on 5-minute structure.
    #
    # Important:
    #   - Ignore 9:30-9:45 opening VWAP noise.
    #   - Reset the count after the first real bullish reclaim:
    #       two consecutive 5-min closes above VWAP + EMA9.
    #   - Count only the most recent 60-90 minutes after that reclaim.
    #   - Treat clean VWAP holds differently from failed VWAP touches.
    #
    # This prevents a good afternoon setup from being blocked just because
    # the stock chopped around VWAP during the opening volatility window.
    opening_touches = 0
    all_touch_indices: List[int] = []

    for idx, b in enumerate(clean):
        high = safe_float(b.get("h"), 0)
        low = safe_float(b.get("l"), 0)
        close = safe_float(b.get("c"), 0)
        is_touch = bool(
            metrics.vwap > 0
            and (
                (low <= metrics.vwap <= high)
                or abs(pct_change(close, metrics.vwap)) <= 0.20
            )
        )

        if not is_touch:
            continue

        all_touch_indices.append(idx)
        if is_opening_noise_bar(b):
            opening_touches += 1

    metrics.vwap_opening_touches_ignored = opening_touches

    # Find bullish VWAP+EMA9 reclaim after the opening noise.
    post_open_start_idx = 0
    for idx, b in enumerate(clean):
        dt = bar_time_et(b)
        if dt and dt.time() >= dtime(9, 45):
            post_open_start_idx = idx
            break
    else:
        # Fallback when timestamps are unavailable: skip first 3 x 5-minute bars.
        post_open_start_idx = min(3, max(0, len(clean) - 1))

    bullish_start_idx: Optional[int] = None
    for idx in range(max(1, post_open_start_idx), len(clean)):
        prev_bar = clean[idx - 1]
        cur_bar = clean[idx]

        prev_close = safe_float(prev_bar.get("c"), 0)
        cur_close = safe_float(cur_bar.get("c"), 0)
        prev_ema = ema_at_index(ema9_values, idx - 1)
        cur_ema = ema_at_index(ema9_values, idx)

        prev_ok = (
            metrics.vwap > 0
            and prev_close >= metrics.vwap
            and (prev_ema <= 0 or prev_close >= prev_ema)
        )
        cur_ok = (
            metrics.vwap > 0
            and cur_close >= metrics.vwap
            and (cur_ema <= 0 or cur_close >= cur_ema)
        )

        if prev_ok and cur_ok:
            bullish_start_idx = idx - 1
            break

    if bullish_start_idx is None:
        bullish_start_idx = post_open_start_idx

    start_bar = clean[bullish_start_idx] if clean and bullish_start_idx < len(clean) else None
    if start_bar:
        start_dt = bar_time_et(start_bar)
        metrics.bullish_structure_start_et = (
            start_dt.isoformat(timespec="minutes") if start_dt else ""
        )

    # Rolling structure window: last 18 x 5-min bars = 90 minutes.
    rolling_start_idx = max(bullish_start_idx, len(clean) - 18)
    touch_scope = clean[rolling_start_idx:]

    meaningful_touches = 0
    clean_holds = 0
    failed_touches = 0

    for local_idx, b in enumerate(touch_scope):
        idx = rolling_start_idx + local_idx
        high = safe_float(b.get("h"), 0)
        low = safe_float(b.get("l"), 0)
        close = safe_float(b.get("c"), 0)
        open_ = safe_float(b.get("o"), close)
        ema_i = ema_at_index(ema9_values, idx)

        is_touch = bool(
            metrics.vwap > 0
            and (
                (low <= metrics.vwap <= high)
                or abs(pct_change(close, metrics.vwap)) <= 0.20
            )
        )
        if not is_touch:
            continue

        meaningful_touches += 1

        clean_hold = (
            close >= metrics.vwap
            and low >= metrics.vwap * 0.992
            and (ema_i <= 0 or close >= ema_i * 0.998)
        )

        failed_touch = (
            close < metrics.vwap
            or (ema_i > 0 and close < ema_i * 0.998)
        )

        # Selling pressure makes a touch more suspicious.
        if metrics.avg_volume_prev_5 > 0:
            vol = safe_float(b.get("v"), 0)
            red_bar = close < open_
            if red_bar and vol > metrics.avg_volume_prev_5 * 1.15:
                failed_touch = True

        if clean_hold:
            clean_holds += 1
        if failed_touch:
            failed_touches += 1

    metrics.vwap_touch_count = meaningful_touches
    metrics.vwap_recent_touch_count = meaningful_touches
    metrics.vwap_clean_hold_count = clean_holds
    metrics.vwap_failed_touch_count = failed_touches

    if failed_touches >= 2:
        metrics.vwap_touch_status = "RECENT_FAILED_RETESTS"
    elif meaningful_touches >= 4 and (
        not metrics.price_above_ema9
        or metrics.ema9_falling
        or metrics.volume_fading_vs_morning
        or metrics.bearish_momentum_divergence
        or metrics.macd_histogram_falling
    ):
        metrics.vwap_touch_status = "MANY_TOUCHES_WITH_WEAK_STRUCTURE"
    elif clean_holds >= 1 and failed_touches == 0:
        metrics.vwap_touch_status = "RECENT_CLEAN_HOLD"
    elif meaningful_touches == 0:
        metrics.vwap_touch_status = "NO_RECENT_TOUCH"
    else:
        metrics.vwap_touch_status = "MIXED"

    # Base/flag compression from last 5x 5-min bars.
    recent_highs = [safe_float(b.get("h"), 0) for b in recent5 if safe_float(b.get("h"), 0) > 0]
    recent_lows = [safe_float(b.get("l"), 0) for b in recent5 if safe_float(b.get("l"), 0) > 0]

    if recent_highs and recent_lows:
        metrics.base_high = max(recent_highs)
        metrics.base_low = min(recent_lows)
        metrics.base_range_pct = pct(metrics.base_high - metrics.base_low, metrics.base_low) if metrics.base_low > 0 else 0

        # Higher-low or flat base:
        first_half = recent5[: max(1, len(recent5) // 2)]
        second_half = recent5[max(1, len(recent5) // 2):]
        first_lows = [safe_float(b.get("l"), 0) for b in first_half if safe_float(b.get("l"), 0) > 0]
        second_lows = [safe_float(b.get("l"), 0) for b in second_half if safe_float(b.get("l"), 0) > 0]

        if first_lows and second_lows:
            first_min = min(first_lows)
            second_min = min(second_lows)
            metrics.higher_low_or_flat_base = second_min >= first_min * 0.997

        metrics.price_near_base_breakout = metrics.price >= metrics.base_high * 0.995 if metrics.base_high > 0 else False

        metrics.base_compression = (
            metrics.base_range_pct <= 2.0
            and metrics.higher_low_or_flat_base
            and metrics.base_volume_constructive
            and metrics.price_near_base_breakout
        )

        recent_range_pct = pct(metrics.base_high - metrics.base_low, metrics.base_low) if metrics.base_low > 0 else 999
        metrics.consolidating_near_high = (
            metrics.hod > 0
            and pct_change(metrics.base_low, metrics.hod) >= -1.8
            and recent_range_pct <= 2.2
        )

    return metrics


def quote_spread_dollars(quote: Dict[str, Any]) -> Optional[float]:
    if not quote:
        return None

    bid = safe_float(quote.get("bp"), 0)
    ask = safe_float(quote.get("ap"), 0)

    if bid > 0 and ask > bid:
        return ask - bid

    return None


def quote_midpoint(quote: Dict[str, Any]) -> Tuple[float, float, float, str]:
    """
    Return bid, ask, midpoint, and quote timestamp from Alpaca latest quote.
    """
    if not quote:
        return 0.0, 0.0, 0.0, ""

    bid = safe_float(quote.get("bp"), 0)
    ask = safe_float(quote.get("ap"), 0)
    ts = safe_str(quote.get("t"), "")

    if bid > 0 and ask >= bid:
        return bid, ask, (bid + ask) / 2.0, ts

    return bid, ask, 0.0, ts


def trade_price_time(trade: Dict[str, Any]) -> Tuple[float, str]:
    """
    Return price and timestamp from Alpaca latest trade.
    """
    if not trade:
        return 0.0, ""

    return safe_float(trade.get("p"), 0), safe_str(trade.get("t"), "")


def timestamp_to_et_iso(value: Any) -> str:
    """
    Convert Alpaca UTC timestamps to ISO ET where possible.
    """
    raw = safe_str(value, "")
    if not raw:
        return ""

    dt = parse_iso_dt(raw)
    if not dt:
        return raw

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    if ZoneInfo:
        dt = dt.astimezone(ZoneInfo("America/New_York"))

    return dt.isoformat(timespec="seconds")


def apply_live_price_overlay(
    metrics: IntradayMetrics,
    quote: Dict[str, Any],
    trade: Optional[Dict[str, Any]] = None,
) -> IntradayMetrics:
    """
    Use the freshest Alpaca live price for Signal Desk decisions.

    Priority:
      1. Valid latest quote midpoint when spread is reasonable.
      2. Latest trade price.
      3. Last 1-minute bar close.

    VWAP/HOD still come from intraday bars, but after price overlay we recalculate
    VWAP distance, HOD distance, above VWAP, and EMA9 price checks so trigger
    promotion uses the same displayed/current price.
    """
    if not metrics.has_data:
        return metrics

    bid, ask, mid, quote_ts = quote_midpoint(quote or {})
    trade_px, trade_ts = trade_price_time(trade or {})

    metrics.bid = bid
    metrics.ask = ask
    metrics.quote_mid = mid
    metrics.quote_time = timestamp_to_et_iso(quote_ts)
    metrics.trade_price = trade_px
    metrics.trade_time = timestamp_to_et_iso(trade_ts)

    selected_price = 0.0
    selected_source = ""
    selected_time = ""

    spread_pct = ((ask - bid) / mid * 100.0) if mid > 0 and ask >= bid else 999.0

    if mid > 0 and spread_pct <= MAX_QUOTE_SPREAD_FOR_PRICE_PCT:
        selected_price = mid
        selected_source = f"Alpaca {DATA_FEED.upper()} quote mid"
        selected_time = metrics.quote_time
    elif trade_px > 0:
        selected_price = trade_px
        selected_source = f"Alpaca {DATA_FEED.upper()} latest trade"
        selected_time = metrics.trade_time
    elif mid > 0:
        selected_price = mid
        selected_source = f"Alpaca {DATA_FEED.upper()} quote mid/wide spread"
        selected_time = metrics.quote_time

    if selected_price <= 0:
        return metrics

    metrics.price = selected_price
    metrics.price_source = selected_source
    metrics.price_updated_at = selected_time or metrics.latest_bar_time

    if metrics.session_open > 0:
        metrics.day_change_pct = pct_change(metrics.price, metrics.session_open)

    if metrics.vwap > 0:
        metrics.above_vwap = metrics.price >= metrics.vwap
        metrics.vwap_dist_pct = pct_change(metrics.price, metrics.vwap)

    if metrics.hod > 0:
        # If the live quote/trade prints above the last completed bar HOD,
        # include it so HOD distance does not incorrectly show a negative value.
        metrics.hod = max(metrics.hod, metrics.price)
        metrics.hod_distance_pct = pct_change(metrics.price, metrics.hod)

    if metrics.ema9 > 0:
        metrics.price_above_ema9 = metrics.price >= metrics.ema9
        if metrics.price_above_ema9 and metrics.ema9_above_vwap and not metrics.ema9_falling:
            metrics.ema9_status = "BULLISH_ALIGNMENT"
        elif metrics.ema9_crossed_above_vwap_recent and metrics.price_above_ema9:
            metrics.ema9_status = "RECENT_BULLISH_CROSS"
        elif not metrics.price_above_ema9:
            metrics.ema9_status = "PRICE_BELOW_EMA9"
        elif metrics.ema9_falling:
            metrics.ema9_status = "EMA9_FALLING"
        elif not metrics.ema9_above_vwap:
            metrics.ema9_status = "EMA9_BELOW_VWAP"
        else:
            metrics.ema9_status = "NEUTRAL"

    if metrics.base_high > 0:
        metrics.price_near_base_breakout = metrics.price >= metrics.base_high * 0.995
        metrics.base_compression = (
            metrics.base_range_pct <= 2.0
            and metrics.higher_low_or_flat_base
            and metrics.base_volume_constructive
            and metrics.price_near_base_breakout
        )

    return metrics


# ==============================================================
# RULEBOOK CALCULATIONS
# ==============================================================

def vwap_reclaim_zone(atr_pct: float) -> Tuple[float, float]:
    if atr_pct < 2.5:
        return -0.4, 0.3
    if atr_pct <= 5.0:
        return -0.6, 0.5
    return -0.9, 0.7


def vwap_condition_pass(metrics: IntradayMetrics, atr_pct: float) -> bool:
    if not metrics.has_data or metrics.vwap <= 0:
        return False

    low, high = vwap_reclaim_zone(atr_pct)
    return metrics.vwap_dist_pct >= 0 or (low <= metrics.vwap_dist_pct <= high)


def hod_condition_pass(metrics: IntradayMetrics, source_bucket: str) -> bool:
    if not metrics.has_data:
        return False

    if "POTENTIAL" in source_bucket:
        return metrics.hod_distance_pct >= POTENTIAL_HOD_MAX_DISTANCE

    return metrics.hod_distance_pct >= ACTIVE_HOD_MAX_DISTANCE


def severe_risk_off(regime: Dict[str, Any]) -> bool:
    bias = safe_str(regime.get("bias"), "").upper()
    label = safe_str(regime.get("label"), "").lower()
    vix = safe_float(regime.get("vix_level"), 0)
    spy = safe_float(regime.get("spy_change"), 0)
    qqq = safe_float(regime.get("qqq_change"), 0)

    return (
        bias == "CAUTION"
        or vix >= 25
        or "risk-off" in label
        or spy <= -1.5
        or qqq <= -1.8
    )



ETF_SECTOR_LABELS: Dict[str, str] = {
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    "IWM": "Small Caps",
    "XLK": "Technology",
    "SMH": "Semiconductors",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLV": "Healthcare",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLC": "Communication Services",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "XLB": "Materials",
    "IBB": "Biotech",
    "KBE": "Banks",
    "KRE": "Regional Banks",
    "ITA": "Aerospace & Defense",
    "ARKK": "Innovation/Growth",
}

def etf_sector_label(etf: Any) -> str:
    code = safe_str(etf, "").upper()
    return ETF_SECTOR_LABELS.get(code, code or "Unknown Sector")


# Backward-compatible name used by existing JSON/dashboard fields.
# It now returns the sector/industry label, not the long ETF fund name.
def etf_full_name(etf: Any) -> str:
    return etf_sector_label(etf)


def sector_rotation_context(row: Dict[str, Any], regime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Current intraday sector-rotation proxy.

    This is not institutional order-flow. It ranks sector support using:
      - sector ETF same-day % change,
      - sector ETF relative strength versus SPY,
      - sector ETF relative strength versus QQQ,
      - stock performance versus its sector,
      - scanner-provided sector_status when available.

    The output is intentionally a small confidence adjustment, not a hard trade trigger.
    """
    regime = regime or {}

    etf = safe_str(row.get("sector_etf"), "SPY").upper()
    sector = safe_str(row.get("sector"), "Unknown")

    sector_change = safe_float(row.get("sector_change_pct"), 0.0)
    sector_vs_spy = safe_float(row.get("sector_vs_spy_pct"), None)
    if sector_vs_spy is None:
        sector_vs_spy = sector_change - safe_float(regime.get("spy_change"), 0.0)

    sector_vs_qqq_raw = row.get("sector_vs_qqq_pct")
    sector_vs_qqq = safe_float(sector_vs_qqq_raw, None)
    if sector_vs_qqq is None:
        sector_vs_qqq = sector_change - safe_float(regime.get("qqq_change"), 0.0)

    stock_vs_sector = safe_float(row.get("stock_vs_sector_pct"), 0.0)
    scanner_status = safe_str(row.get("sector_status"), "UNKNOWN").upper()

    # Weight ETF rotation more than single-stock outperformance.
    score = (
        0.60 * sector_change
        + 0.95 * sector_vs_spy
        + 0.45 * sector_vs_qqq
        + 0.18 * stock_vs_sector
    )

    if scanner_status in {"LEADING", "IMPROVING"}:
        score += 0.35
    elif scanner_status in {"WEAK", "ROTATION_OUT"}:
        score -= 0.55

    if score >= 1.25 and sector_change > 0:
        label = "STRONG_ROTATION"
        confidence_adjustment = 5.0
        dashboard_label = "Strong"
    elif score >= 0.45 and sector_change >= 0:
        label = "SUPPORTIVE_ROTATION"
        confidence_adjustment = 3.0
        dashboard_label = "Supportive"
    elif score <= -0.85 or (sector_change < -0.35 and sector_vs_spy < -0.25):
        label = "WEAK_ROTATION"
        confidence_adjustment = -5.0
        dashboard_label = "Weak"
    elif score <= -0.25:
        label = "SOFT_ROTATION"
        confidence_adjustment = -2.0
        dashboard_label = "Soft"
    else:
        label = "NEUTRAL_ROTATION"
        confidence_adjustment = 0.0
        dashboard_label = "Neutral"

    return {
        "sector": sector,
        "sector_etf": etf,
        "sector_etf_name": etf_full_name(etf),
        "sector_change_pct": round(sector_change, 2),
        "sector_vs_spy_pct": round(sector_vs_spy, 2),
        "sector_vs_qqq_pct": round(sector_vs_qqq, 2),
        "stock_vs_sector_pct": round(stock_vs_sector, 2),
        "sector_rotation_score": round(score, 2),
        "sector_rotation_label": label,
        "sector_rotation_display": dashboard_label,
        "sector_confidence_adjustment": confidence_adjustment,
    }


def sector_weak(row: Dict[str, Any]) -> bool:
    status = safe_str(row.get("sector_status"), "").upper()
    sector_score = safe_float(row.get("sector_score"), 0)
    rotation = sector_rotation_context(row, {})
    return (
        status in {"WEAK", "ROTATION_OUT"}
        or sector_score <= -3
        or safe_str(rotation.get("sector_rotation_label"), "") == "WEAK_ROTATION"
    )


def high_risk_extreme(row: Dict[str, Any]) -> bool:
    risk = safe_str(row.get("risk_category"), "").upper()
    bucket = safe_str(row.get("setup_bucket"), "").upper()
    return risk in {"HIGH_RISK_EXTREME", "EXTREME"} or bucket == "HIGH_RISK_EXTREME"


def is_earnings_reaction_row(row: Dict[str, Any]) -> bool:
    """
    True when scanner marks the candidate as an earnings reaction / earnings-day event.

    This does not automatically forbid a day trade. It means the setup must be
    treated as EARNINGS_REACTION_ONLY with stricter confirmation and warnings.
    """
    raw_flag = safe_str(row.get("is_earnings_reaction"), "").strip().lower()
    if raw_flag in {"true", "1", "yes", "y"}:
        return True

    text_fields = " ".join([
        safe_str(row.get("catalyst_label"), ""),
        safe_str(row.get("catalyst_headline"), ""),
        safe_str(row.get("risk_flags"), ""),
        safe_str(row.get("tags"), ""),
    ]).lower()

    if "earnings" in text_fields or "eps" in text_fields or "revenue" in text_fields:
        return True

    dte = safe_str(row.get("days_to_earnings"), "").strip().lower()
    return dte in {"0", "0.0", "today", "same day"}


def event_context_for_row(row: Dict[str, Any]) -> str:
    return "EARNINGS_REACTION" if is_earnings_reaction_row(row) else "NORMAL"


def earnings_reaction_requirements(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    plan: Dict[str, Any],
    confidence: float,
) -> Tuple[bool, List[str]]:
    """
    Earnings-day names are allowed only as reaction trades, not normal setups.

    The idea:
      - do not buy pre-earnings anticipation,
      - do not treat earnings names as ordinary VWAP reclaim/pullback,
      - allow only clear liquid post-reaction VWAP structure.
    """
    if not is_earnings_reaction_row(row):
        return True, []

    reasons: List[str] = []
    rr = safe_float(plan.get("reward_risk"), 0)

    if rr < MIN_RR_EARNINGS_REACTION:
        reasons.append(f"Earnings reaction requires R/R >= {MIN_RR_EARNINGS_REACTION:.1f}; current {rr:.2f}")

    if confidence < MIN_CONF_EARNINGS_READY:
        reasons.append(f"Earnings reaction requires confidence >= {MIN_CONF_EARNINGS_READY:.0f}; current {confidence:.1f}")

    if not metrics.above_vwap:
        reasons.append("Earnings reaction must be above VWAP")

    if not (
        metrics.recent_5m_dollar_volume >= MIN_EARNINGS_5M_DOLLAR_VOL_READY
        or metrics.avg_volume_1m_5 >= MIN_EARNINGS_1M_AVG_VOL_READY
    ):
        reasons.append(
            "Earnings reaction needs stronger live participation "
            f"(5m $vol ${metrics.recent_5m_dollar_volume:,.0f}, "
            f"1m avg vol {metrics.avg_volume_1m_5:.0f})"
        )

    if metrics.vwap_dist_pct > RECLAIM_PULLBACK_MAX_EXTENSION_PCT:
        reasons.append(f"Earnings reaction is too extended from VWAP ({metrics.vwap_dist_pct:.2f}%)")

    return len(reasons) == 0, reasons


def vwap_lifecycle_macd_confirmation(metrics: IntradayMetrics, setup_type: str) -> Tuple[bool, str]:
    """
    MACD logic for VWAP reclaim/pullback lifecycle.

    We do NOT invalidate a normal reclaim pullback only because MACD cools.
    We DO block when MACD rolls down hard while price is losing EMA9/VWAP pressure.
    """
    if metrics.bearish_momentum_divergence:
        return False, "Bearish MACD/momentum divergence"

    reclaim_family = setup_type in {
        "VWAP_EMA_RECLAIM_RUNNER",
        "VWAP_RECLAIM_BREAKOUT",
        "RECLAIM_PULLBACK_HOLDING",
        "VWAP_PULLBACK_CONTINUATION",
    }

    if reclaim_family:
        # Hard fail only when momentum and price structure both deteriorate.
        if (
            metrics.macd_bearish_crossover_recent
            and metrics.macd_histogram_falling
            and metrics.macd_1m_bearish_crossover_recent
            and (not metrics.price_above_ema9)
            and metrics.vwap_dist_pct <= 0.35
        ):
            return False, "Hard MACD failure: 5m and 1m bearish while price is pressing VWAP"

        if (
            metrics.macd_1m_curling_down
            and not metrics.macd_1m_above_signal
            and not metrics.price_above_ema9
            and metrics.vwap_dist_pct <= 0.20
        ):
            return False, "1m MACD curling down while price is below EMA9 near VWAP"

        if metrics.macd_1m_curling_up or metrics.macd_1m_bullish_crossover_recent:
            return True, "1m MACD curling up supports VWAP reclaim/pullback"

        if metrics.macd_histogram_rising or metrics.macd_above_signal:
            return True, "5m MACD acceptable for VWAP reclaim/pullback"

        # Flat/cooling MACD is allowed while reclaim support is holding.
        if metrics.reclaim_pullback_holding and metrics.above_vwap:
            return True, "MACD cooling but VWAP reclaim support still holding"

        return True, "MACD neutral; VWAP/reclaim structure still primary"

    return macd_ready_confirmation(metrics)


def is_truthy_value(value: Any) -> bool:
    return safe_str(value).strip().lower() in {"true", "1", "yes", "y"}


def scanner_marks_early_reclaim(row: Optional[Dict[str, Any]]) -> bool:
    if not row:
        return False
    intraday_setup = safe_str(row.get("intraday_setup_type"), "").upper()
    return (
        is_truthy_value(row.get("early_reclaim_runner"))
        or intraday_setup == "VWAP_EMA_RECLAIM_RUNNER"
    )


def setup_display_label(row: Dict[str, Any], setup_type: str) -> str:
    if is_earnings_reaction_row(row) and setup_type and setup_type not in {"MONITORING", "PREMARKET_MONITOR", "BLACKOUT_MONITOR"}:
        return f"EARNINGS_REACTION_{setup_type}"
    return setup_type or "MONITORING"


def is_reclaim_lifecycle_setup_type(setup_type: str) -> bool:
    return setup_type in {"VWAP_EMA_RECLAIM_RUNNER", "VWAP_RECLAIM_BREAKOUT", "RECLAIM_PULLBACK_HOLDING", "VWAP_PULLBACK_CONTINUATION"}


def is_relative_strength_long(row: Dict[str, Any], metrics: IntradayMetrics, regime: Dict[str, Any]) -> bool:
    if not metrics.has_data:
        return False

    spy = safe_float(regime.get("spy_change"), 0)
    qqq = safe_float(regime.get("qqq_change"), 0)
    iwm = safe_float(regime.get("iwm_change"), 0)

    benchmark = min(spy, qqq, iwm, 0)

    # Stock can be green, or at least outperform a sharply red benchmark by 2%.
    green = metrics.day_change_pct >= 0.40
    clear_outperformance = benchmark < 0 and metrics.day_change_pct >= benchmark + 2.0

    return (
        (green or clear_outperformance)
        and metrics.price > 0
        and vwap_condition_pass(metrics, safe_float(row.get("atr_pct"), 0))
        and not sector_weak(row)
        and not high_risk_extreme(row)
    )


def watch_criteria_pass(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    regime: Dict[str, Any],
) -> Tuple[bool, List[str], bool]:
    reasons: List[str] = []
    rs_long = is_relative_strength_long(row, metrics, regime)

    if high_risk_extreme(row):
        reasons.append("High risk/extreme category")
    if safe_float(row.get("dollar_vol_M"), 0) < MIN_AVG_DOLLAR_VOL_M:
        reasons.append("Dollar volume below $25M")
    if severe_risk_off(regime) and not rs_long:
        reasons.append("Market severe risk-off")
    if sector_weak(row):
        reasons.append("Sector weak")
    if not metrics.live_participation_ok:
        reasons.append(metrics.live_participation_reason)
    if not vwap_condition_pass(metrics, safe_float(row.get("atr_pct"), 0)):
        reasons.append("VWAP condition failed")
    if not rs_long and not hod_condition_pass(metrics, safe_str(row.get("signal_source_bucket"), "")):
        reasons.append("Too far below HOD")
    if metrics.vwap_dist_pct > MAX_VWAP_EXTENSION_PCT:
        reasons.append("Too extended above VWAP")

    return len(reasons) == 0, reasons, rs_long


# ==============================================================
# TRADE PLAN CONSTRUCTION — 5-MIN STRUCTURE
# ==============================================================

def trigger_level_for_setup(setup_type: str, metrics: IntradayMetrics) -> float:
    if setup_type == "VWAP_EMA_RECLAIM_RUNNER":
        # Early reclaim lane: entry should be near the VWAP/EMA reclaim base, not
        # a late HOD-style continuation trigger. This addresses NU-type setups
        # where the proper signal is the VWAP/EMA reclaim/hold area.
        base_candidates = [
            metrics.vwap,
            metrics.ema9,
            metrics.vwap_reclaim_support_level,
            metrics.vwap_reclaim_bar_close,
        ]
        base_candidates = [x for x in base_candidates if x and x > 0]
        if base_candidates:
            return max(base_candidates) * 1.0005
        level = max(metrics.vwap_reclaim_bar_high, metrics.price)
        return level * 1.0002 if level > 0 else 0.0
    if setup_type == "VWAP_RECLAIM_BREAKOUT":
        level = max(metrics.vwap_reclaim_bar_high, metrics.price)
        return level * 1.0002 if level > 0 else 0.0
    if setup_type == "RECLAIM_PULLBACK_HOLDING":
        level = max(metrics.pullback_high, metrics.vwap_reclaim_bar_high, metrics.price)
        return level * 1.0002 if level > 0 else 0.0
    if setup_type == "VWAP_PULLBACK_CONTINUATION":
        return metrics.pullback_high * 1.0002
    if setup_type == "BASE_SQUEEZE_BREAKOUT":
        return metrics.base_high * 1.0002
    if setup_type == "HOD_BASE_BREAKOUT":
        return max(metrics.hod, metrics.base_high) * 1.0002
    return max(metrics.pullback_high, metrics.base_high, metrics.hod, metrics.price) * 1.0002


def support_level_for_setup(setup_type: str, metrics: IntradayMetrics, entry: float) -> Tuple[float, str]:
    """
    Use only current 5-minute intraday structure.
    Do not use far-away daily support.
    """
    candidates: List[Tuple[float, str]] = []

    if setup_type in {"VWAP_EMA_RECLAIM_RUNNER", "VWAP_RECLAIM_BREAKOUT", "RECLAIM_PULLBACK_HOLDING"}:
        if metrics.vwap > 0:
            candidates.append((metrics.vwap, "VWAP reclaim support"))
        if metrics.vwap_reclaim_support_level > 0:
            candidates.append((metrics.vwap_reclaim_support_level, "VWAP reclaim lifecycle support"))
        if metrics.vwap_reclaim_bar_low > 0:
            candidates.append((metrics.vwap_reclaim_bar_low, "1Min VWAP reclaim candle low"))
        if metrics.pullback_low > 0:
            candidates.append((metrics.pullback_low, "5Min reclaim pullback low"))

    elif setup_type == "VWAP_PULLBACK_CONTINUATION":
        if metrics.pullback_low > 0:
            candidates.append((metrics.pullback_low, "5Min pullback high/low"))
        if metrics.base_low > 0:
            candidates.append((metrics.base_low, "5Min base low"))

    elif setup_type == "BASE_SQUEEZE_BREAKOUT":
        if metrics.base_low > 0:
            candidates.append((metrics.base_low, "5Min base high/low"))
        if metrics.pullback_low > 0:
            candidates.append((metrics.pullback_low, "5Min pullback low"))

    elif setup_type == "HOD_BASE_BREAKOUT":
        if metrics.base_low > 0:
            candidates.append((metrics.base_low, "5Min near-HOD base support"))
        if metrics.pullback_low > 0:
            candidates.append((metrics.pullback_low, "5Min pullback support"))

    else:
        if metrics.pullback_low > 0:
            candidates.append((metrics.pullback_low, "5Min pullback high/low"))
        if metrics.base_low > 0:
            candidates.append((metrics.base_low, "5Min base high/low"))

    # Keep only supports reasonably close to entry.
    filtered = []
    for level, label in candidates:
        if level > 0 and level < entry and pct(entry - level, entry) <= 3.0:
            filtered.append((level, label))

    if filtered:
        # Conservative but not broken: use closest valid structure support to avoid absurd stops.
        return max(filtered, key=lambda x: x[0])

    # No usable structure support.
    return 0.0, "No usable 5Min support within 3%"


def stop_buffer_pct(row: Dict[str, Any]) -> float:
    atr_pct = safe_float(row.get("atr_pct"), 0)

    if atr_pct <= 2.0:
        return 0.008
    if atr_pct <= 5.0:
        return 0.010
    return 0.012


def nearest_round_level_above(price: float) -> List[float]:
    if price <= 0:
        return []

    levels = []
    increments = [0.25, 0.5, 1.0] if price < 20 else [0.5, 1.0, 2.5, 5.0]

    for inc in increments:
        level = math.ceil(price / inc) * inc
        if level > price * 1.001:
            levels.append(level)

    return sorted(set(round(x, 4) for x in levels))


def resistance_levels_above(entry: float, metrics: IntradayMetrics, row: Dict[str, Any]) -> List[float]:
    levels: List[float] = []

    for level in [
        metrics.hod,
        metrics.base_high,
        safe_float(row.get("premarket_high"), 0),
        safe_float(row.get("prior_day_high"), 0),
        safe_float(row.get("resistance"), 0),
    ]:
        if level > entry * 1.001:
            levels.append(level)

    levels.extend(nearest_round_level_above(entry))

    # Extension levels above VWAP.
    if metrics.vwap > 0:
        for ext in [1.0, 1.5, 2.0, 3.0, 4.0]:
            level = metrics.vwap * (1 + ext / 100.0)
            if level > entry * 1.001:
                levels.append(level)

    return sorted(set(round(x, 4) for x in levels if x > entry * 1.001))


def pick_target_1(entry: float, risk: float, metrics: IntradayMetrics, row: Dict[str, Any]) -> Tuple[float, float]:
    min_target = entry + MIN_RR * risk
    levels = resistance_levels_above(entry, metrics, row)
    usable = [x for x in levels if x >= min_target]

    if usable:
        return min(usable), min_target

    return min_target, min_target


def build_trade_plan(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    setup_type: str,
    quote: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build entry, stop, targets, and R/R using 5-minute structure.
    Invalid plans remain diagnostics only and cannot become WATCH.
    """
    price = metrics.price or safe_float(row.get("price"), 0)

    entry = trigger_level_for_setup(setup_type, metrics)
    if entry <= 0 and price > 0:
        entry = price * 1.0002

    support, structure_label = support_level_for_setup(setup_type, metrics, entry)

    valid = True
    rejection_reason = ""

    if entry <= 0 or support <= 0:
        valid = False
        rejection_reason = "No usable 5Min structure support"
        stop = 0.0
        risk = 0.0
        stop_distance_pct = 999.0
        target_1 = 0.0
        target_2 = 0.0
        rr = 0.0
        min_target_1 = 0.0
    else:
        buffer = stop_buffer_pct(row)
        spread = quote_spread_dollars(quote or {})

        # Breathing room under real 5-min support.
        stop = support * (1 - buffer)

        # If spread is wider than the normal buffer, add it as extra dollars.
        if spread is not None and spread > 0 and price > 0:
            spread_pct = spread / price
            if spread_pct > buffer:
                stop = support - (2 * spread)

        risk = entry - stop
        stop_distance_pct = pct(risk, entry) if entry > 0 else 999.0

        max_stop = MAX_STOP_DIST_HOD if setup_type == "HOD_BASE_BREAKOUT" else MAX_STOP_DIST_NORMAL

        if entry <= 0 or stop <= 0 or risk <= 0:
            valid = False
            rejection_reason = "Invalid entry/stop"
        elif stop_distance_pct > max_stop:
            valid = False
            rejection_reason = f"Stop distance {stop_distance_pct:.2f}% > max {max_stop:.1f}%"

        target_1, min_target_1 = pick_target_1(entry, risk, metrics, row)
        target_2 = max(entry + 2.0 * risk, target_1 + 0.5 * risk)

        rr = (target_1 - entry) / risk if risk > 0 else 0.0

        if valid and rr < MIN_RR:
            valid = False
            rejection_reason = "Target 1 R/R below 1.5"

    return {
        "valid": valid,
        "rejection_reason": rejection_reason,
        "entry_trigger": round(entry, 4),
        "stop_loss": round(stop, 4),
        "target_1": round(target_1, 4),
        "target_2": round(target_2, 4),
        "reward_risk": round(rr, 2),
        "stop_distance_pct": round(stop_distance_pct, 2),
        "support_level": round(support, 4),
        "structure_label": structure_label,
        "min_target_1": round(min_target_1, 4),
        "buffer_pct_used": round(stop_buffer_pct(row) * 100, 2),
        "spread_dollars": round(quote_spread_dollars(quote or {}), 4) if quote_spread_dollars(quote or {}) is not None else None,
    }


def choose_best_provisional_plan(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    quote: Dict[str, Any],
) -> Tuple[str, Dict[str, Any]]:
    """
    Build all setup plans and choose the best valid one.
    Invalid plans are kept only if no valid plan exists, for diagnostics.
    """
    setup_order = [
        "VWAP_RECLAIM_BREAKOUT",
        "VWAP_PULLBACK_CONTINUATION",
        "HOD_BASE_BREAKOUT",
        "BASE_SQUEEZE_BREAKOUT",
    ]
    if scanner_marks_early_reclaim(row):
        setup_order.insert(0, "VWAP_EMA_RECLAIM_RUNNER")

    plans = []
    for setup in setup_order:
        plan = build_trade_plan(row, metrics, setup, quote)
        plans.append((setup, plan))

    valid_plans = [(s, p) for s, p in plans if p.get("valid") and safe_float(p.get("reward_risk"), 0) >= MIN_RR_WATCH]

    if valid_plans:
        # Prefer better R/R, but keep setup order as tie-break.
        valid_plans.sort(key=lambda x: (-safe_float(x[1].get("reward_risk"), 0), setup_order.index(x[0])))
        return valid_plans[0]

    # Return the "least bad" plan for diagnostics.
    plans.sort(key=lambda x: (-safe_float(x[1].get("reward_risk"), 0), safe_float(x[1].get("stop_distance_pct"), 999)))
    return plans[0]


# ==============================================================
# SETUP READINESS
# ==============================================================


def ema9_confirmation_score(metrics: IntradayMetrics) -> float:
    """
    EMA9 is a confirmation layer, not a primary WATCH trigger.
    Positive alignment improves score. Bad alignment penalizes later.
    """
    if metrics.ema9 <= 0:
        return 0.0

    score = 0.0
    if metrics.price_above_ema9:
        score += 3.0
    if metrics.ema9_rising:
        score += 2.0
    if metrics.ema9_above_vwap:
        score += 3.0
    if metrics.ema9_crossed_above_vwap_recent:
        score += 4.0

    return round(min(EMA9_BULLISH_BONUS_MAX, score), 2)



def macd_confirmation_score(metrics: IntradayMetrics) -> float:
    """
    MACD is a confirmation booster, not a primary trigger.
    Good MACD can lift borderline late-day candidates; bearish MACD reduces quality.
    """
    score = 0.0

    if metrics.macd_bullish_crossover_recent:
        score += 5.0

    if metrics.macd_above_signal:
        score += 3.0

    if metrics.macd_histogram_rising:
        score += 2.0

    if metrics.macd_above_zero:
        score += 1.0

    if metrics.macd_bearish_crossover_recent:
        score -= 5.0

    if metrics.macd_histogram_falling and not metrics.macd_above_signal:
        score -= 3.0

    if metrics.bearish_momentum_divergence:
        score -= 8.0

    # Faster 1-minute MACD is especially useful for VWAP reclaim/pullback timing.
    if metrics.macd_1m_bullish_crossover_recent:
        score += 2.0
    elif metrics.macd_1m_curling_up:
        score += 1.5

    if metrics.macd_1m_bearish_crossover_recent and not metrics.macd_1m_above_signal:
        score -= 2.0
    elif metrics.macd_1m_curling_down and not metrics.macd_1m_above_signal:
        score -= 1.0

    return round(clamp(score, -8.0, MACD_BULLISH_BONUS_MAX), 2)


def macd_ready_confirmation(metrics: IntradayMetrics) -> Tuple[bool, str]:
    """
    MACD should not be a hard requirement for WATCH.
    For READY, bearish MACD conditions are blockers; bullish MACD is a bonus through scoring.
    """
    if metrics.bearish_momentum_divergence:
        return False, "Bearish MACD/momentum divergence"

    if metrics.macd_bearish_crossover_recent and metrics.macd_histogram_falling:
        return False, "Bearish MACD crossover / histogram weakening"

    return True, "MACD confirmation acceptable"


def ema9_ready_confirmation(metrics: IntradayMetrics, late_day: Optional[bool] = None) -> Tuple[bool, str]:
    """
    Trigger Ready should have EMA9 confirmation.
    WATCH does not require it, because a valid VWAP pullback may still be forming.
    """
    if not metrics.has_data:
        return False, "No intraday bars"

    if metrics.ema9 <= 0:
        return True, "EMA9 unavailable"

    if not metrics.price_above_ema9:
        return False, "Price below EMA9"

    if metrics.ema9_falling:
        return False, "EMA9 falling"

    if late_day is None:
        late_day = is_late_day()

    if late_day:
        if not (metrics.ema9_above_vwap or metrics.ema9_crossed_above_vwap_recent):
            return False, "Late-day setup needs EMA9 above VWAP or recent EMA9/VWAP bullish cross"

    return True, "EMA9 confirmation valid"


def setup_ready_conf_required(setup_type: str, phase: str) -> float:
    """
    VWAP reclaim breakouts are allowed to become Trigger Ready at a lower
    confidence threshold because the edge is the fresh reclaim candle itself.
    Other setup types keep the normal strict ready threshold.
    """
    if setup_type in {"VWAP_EMA_RECLAIM_RUNNER", "VWAP_RECLAIM_BREAKOUT"}:
        return MIN_CONF_RECLAIM_READY
    if setup_type == "RECLAIM_PULLBACK_HOLDING":
        return MIN_CONF_RECLAIM_PULLBACK_READY
    return ready_confidence_required(phase)


def recent_execution_bars(metrics: IntradayMetrics, count: int = 3) -> List[Dict[str, Any]]:
    bars = clean_bars(metrics.execution_bars or [])
    if count <= 0:
        return bars
    return bars[-count:]


def ready_trigger_distance_pct(metrics: IntradayMetrics, setup_type: str) -> Tuple[float, float]:
    """
    Return (trigger, distance_pct) where distance_pct is how far the locked
    breakout trigger is above current price. Positive means price is below the
    trigger; negative means price is already above it.
    """
    trigger = trigger_level_for_setup(setup_type, metrics)
    if trigger <= 0 or metrics.price <= 0:
        return trigger, 999.0
    return trigger, pct(trigger - metrics.price, metrics.price)


def ready_trigger_proximity_ok(metrics: IntradayMetrics, setup_type: str) -> Tuple[bool, str]:
    """
    TRIGGER_READY should mean the setup is close enough to the planned breakout
    trigger to be actionable. If the trigger is still far above current price,
    keep it as WATCH.

    This prevents cases like UMC:
      - VWAP near current price
      - breakout trigger much higher
      - system incorrectly labels it ready before VWAP hold / micro reclaim is proven.
    """
    trigger, distance_pct = ready_trigger_distance_pct(metrics, setup_type)
    if trigger <= 0:
        return False, "No valid breakout trigger"

    max_distance = READY_TRIGGER_MAX_DISTANCE_PCT
    if metrics.recent_volume_expanding and not metrics.volume_fading_vs_morning:
        max_distance = READY_TRIGGER_MAX_DISTANCE_STRONG_VOLUME_PCT

    if distance_pct > max_distance:
        return (
            False,
            f"Trigger {trigger:.2f} is {distance_pct:.2f}% above current price; "
            "keep WATCH until VWAP/EMA hold forms closer to trigger",
        )

    # If price is already materially above the trigger before a fresh state is
    # created, do not mark it ready as a fresh entry. The process_new_or_watch
    # path will convert a recent touch into TRIGGER_TOUCHED when appropriate.
    if distance_pct < -0.75:
        return False, f"Price already extended {abs(distance_pct):.2f}% above trigger; no fresh Trigger Ready"

    return True, f"Trigger proximity acceptable: {distance_pct:.2f}% from entry"


def vwap_hold_confirmed_for_ready(metrics: IntradayMetrics, setup_type: str) -> Tuple[bool, str]:
    """
    Confirm that VWAP/EMA support is actually holding before allowing a
    VWAP/reclaim setup to become TRIGGER_READY.

    WATCH = pullback/reclaim forming.
    TRIGGER_READY = hold/reclaim confirmed and trigger is nearby.
    ACTIVE_SIGNAL = trigger breaks and later confirms.
    """
    if not metrics.has_data:
        return False, "No intraday bars"

    if not metrics.above_vwap:
        return False, "Price is not above VWAP"

    bars = recent_execution_bars(metrics, max(3, VWAP_READY_MIN_HOLD_BARS))
    if len(bars) < VWAP_READY_MIN_HOLD_BARS:
        return False, "Not enough recent 1-minute bars to confirm VWAP hold"

    recent = bars[-VWAP_READY_MIN_HOLD_BARS:]
    closes = [safe_float(b.get("c"), 0) for b in recent]
    lows = [safe_float(b.get("l"), 0) for b in recent]
    opens = [safe_float(b.get("o"), safe_float(b.get("c"), 0)) for b in recent]

    if metrics.vwap > 0:
        close_holds = sum(1 for c in closes if c >= metrics.vwap)
        low_buffer = metrics.vwap * (1.0 - VWAP_READY_VWAP_LOW_BUFFER_PCT / 100.0)
        low_holds = sum(1 for low in lows if low >= low_buffer)
        if close_holds < VWAP_READY_MIN_HOLD_BARS:
            return False, f"VWAP hold not confirmed: only {close_holds}/{VWAP_READY_MIN_HOLD_BARS} recent closes above VWAP"
        if low_holds < max(1, VWAP_READY_MIN_HOLD_BARS - 1):
            return False, "Recent pullback is undercutting VWAP support"

    if metrics.ema9 > 0:
        if not metrics.price_above_ema9:
            return False, "Price has not reclaimed EMA9"
        if metrics.ema9_falling and not metrics.recent_volume_expanding:
            return False, "EMA9 falling; wait for higher-low/reclaim confirmation"

    green_or_flat = 0
    for o, c in zip(opens, closes):
        if c >= o or (metrics.vwap > 0 and c >= metrics.vwap):
            green_or_flat += 1

    if green_or_flat < VWAP_READY_MIN_GREEN_OR_FLAT_BARS:
        return False, "Recent 1-minute candles do not show VWAP hold / stabilization"

    if metrics.macd_1m_bearish_crossover_recent and metrics.macd_1m_histogram_falling:
        return False, "1-minute MACD bearish after VWAP/reclaim attempt"

    if (
        metrics.macd_1m_curling_down
        and metrics.macd_1m_histogram_falling
        and not metrics.recent_volume_expanding
    ):
        return False, "1-minute MACD curling down; wait for reclaim/hold"

    if metrics.volume_fading_vs_morning and not metrics.recent_volume_expanding:
        return False, volume_fade_label(metrics)

    return True, "VWAP/EMA hold confirmed by recent 1-minute structure"


def vwap_lifecycle_ready_confirmation(metrics: IntradayMetrics, setup_type: str) -> Tuple[bool, str]:
    """
    Shared pre-TRIGGER_READY filter for VWAP reclaim / pullback lifecycle setups.
    It combines:
      1. actual VWAP/EMA hold,
      2. nearby trigger,
      3. no immediate 1-minute momentum failure.
    """
    hold_ok, hold_reason = vwap_hold_confirmed_for_ready(metrics, setup_type)
    if not hold_ok:
        return False, hold_reason

    proximity_ok, proximity_reason = ready_trigger_proximity_ok(metrics, setup_type)
    if not proximity_ok:
        return False, proximity_reason

    return True, f"{hold_reason}; {proximity_reason}"


def setup_vwap_ema_reclaim_runner_ready(
    metrics: IntradayMetrics,
    rr: float,
    confidence: float,
    row: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Scanner-provided early VWAP/EMA reclaim runner lifecycle.

    This is distinct from a generic VWAP pullback:
      - the scanner first identifies a 1m/5m reclaim lane,
      - signal_engine then requires real VWAP/EMA hold before Trigger Ready,
      - entry is built near the reclaim base instead of a late continuation high.
    """
    if not scanner_marks_early_reclaim(row):
        return False, "Scanner did not mark early VWAP/EMA reclaim runner"

    if not metrics.has_data:
        return False, "No intraday bars"

    if not metrics.above_vwap:
        return False, "Early reclaim runner is below VWAP"

    if metrics.vwap_dist_pct > VWAP_RECLAIM_MAX_EXTENSION_PCT:
        return False, f"Early reclaim already extended {metrics.vwap_dist_pct:.2f}% > {VWAP_RECLAIM_MAX_EXTENSION_PCT:.1f}%"

    if metrics.ema9 > 0 and not metrics.price_above_ema9:
        return False, "Early reclaim runner has not held EMA9"

    lifecycle_ok, lifecycle_reason = vwap_lifecycle_ready_confirmation(metrics, "VWAP_EMA_RECLAIM_RUNNER")
    if not lifecycle_ok:
        return False, lifecycle_reason

    macd_ok, macd_reason = vwap_lifecycle_macd_confirmation(metrics, "VWAP_EMA_RECLAIM_RUNNER")
    if not macd_ok:
        return False, macd_reason

    if rr < MIN_RR:
        return False, "R/R below 1.5"

    required_conf = MIN_CONF_RECLAIM_READY
    if confidence < required_conf:
        return False, f"Confidence below early reclaim minimum {required_conf:.0f}"

    quality = safe_str((row or {}).get("vwap_reclaim_quality_label"), "")
    score = safe_float((row or {}).get("early_reclaim_score"), 0)
    suffix = f"; scanner early reclaim score {score:.0f}" if score else ""
    if quality:
        suffix += f"; {quality}"

    return True, f"Early VWAP/EMA reclaim runner confirmed by VWAP/EMA hold{suffix}"


def setup_vwap_reclaim_ready(metrics: IntradayMetrics, rr: float, confidence: float) -> Tuple[bool, str]:
    """
    Detect high-volume VWAP reclaim from below.

    This is the missing setup type exposed by SMCI:
      below VWAP -> strong reclaim candle -> holds above VWAP -> continuation.

    It is intentionally time-sensitive. If the reclaim is old or already
    extended, it should remain WATCH / MISSED_WINDOW, not Trigger Ready.
    """
    if not metrics.has_data:
        return False, "No intraday bars"

    if not metrics.above_vwap:
        return False, "Below VWAP"

    if not metrics.vwap_reclaim_recent:
        return False, metrics.vwap_reclaim_reason or "No recent VWAP reclaim"

    if not metrics.vwap_reclaim_ready:
        return False, metrics.vwap_reclaim_reason or "VWAP reclaim not ready"

    if metrics.vwap_reclaim_age_minutes < RECLAIM_READY_MIN_HOLD_MINUTES:
        return False, (
            f"VWAP reclaim is too fresh ({metrics.vwap_reclaim_age_minutes:.1f}m); "
            "waiting for at least one hold/reclaim confirmation candle"
        )

    if metrics.vwap_dist_pct > VWAP_RECLAIM_MAX_EXTENSION_PCT:
        return False, f"VWAP reclaim already extended {metrics.vwap_dist_pct:.2f}% > {VWAP_RECLAIM_MAX_EXTENSION_PCT:.1f}%"

    lifecycle_ok, lifecycle_reason = vwap_lifecycle_ready_confirmation(metrics, "VWAP_RECLAIM_BREAKOUT")
    if not lifecycle_ok:
        return False, lifecycle_reason

    if not metrics.live_participation_ready_ok:
        return False, metrics.live_participation_reason

    if metrics.ema9 > 0:
        # Fresh VWAP reclaim often happens before the 5-minute EMA9 fully catches up.
        # Do not block the setup for a temporary EMA9 dip if VWAP/reclaim support is holding.
        if not metrics.price_above_ema9 and not metrics.reclaim_pullback_holding:
            return False, "Price below EMA9 after VWAP reclaim"
        if metrics.ema9_falling and not metrics.reclaim_pullback_holding:
            return False, "EMA9 falling after VWAP reclaim"

    macd_ok, macd_reason = vwap_lifecycle_macd_confirmation(metrics, "VWAP_RECLAIM_BREAKOUT")
    if not macd_ok:
        return False, macd_reason

    if rr < MIN_RR:
        return False, "R/R below 1.5"

    required_conf = MIN_CONF_RECLAIM_READY
    if confidence < required_conf:
        return False, f"Confidence below reclaim minimum {required_conf:.0f}"

    return True, metrics.vwap_reclaim_reason or "High-volume VWAP reclaim breakout"


def setup_reclaim_pullback_ready(metrics: IntradayMetrics, rr: float, confidence: float) -> Tuple[bool, str]:
    """
    Second phase of the common day-trade lifecycle:
      VWAP_RECLAIM_BREAKOUT -> pullback toward EMA9/VWAP -> continuation.

    A reclaim pullback should NOT be invalidated merely because price briefly
    dips below EMA9. It remains alive while VWAP/reclaim support is holding and
    R/R is still valid.
    """
    if not metrics.has_data:
        return False, "No intraday bars"

    if not metrics.vwap_reclaim_lifecycle_active:
        return False, metrics.reclaim_pullback_reason or "No active VWAP reclaim lifecycle"

    if not metrics.above_vwap:
        return False, "Reclaim pullback below VWAP"

    if not metrics.reclaim_pullback_holding:
        return False, metrics.reclaim_pullback_reason or "Reclaim pullback not holding"

    if metrics.vwap_dist_pct > RECLAIM_PULLBACK_MAX_EXTENSION_PCT:
        return False, f"Reclaim pullback extended {metrics.vwap_dist_pct:.2f}% > {RECLAIM_PULLBACK_MAX_EXTENSION_PCT:.1f}%"

    lifecycle_ok, lifecycle_reason = vwap_lifecycle_ready_confirmation(metrics, "RECLAIM_PULLBACK_HOLDING")
    if not lifecycle_ok:
        return False, lifecycle_reason

    if not metrics.live_participation_ready_ok:
        return False, metrics.live_participation_reason

    macd_ok, macd_reason = vwap_lifecycle_macd_confirmation(metrics, "RECLAIM_PULLBACK_HOLDING")
    if not macd_ok:
        return False, macd_reason

    if rr < MIN_RR:
        return False, "R/R below 1.5"

    required_conf = MIN_CONF_RECLAIM_PULLBACK_READY
    if confidence < required_conf:
        return False, f"Confidence below reclaim-pullback minimum {required_conf:.0f}"

    return True, metrics.reclaim_pullback_reason or "VWAP reclaim pullback holding; waiting for continuation trigger"


def setup_vwap_pullback_ready(metrics: IntradayMetrics, rr: float, confidence: float) -> Tuple[bool, str]:
    if not metrics.has_data:
        return False, "No intraday bars"

    if not metrics.above_vwap:
        return False, "Below VWAP"

    ema_ok, ema_reason = ema9_ready_confirmation(metrics)
    if not ema_ok:
        return False, ema_reason

    macd_ok, macd_reason = vwap_lifecycle_macd_confirmation(metrics, "VWAP_PULLBACK_CONTINUATION")
    if not macd_ok:
        return False, macd_reason

    # For ordinary VWAP pullbacks, prefer MACD curling up / above signal.
    # For reclaim-lifecycle pullbacks, neutral MACD is acceptable while support holds.
    if (
        not metrics.reclaim_pullback_holding
        and metrics.macd_1m_curling_down
        and not metrics.macd_1m_above_signal
        and not metrics.macd_histogram_rising
    ):
        return False, "VWAP pullback MACD still curling down"

    if metrics.volume_fading_vs_morning:
        return False, volume_fade_label(metrics)

    if is_late_day() and not late_day_volume_confirmed(metrics):
        return False, "Late-day setup needs volume expansion"

    if not metrics.pullback_holding_vwap:
        return False, "No clean 5-min VWAP hold"

    lifecycle_ok, lifecycle_reason = vwap_lifecycle_ready_confirmation(metrics, "VWAP_PULLBACK_CONTINUATION")
    if not lifecycle_ok:
        return False, lifecycle_reason

    if metrics.volume_drying and not metrics.base_volume_constructive:
        return False, "Pullback volume not constructive"

    # Do not punish old/opening VWAP noise. Only block weak recent VWAP retests.
    if metrics.vwap_failed_touch_count >= 2:
        return False, "Repeated recent failed VWAP retests"

    if metrics.vwap_recent_touch_count >= 4 and metrics.vwap_touch_status == "MANY_TOUCHES_WITH_WEAK_STRUCTURE":
        return False, "4th+ recent VWAP touches with weak structure"

    if rr < MIN_RR:
        return False, "R/R below 1.5"

    required_conf = ready_confidence_required("VALID_AFTERNOON" if is_late_day() else "VALID_MORNING")
    if confidence < required_conf:
        return False, f"Confidence below {required_conf:.0f}"

    return True, "5-min VWAP pullback holding; waiting for break above pullback high"


def base_squeeze_not_ready_reason(metrics: IntradayMetrics, rr: float, confidence: float) -> str:
    reasons = []

    if not metrics.has_data:
        reasons.append("No intraday bars")
    if not metrics.above_vwap:
        reasons.append("Below VWAP")
    ema_ok, ema_reason = ema9_ready_confirmation(metrics)
    if not ema_ok:
        reasons.append(ema_reason)
    if metrics.bearish_momentum_divergence:
        reasons.append("bearish MACD/momentum divergence")
    if metrics.volume_fading_vs_morning:
        reasons.append("volume fading vs morning reference")
    if is_late_day() and not late_day_volume_confirmed(metrics):
        reasons.append("late-day setup needs volume expansion")
    if metrics.base_range_pct > 2.0:
        reasons.append(f"base range {metrics.base_range_pct:.2f}% > 2.0%")
    if not metrics.higher_low_or_flat_base:
        reasons.append("no higher-low/flat-base structure")
    if not metrics.base_volume_constructive:
        reasons.append("base volume not contracting/stable")
    if not metrics.price_near_base_breakout:
        reasons.append("price not close to base breakout")
    if metrics.hod > 0 and pct_change(metrics.base_low, metrics.hod) < -2.5:
        reasons.append("Base too far from HOD")
    if rr < MIN_RR:
        reasons.append("R/R below 1.5")
    required_conf = ready_confidence_required("VALID_AFTERNOON" if is_late_day() else "VALID_MORNING")
    if confidence < required_conf:
        reasons.append(f"Confidence below {required_conf:.0f}")

    return "; ".join(reasons) if reasons else "Base/flag squeeze not ready"


def setup_base_squeeze_ready(metrics: IntradayMetrics, rr: float, confidence: float) -> Tuple[bool, str]:
    if not metrics.has_data:
        return False, "No intraday bars"

    if not metrics.above_vwap:
        return False, "Below VWAP"

    ema_ok, ema_reason = ema9_ready_confirmation(metrics)
    if not ema_ok:
        return False, ema_reason

    macd_ok, macd_reason = macd_ready_confirmation(metrics)
    if not macd_ok:
        return False, macd_reason

    if metrics.volume_fading_vs_morning:
        return False, volume_fade_label(metrics)

    if is_late_day() and not late_day_volume_confirmed(metrics):
        return False, "Late-day setup needs volume expansion"

    if not metrics.base_compression:
        return False, f"No clean base/flag compression: {base_squeeze_not_ready_reason(metrics, rr, confidence)}"

    if metrics.hod > 0 and pct_change(metrics.base_low, metrics.hod) < -2.5:
        return False, "Base too far from HOD"

    if rr < MIN_RR:
        return False, "R/R below 1.5"

    required_conf = ready_confidence_required("VALID_AFTERNOON" if is_late_day() else "VALID_MORNING")
    if confidence < required_conf:
        return False, f"Confidence below {required_conf:.0f}"

    return True, "5-min base/flag squeeze; waiting for break above compression high"


def hod_base_structure_not_ready_reason(metrics: IntradayMetrics) -> str:
    """
    Explain why a near-HOD idea is not a valid HOD base breakout.

    Trader logic:
      - A vertical push into HOD is not a breakout setup.
      - A valid HOD breakout first needs a base/flag near HOD with higher lows
        or tight compression, then a break with confirmation.
    """
    reasons = []

    if not metrics.has_data:
        reasons.append("No intraday bars")

    if metrics.hod <= 0:
        reasons.append("HOD unavailable")
    elif metrics.hod_distance_pct < HOD_BASE_MAX_DISTANCE_FROM_HOD_PCT:
        reasons.append(f"Not close enough to HOD ({metrics.hod_distance_pct:.2f}%)")

    if metrics.vwap_dist_pct > HOD_BASE_MAX_VWAP_EXTENSION_PCT:
        reasons.append(
            f"Too extended above VWAP for HOD base ({metrics.vwap_dist_pct:.2f}% > {HOD_BASE_MAX_VWAP_EXTENSION_PCT:.1f}%)"
        )

    if not metrics.consolidating_near_high:
        reasons.append("No controlled consolidation near HOD; likely vertical HOD tap / chase risk")

    if not metrics.base_compression:
        reasons.append("No tight base/flag under HOD")

    if metrics.base_range_pct > HOD_BASE_MAX_RANGE_PCT:
        reasons.append(f"Near-HOD base range too wide ({metrics.base_range_pct:.2f}% > {HOD_BASE_MAX_RANGE_PCT:.1f}%)")

    if metrics.structure_bar_count and metrics.structure_bar_count < HOD_BASE_MIN_STRUCTURE_BARS:
        reasons.append(f"Near-HOD base too young ({metrics.structure_bar_count} bars < {HOD_BASE_MIN_STRUCTURE_BARS})")

    if not metrics.higher_low_or_flat_base:
        reasons.append("No higher-low / flat-base structure under HOD")

    if not metrics.base_volume_constructive:
        reasons.append("Base volume not constructive")

    if metrics.hod > 0 and metrics.base_low > 0 and pct_change(metrics.base_low, metrics.hod) < HOD_BASE_MAX_LOW_FROM_HOD_PCT:
        reasons.append("Base low too far below HOD")

    if not metrics.price_near_base_breakout:
        reasons.append("Price not near base/HOD breakout level")

    return "; ".join(reasons) if reasons else "HOD base breakout structure not ready"


def setup_hod_base_breakout_ready(metrics: IntradayMetrics, rr: float, confidence: float) -> Tuple[bool, str]:
    """
    HOD breakout is a trigger AFTER a base forms, not a separate vertical-chase setup.

    Valid lifecycle:
      BASE_NEAR_HOD -> HOD_BASE_BREAKOUT -> TRIGGER_TOUCHED -> ACTIVE_SIGNAL

    Invalid:
      several large candles run into HOD, tap/cross it, then fade. That is
      EXTENDED_HOD_TAP / CHASE_RISK, not a Trigger Ready HOD breakout.
    """
    if not metrics.has_data:
        return False, "No intraday bars"

    if not metrics.above_vwap:
        return False, "Below VWAP"

    ema_ok, ema_reason = ema9_ready_confirmation(metrics)
    if not ema_ok:
        return False, ema_reason

    macd_ok, macd_reason = macd_ready_confirmation(metrics)
    if not macd_ok:
        return False, macd_reason

    if metrics.volume_fading_vs_morning:
        return False, volume_fade_label(metrics)

    if is_late_day() and not late_day_volume_confirmed(metrics):
        return False, "Late-day setup needs volume expansion"

    structure_reason = hod_base_structure_not_ready_reason(metrics)
    if structure_reason != "HOD base breakout structure not ready":
        return False, structure_reason

    if rr < MIN_RR:
        return False, "R/R below 1.5"

    required_conf = ready_confidence_required("VALID_AFTERNOON" if is_late_day() else "VALID_MORNING")
    if confidence < required_conf:
        return False, f"Confidence below {required_conf:.0f}"

    return True, "Near-HOD base/flag formed; waiting for break above HOD/base high with volume"


def setup_hod_breakout_ready(metrics: IntradayMetrics, rr: float, confidence: float) -> Tuple[bool, str]:
    """
    Backward-compatible wrapper. Standalone HOD taps are intentionally not valid.
    Use HOD_BASE_BREAKOUT only.
    """
    return setup_hod_base_breakout_ready(metrics, rr, confidence)


def choose_setup(metrics: IntradayMetrics, plan: Dict[str, Any], confidence: float, phase: str, row: Optional[Dict[str, Any]] = None) -> Tuple[str, str, List[str]]:
    rr = safe_float(plan.get("reward_risk"), 0)
    reasons = []
    row = row or {}

    def _event_allowed(candidate_setup: str) -> Tuple[bool, str]:
        ok, event_reasons = earnings_reaction_requirements(row, metrics, plan, confidence)
        if ok:
            return True, ""
        return False, "; ".join(event_reasons)

    early_reclaim_ready, early_reclaim_reason = setup_vwap_ema_reclaim_runner_ready(metrics, rr, confidence, row)
    if early_reclaim_ready:
        event_ok, event_reason = _event_allowed("VWAP_EMA_RECLAIM_RUNNER")
        if event_ok:
            return "VWAP_EMA_RECLAIM_RUNNER", early_reclaim_reason, reasons
        reasons.append(f"Earnings reaction filter: {event_reason}")
    if scanner_marks_early_reclaim(row):
        reasons.append(f"Early VWAP/EMA reclaim not ready: {early_reclaim_reason}")

    reclaim_ready, reclaim_reason = setup_vwap_reclaim_ready(metrics, rr, confidence)
    if reclaim_ready:
        event_ok, event_reason = _event_allowed("VWAP_RECLAIM_BREAKOUT")
        if event_ok:
            return "VWAP_RECLAIM_BREAKOUT", reclaim_reason, reasons
        reasons.append(f"Earnings reaction filter: {event_reason}")
    reasons.append(f"VWAP reclaim not ready: {reclaim_reason}")

    reclaim_pullback_ready, reclaim_pullback_reason = setup_reclaim_pullback_ready(metrics, rr, confidence)
    if reclaim_pullback_ready:
        event_ok, event_reason = _event_allowed("RECLAIM_PULLBACK_HOLDING")
        if event_ok:
            return "RECLAIM_PULLBACK_HOLDING", reclaim_pullback_reason, reasons
        reasons.append(f"Earnings reaction filter: {event_reason}")
    reasons.append(f"Reclaim pullback not ready: {reclaim_pullback_reason}")

    vwap_ready, vwap_reason = setup_vwap_pullback_ready(metrics, rr, confidence)
    if vwap_ready:
        event_ok, event_reason = _event_allowed("VWAP_PULLBACK_CONTINUATION")
        if event_ok:
            return "VWAP_PULLBACK_CONTINUATION", vwap_reason, reasons
        reasons.append(f"Earnings reaction filter: {event_reason}")
    reasons.append(f"VWAP pullback not ready: {vwap_reason}")

    hod_base_ready, hod_base_reason = setup_hod_base_breakout_ready(metrics, rr, confidence)
    if hod_base_ready:
        event_ok, event_reason = _event_allowed("HOD_BASE_BREAKOUT")
        if event_ok:
            return "HOD_BASE_BREAKOUT", hod_base_reason, reasons
        reasons.append(f"Earnings reaction filter: {event_reason}")
    reasons.append(f"HOD base breakout not ready: {hod_base_reason}")

    base_ready, base_reason = setup_base_squeeze_ready(metrics, rr, confidence)
    if base_ready:
        # If this is actually a near-HOD base but failed HOD_BASE_BREAKOUT,
        # do not relabel it as a generic base squeeze. It needs a proper HOD
        # base break or a fresh non-HOD base.
        if metrics.hod > 0 and metrics.hod_distance_pct >= HOD_BASE_MAX_DISTANCE_FROM_HOD_PCT and metrics.consolidating_near_high:
            reasons.append("Base/flag near HOD requires HOD_BASE_BREAKOUT confirmation; not using generic base label")
        else:
            event_ok, event_reason = _event_allowed("BASE_SQUEEZE_BREAKOUT")
            if event_ok:
                return "BASE_SQUEEZE_BREAKOUT", base_reason, reasons
            reasons.append(f"Earnings reaction filter: {event_reason}")
    reasons.append(f"Base/flag squeeze not ready: {base_reason}")

    return "", "No trigger-ready setup", reasons


# ==============================================================
# SCORING
# ==============================================================

def live_signal_score(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    plan: Dict[str, Any],
    regime: Dict[str, Any],
    phase: str,
) -> float:
    if not metrics.has_data:
        return 0.0

    # 1. VWAP structure: 20
    vwap_score = 0.0
    if metrics.above_vwap:
        if metrics.vwap_reclaim_ready:
            vwap_score = 22
        elif metrics.reclaim_pullback_holding:
            vwap_score = 21
        elif 0 <= metrics.vwap_dist_pct <= 2.0:
            vwap_score = 20
        elif 2.0 < metrics.vwap_dist_pct <= 3.0:
            vwap_score = 16
        elif 3.0 < metrics.vwap_dist_pct <= 5.0:
            vwap_score = 9
        else:
            vwap_score = 3
    else:
        low, high = vwap_reclaim_zone(safe_float(row.get("atr_pct"), 0))
        vwap_score = 6 if low <= metrics.vwap_dist_pct <= high else 0

    # 2. EMA9 confirmation: 10
    ema_score = ema9_confirmation_score(metrics)

    # 3. MACD / momentum confirmation: booster/penalty.
    macd_score = macd_confirmation_score(metrics)

    # 4. HOD/range position: 15
    if metrics.hod_distance_pct >= -0.75:
        hod_score = 15
    elif metrics.hod_distance_pct >= -2.5:
        hod_score = 10
    elif metrics.hod_distance_pct >= -4.0:
        hod_score = 6
    else:
        hod_score = 0

    # 3. Volume/structure: 15
    if metrics.vwap_reclaim_ready:
        volume_score = 17
    elif metrics.reclaim_pullback_holding and not metrics.volume_fading_vs_morning:
        volume_score = 14
    elif metrics.recent_volume_expanding and not metrics.volume_fading_vs_morning:
        volume_score = 15
    elif metrics.base_compression and not metrics.volume_fading_vs_morning:
        volume_score = 13
    elif metrics.volume_stable_or_increasing and not metrics.volume_fading_vs_morning:
        volume_score = 11
    elif metrics.base_volume_constructive and not metrics.volume_fading_vs_morning:
        volume_score = 8
    elif metrics.avg_volume_5 > 0:
        volume_score = 4
    else:
        volume_score = 0

    # 4. Risk/reward and plan validity: 15
    rr = safe_float(plan.get("reward_risk"), 0)
    plan_valid = bool(plan.get("valid"))
    if plan_valid and rr >= 2.5:
        rr_score = 15
    elif plan_valid and rr >= 2.0:
        rr_score = 12
    elif plan_valid and rr >= 1.5:
        rr_score = 10
    elif plan_valid and rr >= 0.75:
        rr_score = 5
    else:
        rr_score = 0

    # 5. Sector + market alignment: 15
    sector_status = safe_str(row.get("sector_status"), "").upper()
    rotation = sector_rotation_context(row, regime)

    if sector_status == "LEADING":
        sector_score = 8
    elif sector_status == "IMPROVING":
        sector_score = 7
    elif sector_status in {"NEUTRAL", "UNKNOWN", ""}:
        sector_score = 5
    elif sector_status == "WEAK":
        sector_score = 1
    else:
        sector_score = 4

    # Sector rotation is a small confirmation/penalty only. It should not
    # overpower VWAP, trigger behavior, R/R, or live participation.
    sector_score = clamp(
        sector_score + safe_float(rotation.get("sector_confidence_adjustment"), 0.0),
        0,
        12,
    )

    rs_long = is_relative_strength_long(row, metrics, regime)
    if severe_risk_off(regime):
        market_score = 5 if rs_long else 0
    else:
        market_score = 5

    # 6. Time-of-day: 10
    if phase in {"VALID_MORNING", "VALID_AFTERNOON"}:
        time_score = 10
    elif phase == "PREMARKET":
        time_score = 4
    else:
        time_score = 0

    # 7. Extension / touch penalty: up to -10
    penalty = 0
    if metrics.vwap_dist_pct > 5.0:
        penalty += 7
    elif metrics.vwap_dist_pct > 3.0:
        penalty += 4

    if metrics.vwap_touch_count >= 4:
        penalty += 3

    if metrics.ema9 > 0:
        if not metrics.price_above_ema9:
            penalty += EMA9_BELOW_PRICE_PENALTY
        if metrics.ema9_falling:
            penalty += EMA9_FALLING_PENALTY
        if not metrics.ema9_above_vwap and not metrics.ema9_crossed_above_vwap_recent:
            penalty += EMA9_BELOW_VWAP_PENALTY

    if metrics.hod_distance_pct < -4.0:
        penalty += 3

    if metrics.volume_fading_vs_morning and not metrics.vwap_reclaim_ready:
        penalty += VOLUME_FADE_PENALTY

    if not metrics.live_participation_ok:
        penalty += 8

    if metrics.bearish_momentum_divergence:
        penalty += BEARISH_DIVERGENCE_PENALTY

    if metrics.macd_bearish_crossover_recent and not metrics.reclaim_pullback_holding:
        penalty += MACD_BEARISH_CROSSOVER_PENALTY

    if metrics.macd_histogram_falling and not metrics.macd_above_signal and not metrics.reclaim_pullback_holding:
        penalty += MACD_HISTOGRAM_WEAKENING_PENALTY

    if is_late_day() and not late_day_volume_confirmed(metrics):
        penalty += LATE_DAY_NO_VOLUME_EXPANSION_PENALTY

    score = (
        vwap_score
        + hod_score
        + ema_score
        + macd_score
        + volume_score
        + rr_score
        + sector_score
        + market_score
        + time_score
        - min(25, penalty)
    )

    return round(clamp(score, 0, 100), 2)


def final_confidence(scanner_score: float, live_score: float) -> float:
    return round(clamp(0.40 * scanner_score + 0.60 * live_score, 0, 100), 1)


# ==============================================================
# DIAGNOSTICS
# ==============================================================

def not_ready_reasons(metrics: IntradayMetrics, plan: Dict[str, Any], confidence: float) -> List[str]:
    reasons: List[str] = []

    if not plan.get("valid"):
        reasons.append(f"Plan invalid: {safe_str(plan.get('rejection_reason'), 'Invalid plan')}")

    required_conf = ready_confidence_required("VALID_AFTERNOON" if is_late_day() else "VALID_MORNING")
    if confidence < required_conf:
        reasons.append(f"Confidence {confidence:.1f} < ready minimum {required_conf:.0f}")

    rr = safe_float(plan.get("reward_risk"), 0)
    if rr < MIN_RR:
        reasons.append(f"R/R {rr:.2f} < minimum 1.5")

    # Event-risk diagnostics. If this is an earnings-day name, Signal Desk
    # should make clear it is an earnings reaction trade only.
    if plan.get("valid"):
        # Row is not available here, so earnings-specific reasons are attached
        # in diagnostic_candidate/signal_base. Setup-specific not-ready reasons
        # remain focused on live technicals.
        pass

    _, reclaim_reason = setup_vwap_reclaim_ready(metrics, rr, confidence)
    reasons.append(f"VWAP reclaim not ready: {reclaim_reason}")

    _, reclaim_pullback_reason = setup_reclaim_pullback_ready(metrics, rr, confidence)
    reasons.append(f"Reclaim pullback not ready: {reclaim_pullback_reason}")

    _, vwap_reason = setup_vwap_pullback_ready(metrics, rr, confidence)
    reasons.append(f"VWAP pullback not ready: {vwap_reason}")

    _, base_reason = setup_base_squeeze_ready(metrics, rr, confidence)
    reasons.append(f"Base/flag squeeze not ready: {base_reason}")

    _, hod_reason = setup_hod_breakout_ready(metrics, rr, confidence)
    reasons.append(f"HOD breakout not ready: {hod_reason}")

    # De-duplicate while preserving order.
    out = []
    seen = set()
    for r in reasons:
        if r not in seen:
            out.append(r)
            seen.add(r)
    return out


def diagnostic_candidate(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    plan: Dict[str, Any],
    live: float,
    conf: float,
    rejected_reasons: List[str],
    phase: str,
    regime: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "symbol": normalize_symbol(row.get("symbol")),
        "diagnostic_status": "REJECTED",
        "scanner_score": safe_float(row.get("score"), 0),
        "base_scanner_score": safe_float(row.get("base_scanner_score"), safe_float(row.get("score"), 0)),
        "smart_money_score": safe_float(row.get("smart_money_score"), 0),
        "smart_money_adjustment": safe_float(row.get("smart_money_adjustment"), 0),
        "smart_money_label": safe_str(row.get("smart_money_label"), ""),
        "smart_money_bias": safe_str(row.get("smart_money_bias"), ""),
        "smart_money_signals": safe_str(row.get("smart_money_signals"), ""),
        "source_bucket": safe_str(row.get("signal_source_bucket"), ""),
        "signal_rank": safe_int(row.get("signal_rank"), 0),
        "market_phase": phase,
        "sector_status": safe_str(row.get("sector_status"), ""),
        **sector_rotation_context(row, regime),
        "risk_category": safe_str(row.get("risk_category"), "NORMAL"),
        "setup_bucket": safe_str(row.get("setup_bucket"), ""),
        "event_context": event_context_for_row(row),
        "earnings_reaction_trade": is_earnings_reaction_row(row),
        "event_risk_warning": (
            "Earnings reaction only — higher volatility; requires stricter VWAP/volume/R:R confirmation."
            if is_earnings_reaction_row(row) else ""
        ),
        "dollar_vol_M": safe_float(row.get("dollar_vol_M"), 0),
        "atr_pct": safe_float(row.get("atr_pct"), 0),
        "price": round(metrics.price, 4),
        "price_source": metrics.price_source,
        "price_updated_at": metrics.price_updated_at,
        "latest_bar_time": timestamp_to_et_iso(metrics.latest_bar_time),
        "bid": round(metrics.bid, 4),
        "ask": round(metrics.ask, 4),
        "quote_mid": round(metrics.quote_mid, 4),
        "quote_time": metrics.quote_time,
        "trade_price": round(metrics.trade_price, 4),
        "trade_time": metrics.trade_time,
        "session_open": round(metrics.session_open, 4),
        "day_change_pct": round(metrics.day_change_pct, 2),
        "relative_strength_long": is_relative_strength_long(row, metrics, regime),
        "vwap": round(metrics.vwap, 4),
        "hod": round(metrics.hod, 4),
        "vwap_dist_pct": round(metrics.vwap_dist_pct, 2),
        "hod_distance_pct": round(metrics.hod_distance_pct, 2),
        "above_vwap": metrics.above_vwap,
        "vwap_touch_count": metrics.vwap_touch_count,
        "vwap_opening_touches_ignored": metrics.vwap_opening_touches_ignored,
        "vwap_recent_touch_count": metrics.vwap_recent_touch_count,
        "vwap_clean_hold_count": metrics.vwap_clean_hold_count,
        "vwap_failed_touch_count": metrics.vwap_failed_touch_count,
        "vwap_touch_status": metrics.vwap_touch_status,
        "bullish_structure_start_et": metrics.bullish_structure_start_et,
        "ema9": round(metrics.ema9, 4),
        "ema9_prev": round(metrics.ema9_prev, 4),
        "ema9_slope_pct": round(metrics.ema9_slope_pct, 3),
        "price_above_ema9": metrics.price_above_ema9,
        "ema9_rising": metrics.ema9_rising,
        "ema9_falling": metrics.ema9_falling,
        "ema9_above_vwap": metrics.ema9_above_vwap,
        "ema9_crossed_above_vwap_recent": metrics.ema9_crossed_above_vwap_recent,
        "ema9_status": metrics.ema9_status,
        "macd_value": round(metrics.macd_value, 4),
        "macd_signal": round(metrics.macd_signal, 4),
        "macd_histogram": round(metrics.macd_histogram, 4),
        "macd_histogram_prev": round(metrics.macd_histogram_prev, 4),
        "macd_above_signal": metrics.macd_above_signal,
        "macd_above_zero": metrics.macd_above_zero,
        "macd_bullish_crossover_recent": metrics.macd_bullish_crossover_recent,
        "macd_bearish_crossover_recent": metrics.macd_bearish_crossover_recent,
        "macd_histogram_rising": metrics.macd_histogram_rising,
        "macd_histogram_falling": metrics.macd_histogram_falling,
        "macd_status": metrics.macd_status,
        "macd_1m_value": round(metrics.macd_1m_value, 4),
        "macd_1m_signal": round(metrics.macd_1m_signal, 4),
        "macd_1m_histogram": round(metrics.macd_1m_histogram, 4),
        "macd_1m_histogram_prev": round(metrics.macd_1m_histogram_prev, 4),
        "macd_1m_above_signal": metrics.macd_1m_above_signal,
        "macd_1m_bullish_crossover_recent": metrics.macd_1m_bullish_crossover_recent,
        "macd_1m_bearish_crossover_recent": metrics.macd_1m_bearish_crossover_recent,
        "macd_1m_histogram_rising": metrics.macd_1m_histogram_rising,
        "macd_1m_histogram_falling": metrics.macd_1m_histogram_falling,
        "macd_1m_curling_up": metrics.macd_1m_curling_up,
        "macd_1m_curling_down": metrics.macd_1m_curling_down,
        "macd_1m_status": metrics.macd_1m_status,
        "base_compression": metrics.base_compression,
        "base_range_pct": round(metrics.base_range_pct, 2),
        "base_high": round(metrics.base_high, 4),
        "base_low": round(metrics.base_low, 4),
        "execution_timeframe": EXECUTION_TIMEFRAME,
        "structure_timeframe": STRUCTURE_TIMEFRAME,
        "structure_bar_count": metrics.structure_bar_count,
        "recent_1m_volume": round(metrics.recent_1m_volume, 2),
        "avg_volume_1m_5": round(metrics.avg_volume_1m_5, 2),
        "avg_volume_1m_20": round(metrics.avg_volume_1m_20, 2),
        "recent_5m_dollar_volume": round(metrics.recent_5m_dollar_volume, 2),
        "live_participation_ok": metrics.live_participation_ok,
        "live_participation_ready_ok": metrics.live_participation_ready_ok,
        "live_participation_reason": metrics.live_participation_reason,
        "vwap_reclaim_recent": metrics.vwap_reclaim_recent,
        "vwap_reclaim_ready": metrics.vwap_reclaim_ready,
        "vwap_reclaim_bar_high": round(metrics.vwap_reclaim_bar_high, 4),
        "vwap_reclaim_bar_low": round(metrics.vwap_reclaim_bar_low, 4),
        "vwap_reclaim_bar_close": round(metrics.vwap_reclaim_bar_close, 4),
        "vwap_reclaim_bar_time": metrics.vwap_reclaim_bar_time,
        "vwap_reclaim_age_minutes": round(metrics.vwap_reclaim_age_minutes, 2),
        "vwap_reclaim_volume_ratio": round(metrics.vwap_reclaim_volume_ratio, 2),
        "vwap_reclaim_reason": metrics.vwap_reclaim_reason,
        "vwap_reclaim_lifecycle_active": metrics.vwap_reclaim_lifecycle_active,
        "vwap_reclaim_support_level": round(metrics.vwap_reclaim_support_level, 4),
        "reclaim_pullback_holding": metrics.reclaim_pullback_holding,
        "reclaim_pullback_reason": metrics.reclaim_pullback_reason,
        "avg_volume_5": round(metrics.avg_volume_5, 2),
        "avg_volume_prev_5": round(metrics.avg_volume_prev_5, 2),
        "morning_avg_volume_5m": round(metrics.morning_avg_volume_5m, 2),
        "recent_to_morning_volume_ratio": round(metrics.recent_to_morning_volume_ratio, 2),
        "volume_fading_vs_morning": metrics.volume_fading_vs_morning,
        "recent_volume_expanding": metrics.recent_volume_expanding,
        "bearish_momentum_divergence": metrics.bearish_momentum_divergence,
        "momentum_status": metrics.momentum_status,
        "live_signal_score": round(live, 1),
        "confidence": round(conf, 1),
        "reward_risk": safe_float(plan.get("reward_risk"), 0),
        "plan_valid": bool(plan.get("valid")),
        "rejected_reasons": rejected_reasons,
        "not_ready_reasons": not_ready_reasons(metrics, plan, conf),
        "last_checked": iso_now_et(),
        "entry_trigger": plan.get("entry_trigger"),
        "stop_loss": plan.get("stop_loss"),
        "target_1": plan.get("target_1"),
        "target_2": plan.get("target_2"),
        "stop_distance_pct": plan.get("stop_distance_pct"),
        "support_level": plan.get("support_level"),
        "structure_label": plan.get("structure_label"),
        "min_target_1": plan.get("min_target_1"),
        "buffer_pct_used": plan.get("buffer_pct_used"),
    }


# ==============================================================
# SIGNAL STATE LOGIC
# ==============================================================

def signal_base(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    plan: Dict[str, Any],
    status: str,
    setup_type: str,
    confidence: float,
    live_score: float,
    reason: str,
    phase: str,
    regime: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    symbol = normalize_symbol(row.get("symbol"))
    now_text = iso_now_et()

    return {
        "symbol": symbol,
        "signal_id": "",
        "strategy_version": SIGNAL_ENGINE_STRATEGY_VERSION,
        "signal_status": status,
        "setup_type": setup_type or "MONITORING",
        "setup_label": setup_display_label(row, setup_type or "MONITORING"),
        "event_context": event_context_for_row(row),
        "earnings_reaction_trade": is_earnings_reaction_row(row),
        "event_risk_warning": (
            "Earnings reaction only — higher volatility; requires stricter VWAP/volume/R:R confirmation."
            if is_earnings_reaction_row(row) else ""
        ),
        "confidence": round(confidence, 1),
        "readiness_grade": "PRIME_READY" if confidence >= 80 else ("TRIGGER_READY" if confidence >= MIN_CONF_READY else "WATCH"),
        "live_signal_score": round(live_score, 1),
        "scanner_score": safe_float(row.get("score"), 0),
        "base_scanner_score": safe_float(row.get("base_scanner_score"), safe_float(row.get("score"), 0)),
        "smart_money_score": safe_float(row.get("smart_money_score"), 0),
        "smart_money_adjustment": safe_float(row.get("smart_money_adjustment"), 0),
        "smart_money_label": safe_str(row.get("smart_money_label"), ""),
        "smart_money_bias": safe_str(row.get("smart_money_bias"), ""),
        "smart_money_signals": safe_str(row.get("smart_money_signals"), ""),
        "smart_money_volume_ratio": safe_float(row.get("smart_money_volume_ratio"), 0),
        "smart_money_vwap_distance_pct": safe_float(row.get("smart_money_vwap_distance_pct"), 0),
        "smart_money_vwap_touch_count": safe_int(row.get("smart_money_vwap_touch_count"), 0),
        "smart_money_range_ratio": safe_float(row.get("smart_money_range_ratio"), 0),
        "source_bucket": safe_str(row.get("signal_source_bucket"), ""),
        "signal_rank": safe_int(row.get("signal_rank"), 0),
        "entry_trigger": plan.get("entry_trigger"),
        "stop_loss": plan.get("stop_loss"),
        "target_1": plan.get("target_1"),
        "target_2": plan.get("target_2"),
        "reward_risk": plan.get("reward_risk"),
        "stop_distance_pct": plan.get("stop_distance_pct"),
        "support_level": plan.get("support_level"),
        "structure_label": plan.get("structure_label"),
        "min_target_1": plan.get("min_target_1"),
        "buffer_pct_used": plan.get("buffer_pct_used"),
        "atr_pct": safe_float(row.get("atr_pct"), 0),
        "dollar_vol_M": safe_float(row.get("dollar_vol_M"), 0),
        "risk_category": safe_str(row.get("risk_category"), "NORMAL"),
        "setup_bucket": safe_str(row.get("setup_bucket"), ""),
        "intraday_setup_type": safe_str(row.get("intraday_setup_type"), ""),
        "early_reclaim_runner": scanner_marks_early_reclaim(row),
        "early_reclaim_score": safe_float(row.get("early_reclaim_score"), 0),
        "early_reclaim_reason": safe_str(row.get("early_reclaim_reason"), ""),
        "vwap_reclaim_quality_label": safe_str(row.get("vwap_reclaim_quality_label"), ""),
        "vwap_reclaim_attempt_count": safe_int(row.get("vwap_reclaim_attempt_count"), 0),
        "vwap_reclaim_failed_count": safe_int(row.get("vwap_reclaim_failed_count"), 0),
        "vwap_reclaim_current_attempt": safe_int(row.get("vwap_reclaim_current_attempt"), 0),
        "early_reclaim_bucket_promoted": is_truthy_value(row.get("early_reclaim_bucket_promoted")),
        "price": round(metrics.price, 4),
        "price_source": metrics.price_source,
        "price_updated_at": metrics.price_updated_at,
        "latest_bar_time": timestamp_to_et_iso(metrics.latest_bar_time),
        "bid": round(metrics.bid, 4),
        "ask": round(metrics.ask, 4),
        "quote_mid": round(metrics.quote_mid, 4),
        "quote_time": metrics.quote_time,
        "trade_price": round(metrics.trade_price, 4),
        "trade_time": metrics.trade_time,
        "session_open": round(metrics.session_open, 4),
        "day_change_pct": round(metrics.day_change_pct, 2),
        "relative_strength_long": is_relative_strength_long(row, metrics, regime or {}),
        "vwap": round(metrics.vwap, 4),
        "hod": round(metrics.hod, 4),
        "vwap_dist_pct": round(metrics.vwap_dist_pct, 2),
        "hod_distance_pct": round(metrics.hod_distance_pct, 2),
        "above_vwap": metrics.above_vwap,
        "vwap_touch_count": metrics.vwap_touch_count,
        "vwap_opening_touches_ignored": metrics.vwap_opening_touches_ignored,
        "vwap_recent_touch_count": metrics.vwap_recent_touch_count,
        "vwap_clean_hold_count": metrics.vwap_clean_hold_count,
        "vwap_failed_touch_count": metrics.vwap_failed_touch_count,
        "vwap_touch_status": metrics.vwap_touch_status,
        "bullish_structure_start_et": metrics.bullish_structure_start_et,
        "ema9": round(metrics.ema9, 4),
        "ema9_prev": round(metrics.ema9_prev, 4),
        "ema9_slope_pct": round(metrics.ema9_slope_pct, 3),
        "price_above_ema9": metrics.price_above_ema9,
        "ema9_rising": metrics.ema9_rising,
        "ema9_falling": metrics.ema9_falling,
        "ema9_above_vwap": metrics.ema9_above_vwap,
        "ema9_crossed_above_vwap_recent": metrics.ema9_crossed_above_vwap_recent,
        "ema9_status": metrics.ema9_status,
        "macd_value": round(metrics.macd_value, 4),
        "macd_signal": round(metrics.macd_signal, 4),
        "macd_histogram": round(metrics.macd_histogram, 4),
        "macd_histogram_prev": round(metrics.macd_histogram_prev, 4),
        "macd_above_signal": metrics.macd_above_signal,
        "macd_above_zero": metrics.macd_above_zero,
        "macd_bullish_crossover_recent": metrics.macd_bullish_crossover_recent,
        "macd_bearish_crossover_recent": metrics.macd_bearish_crossover_recent,
        "macd_histogram_rising": metrics.macd_histogram_rising,
        "macd_histogram_falling": metrics.macd_histogram_falling,
        "macd_status": metrics.macd_status,
        "macd_1m_value": round(metrics.macd_1m_value, 4),
        "macd_1m_signal": round(metrics.macd_1m_signal, 4),
        "macd_1m_histogram": round(metrics.macd_1m_histogram, 4),
        "macd_1m_histogram_prev": round(metrics.macd_1m_histogram_prev, 4),
        "macd_1m_above_signal": metrics.macd_1m_above_signal,
        "macd_1m_bullish_crossover_recent": metrics.macd_1m_bullish_crossover_recent,
        "macd_1m_bearish_crossover_recent": metrics.macd_1m_bearish_crossover_recent,
        "macd_1m_histogram_rising": metrics.macd_1m_histogram_rising,
        "macd_1m_histogram_falling": metrics.macd_1m_histogram_falling,
        "macd_1m_curling_up": metrics.macd_1m_curling_up,
        "macd_1m_curling_down": metrics.macd_1m_curling_down,
        "macd_1m_status": metrics.macd_1m_status,
        "base_compression": metrics.base_compression,
        "base_range_pct": round(metrics.base_range_pct, 2),
        "base_high": round(metrics.base_high, 4),
        "base_low": round(metrics.base_low, 4),
        "execution_timeframe": EXECUTION_TIMEFRAME,
        "structure_timeframe": STRUCTURE_TIMEFRAME,
        "structure_bar_count": metrics.structure_bar_count,
        "recent_1m_volume": round(metrics.recent_1m_volume, 2),
        "avg_volume_1m_5": round(metrics.avg_volume_1m_5, 2),
        "avg_volume_1m_20": round(metrics.avg_volume_1m_20, 2),
        "recent_5m_dollar_volume": round(metrics.recent_5m_dollar_volume, 2),
        "live_participation_ok": metrics.live_participation_ok,
        "live_participation_ready_ok": metrics.live_participation_ready_ok,
        "live_participation_reason": metrics.live_participation_reason,
        "vwap_reclaim_recent": metrics.vwap_reclaim_recent,
        "vwap_reclaim_ready": metrics.vwap_reclaim_ready,
        "vwap_reclaim_bar_high": round(metrics.vwap_reclaim_bar_high, 4),
        "vwap_reclaim_bar_low": round(metrics.vwap_reclaim_bar_low, 4),
        "vwap_reclaim_bar_close": round(metrics.vwap_reclaim_bar_close, 4),
        "vwap_reclaim_bar_time": metrics.vwap_reclaim_bar_time,
        "vwap_reclaim_age_minutes": round(metrics.vwap_reclaim_age_minutes, 2),
        "vwap_reclaim_volume_ratio": round(metrics.vwap_reclaim_volume_ratio, 2),
        "vwap_reclaim_reason": metrics.vwap_reclaim_reason,
        "vwap_reclaim_lifecycle_active": metrics.vwap_reclaim_lifecycle_active,
        "vwap_reclaim_support_level": round(metrics.vwap_reclaim_support_level, 4),
        "reclaim_pullback_holding": metrics.reclaim_pullback_holding,
        "reclaim_pullback_reason": metrics.reclaim_pullback_reason,
        "avg_volume_5": round(metrics.avg_volume_5, 2),
        "avg_volume_prev_5": round(metrics.avg_volume_prev_5, 2),
        "morning_avg_volume_5m": round(metrics.morning_avg_volume_5m, 2),
        "recent_to_morning_volume_ratio": round(metrics.recent_to_morning_volume_ratio, 2),
        "volume_fading_vs_morning": metrics.volume_fading_vs_morning,
        "recent_volume_expanding": metrics.recent_volume_expanding,
        "bearish_momentum_divergence": metrics.bearish_momentum_divergence,
        "momentum_status": metrics.momentum_status,
        "sector_status": safe_str(row.get("sector_status"), ""),
        **sector_rotation_context(row, regime or {}),
        "market_phase": phase,
        "reason": reason,
        "invalidation": "For VWAP reclaim: invalidate only on VWAP/reclaim-base loss, stale setup, stop hit, or invalid R/R. For other setups: lose VWAP/trigger/base support or stale signal.",
        "last_checked": now_text,
        "updated_at": now_text,
        "session_date": session_date_str(),
        "actionable": status == "ACTIVE_SIGNAL",
        "actionability": (
            "ACTIVE" if status == "ACTIVE_SIGNAL"
            else "TRIGGER_READY" if status == "TRIGGER_READY"
            else "WATCH"
        ),
        "suppression_reason": "",
        "risk_flags": safe_str(row.get("risk_flags"), ""),
        "company_name": safe_str(row.get("company_name"), ""),
        "not_ready_reasons": not_ready_reasons(metrics, plan, confidence) if status == "WATCH" else [],
    }


def make_invalidated(
    existing: Dict[str, Any],
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    reason: str,
    category: str,
    phase: str,
) -> Dict[str, Any]:
    symbol = normalize_symbol(existing.get("symbol") or row.get("symbol"))
    now_text = iso_now_et()

    out = dict(existing)
    out.update({
        "symbol": symbol,
        "signal_status": "INVALIDATED",
        "invalidation_reason": reason,
        "invalidation_category": category,
        "reason": reason,
        "actionable": False,
        "actionability": "INVALIDATED",
        "last_checked": now_text,
        "invalidated_at": now_text,
        "updated_at": now_text,
        "market_phase": phase,
        "price": round(metrics.price, 4) if metrics.has_data else out.get("price", 0),
        "price_source": metrics.price_source if metrics.has_data else out.get("price_source", ""),
        "price_updated_at": metrics.price_updated_at if metrics.has_data else out.get("price_updated_at", ""),
        "latest_bar_time": timestamp_to_et_iso(metrics.latest_bar_time) if metrics.has_data else out.get("latest_bar_time", ""),
        "bid": round(metrics.bid, 4) if metrics.has_data else out.get("bid", 0),
        "ask": round(metrics.ask, 4) if metrics.has_data else out.get("ask", 0),
        "quote_mid": round(metrics.quote_mid, 4) if metrics.has_data else out.get("quote_mid", 0),
        "quote_time": metrics.quote_time if metrics.has_data else out.get("quote_time", ""),
        "trade_price": round(metrics.trade_price, 4) if metrics.has_data else out.get("trade_price", 0),
        "trade_time": metrics.trade_time if metrics.has_data else out.get("trade_time", ""),
        "vwap": round(metrics.vwap, 4) if metrics.has_data else out.get("vwap", 0),
        "hod": round(metrics.hod, 4) if metrics.has_data else out.get("hod", 0),
        "vwap_dist_pct": round(metrics.vwap_dist_pct, 2) if metrics.has_data else out.get("vwap_dist_pct", 0),
        "hod_distance_pct": round(metrics.hod_distance_pct, 2) if metrics.has_data else out.get("hod_distance_pct", 0),
    })
    decision_log(
        symbol,
        "INVALIDATED",
        category=category,
        reason=reason,
        phase=phase,
        price=out.get("price", 0),
        status_before=normalize_status(existing.get("signal_status")),
    )
    return out


def is_previous_session(signal: Dict[str, Any]) -> bool:
    return safe_str(signal.get("session_date"), "") not in {"", session_date_str()}


def expired_by_age(signal: Dict[str, Any], max_minutes: int, now: Optional[datetime] = None) -> bool:
    keys = ["triggered_at", "ready_since", "detected_at", "updated_at"]
    for key in keys:
        val = signal.get(key)
        age = minutes_since(val, now)
        if age is not None:
            return age > max_minutes
    return False


def is_reclaim_lifecycle_setup(signal: Dict[str, Any]) -> bool:
    return safe_str(signal.get("setup_type"), "").upper() in {
        "VWAP_EMA_RECLAIM_RUNNER",
        "VWAP_RECLAIM_BREAKOUT",
        "RECLAIM_PULLBACK_HOLDING",
        "VWAP_PULLBACK_CONTINUATION",
    }


def recent_closes_below_level(metrics: IntradayMetrics, level: float, count: int = 2) -> int:
    if level <= 0:
        return 0
    bars = recent_execution_bars(metrics, count)
    closes = [safe_float(b.get("c"), 0) for b in bars]
    return sum(1 for c in closes if c > 0 and c < level)


def latest_close_low_high(metrics: IntradayMetrics) -> Tuple[float, float, float]:
    bar = latest_execution_bar(metrics)
    close = safe_float(bar.get("c"), metrics.price)
    low = safe_float(bar.get("l"), metrics.price)
    high = safe_float(bar.get("h"), metrics.price)
    return close, low, high


def reclaim_active_confirmation_quality(
    signal: Dict[str, Any],
    metrics: IntradayMetrics,
    setup_type: str = "",
) -> Tuple[bool, str]:
    """
    Reclaim ACTIVE confirmation must be candle-close based.

    CIFR exposed the problem: a current tick can touch/hold briefly, become ACTIVE,
    then immediately lose VWAP. Require the latest 1-minute close to hold trigger,
    VWAP, and EMA9 before activation.
    """
    trigger = safe_float(signal.get("entry_trigger"), 0)
    if trigger <= 0:
        return False, "No locked reclaim trigger"

    close, low, high = latest_close_low_high(metrics)

    if close <= 0:
        return False, "No latest 1-minute close for reclaim confirmation"

    if close < trigger:
        return False, f"Latest 1-minute close {close:.2f} did not hold trigger {trigger:.2f}"

    if RECLAIM_ACTIVE_REQUIRE_CLOSE_ABOVE_VWAP and metrics.vwap > 0 and close < metrics.vwap:
        return False, f"Latest 1-minute close {close:.2f} did not hold VWAP {metrics.vwap:.2f}"

    if RECLAIM_ACTIVE_REQUIRE_CLOSE_ABOVE_EMA9 and metrics.ema9 > 0 and close < metrics.ema9:
        return False, f"Latest 1-minute close {close:.2f} did not hold EMA9 {metrics.ema9:.2f}"

    if metrics.vwap > 0:
        vwap_undercut = metrics.vwap * (1.0 - RECLAIM_ACTIVE_MAX_VWAP_UNDERCUT_PCT / 100.0)
        if low < vwap_undercut and close <= metrics.vwap * 1.0005:
            return False, "Reclaim confirmation candle undercut VWAP and did not reclaim strongly"

    if (
        metrics.macd_1m_curling_down
        and metrics.macd_1m_histogram_falling
        and not metrics.macd_1m_above_signal
        and metrics.ema9 > 0
        and close < metrics.ema9 * 1.001
    ):
        return False, "1-minute MACD curling down while confirmation candle is pressing EMA9"

    if high > trigger and close > 0:
        wick_reject_pct = pct_change(high, max(close, trigger))
        if wick_reject_pct >= TRIGGER_WICK_REJECTION_PCT and close <= trigger * 1.002:
            return False, f"Reclaim trigger wick rejected near entry ({wick_reject_pct:.2f}% upper rejection)"

    return True, "Latest 1-minute close held trigger, VWAP, and EMA9"


def reclaim_active_hard_failure(signal: Dict[str, Any], metrics: IntradayMetrics) -> Tuple[bool, str]:
    """
    Active reclaim invalidation is softer than old logic.

    Do not invalidate on a single VWAP wick. Invalidate only if:
      - stop is hit,
      - real reclaim/support breaks,
      - or 2 recent closes lose VWAP/EMA9 with weak momentum.
    """
    stop = safe_float(signal.get("stop_loss"), 0)
    if stop > 0 and metrics.price <= stop:
        return True, "Stop would have been hit"

    vwap = safe_float(metrics.vwap, 0)
    stored_vwap = safe_float(signal.get("vwap"), 0)
    support_candidates = [
        safe_float(signal.get("support_level"), 0),
        safe_float(signal.get("vwap_reclaim_support_level"), 0),
        safe_float(signal.get("vwap_reclaim_bar_low"), 0),
        vwap,
        stored_vwap,
    ]
    support_candidates = [x for x in support_candidates if x > 0]
    support = max(support_candidates) if support_candidates else 0

    if support > 0:
        hard_support = support * (1.0 - RECLAIM_ACTIVE_SUPPORT_BREAK_BUFFER_PCT / 100.0)
        if metrics.price < hard_support:
            return True, f"Active reclaim signal broke reclaim support: price {metrics.price:.2f} < support {support:.2f}"

    close, low, _high = latest_close_low_high(metrics)
    closes_below_vwap = recent_closes_below_level(metrics, vwap, RECLAIM_ACTIVE_VWAP_LOSS_CLOSES) if vwap > 0 else 0
    closes_below_ema9 = recent_closes_below_level(metrics, metrics.ema9, RECLAIM_ACTIVE_VWAP_LOSS_CLOSES) if metrics.ema9 > 0 else 0

    if (
        vwap > 0
        and closes_below_vwap >= RECLAIM_ACTIVE_VWAP_LOSS_CLOSES
        and (metrics.ema9 <= 0 or closes_below_ema9 >= RECLAIM_ACTIVE_VWAP_LOSS_CLOSES)
    ):
        return True, f"Active reclaim lost VWAP/EMA on {RECLAIM_ACTIVE_VWAP_LOSS_CLOSES} consecutive 1-minute closes"

    if (
        vwap > 0
        and close > 0
        and close < vwap
        and metrics.macd_1m_curling_down
        and metrics.macd_1m_histogram_falling
        and not metrics.price_above_ema9
    ):
        return True, "Active reclaim closed below VWAP with weakening 1-minute MACD and EMA9 loss"

    return False, ""


def reclaim_lifecycle_holding(signal: Dict[str, Any], metrics: IntradayMetrics) -> Tuple[bool, str]:
    """
    For VWAP reclaim setups, a pullback to EMA9/VWAP is normal.
    Keep the setup alive while VWAP/reclaim support is holding.
    """
    if not metrics.has_data:
        return False, "No intraday data available"

    vwap = safe_float(metrics.vwap, 0)
    stored_vwap = safe_float(signal.get("vwap"), 0)
    support_candidates = [
        safe_float(signal.get("support_level"), 0),
        safe_float(signal.get("vwap_reclaim_support_level"), 0),
        safe_float(signal.get("vwap_reclaim_bar_low"), 0),
        vwap,
        stored_vwap,
    ]
    support_candidates = [x for x in support_candidates if x > 0]
    support = max(support_candidates) if support_candidates else 0

    if vwap > 0 and metrics.price < vwap * (1.0 - RECLAIM_PULLBACK_SUPPORT_BUFFER_PCT / 100.0):
        return False, f"Lost VWAP reclaim support: price {metrics.price:.2f} < VWAP {vwap:.2f}"

    if support > 0 and metrics.price < support * 0.992:
        return False, f"Broke reclaim lifecycle support: price {metrics.price:.2f} < support {support:.2f}"

    if metrics.bearish_momentum_divergence and metrics.price < vwap:
        return False, "Bearish divergence while losing VWAP reclaim support"

    return True, (
        f"VWAP reclaim lifecycle still holding: price {metrics.price:.2f}, "
        f"VWAP {vwap:.2f}, support {support:.2f}"
    )


def trigger_fired(existing: Dict[str, Any], metrics: IntradayMetrics) -> bool:
    trigger = safe_float(existing.get("entry_trigger"), 0)
    if trigger <= 0 or not metrics.has_data:
        return False
    return metrics.price >= trigger and metrics.above_vwap and metrics.price_above_ema9


def active_invalidated(signal: Dict[str, Any], metrics: IntradayMetrics) -> Tuple[bool, str, str]:
    if not metrics.has_data:
        return True, "No intraday data available", "EXTERNAL_RISK"

    trigger = safe_float(signal.get("entry_trigger"), 0)
    stop = safe_float(signal.get("stop_loss"), 0)

    if is_reclaim_lifecycle_setup(signal):
        hard_fail, hard_reason = reclaim_active_hard_failure(signal, metrics)
        if hard_fail:
            return True, hard_reason, "FAILED_SETUP"

        if trigger > 0 and metrics.price < trigger:
            holding, _hold_reason = reclaim_lifecycle_holding(signal, metrics)
            if not holding:
                return True, "Active reclaim signal lost VWAP/reclaim support after falling below trigger", "FAILED_SETUP"

        if expired_by_age(signal, ACTIVE_STALE_MINUTES):
            return True, "Active reclaim signal stale for more than 2 refresh cycles", "MISSED_WINDOW"

        # Reclaim setups may wick under VWAP/EMA briefly. Keep tracking unless the
        # hard-failure rules above confirm support loss.
        return False, "", ""

    if not metrics.above_vwap:
        return True, "Lost VWAP after active signal", "FAILED_SETUP"

    if trigger > 0 and metrics.price < trigger:
        return True, "Price fell back below trigger", "FAILED_SETUP"

    if stop > 0 and metrics.price <= stop:
        return True, "Stop would have been hit", "FAILED_SETUP"

    if expired_by_age(signal, ACTIVE_STALE_MINUTES):
        return True, "Active signal stale for more than 2 refresh cycles", "MISSED_WINDOW"

    return False, "", ""


def ready_invalidated(signal: Dict[str, Any], metrics: IntradayMetrics) -> Tuple[bool, str, str]:
    if not metrics.has_data:
        return True, "No intraday data available", "EXTERNAL_RISK"

    support = safe_float(signal.get("support_level"), 0)
    trigger = safe_float(signal.get("entry_trigger"), 0)

    # VWAP reclaim lifecycle is intentionally more tolerant:
    # after reclaim, a pullback toward EMA9/VWAP is normal and should not be
    # invalidated unless VWAP/reclaim support fails.
    if is_reclaim_lifecycle_setup(signal):
        holding, hold_reason = reclaim_lifecycle_holding(signal, metrics)
        if not holding:
            return True, hold_reason, "FAILED_SETUP"

        if expired_by_age(signal, TRIGGER_READY_STALE_MINUTES):
            return True, "VWAP reclaim trigger-ready setup became stale", "MISSED_WINDOW"

        if trigger > 0 and pct_change(metrics.price, trigger) > 2.0:
            return True, "Price moved too far above reclaim trigger without valid active signal", "MISSED_WINDOW"

        return False, "", ""

    if not metrics.above_vwap:
        return True, "Lost VWAP before trigger", "FAILED_SETUP"

    if support > 0 and metrics.price < support:
        return True, "Broke setup support before trigger", "FAILED_SETUP"

    if metrics.ema9 > 0 and not metrics.price_above_ema9:
        return True, "Price lost EMA9 before trigger", "FAILED_SETUP"

    if is_late_day() and metrics.ema9_falling:
        return True, "Late-day setup EMA9 turned down before trigger", "FAILED_SETUP"

    macd_ok, macd_reason = macd_ready_confirmation(metrics)
    if not macd_ok:
        return True, f"{macd_reason} developed before trigger", "FAILED_SETUP"

    if metrics.volume_fading_vs_morning:
        return True, volume_fade_label(metrics) + " before trigger", "FAILED_SETUP"

    if is_late_day() and not late_day_volume_confirmed(metrics):
        return True, "Late-day trigger-ready setup lacks volume expansion", "FAILED_SETUP"

    if expired_by_age(signal, TRIGGER_READY_STALE_MINUTES):
        return True, "Trigger-ready setup became stale", "MISSED_WINDOW"

    if trigger > 0 and pct_change(metrics.price, trigger) > 2.0:
        return True, "Price moved too far above trigger without valid active signal", "MISSED_WINDOW"

    return False, "", ""


def suppress_trigger_during_blackout(
    signal: Dict[str, Any],
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    phase: str,
    regime: Dict[str, Any],
) -> Dict[str, Any]:
    now_text = iso_now_et()
    out = dict(signal)
    lunch_phase = is_lunch_blackout_phase(phase)
    out.update({
        "signal_status": "TRIGGER_READY",
        "actionable": False,
        "actionability": "LUNCH_CAUTION" if lunch_phase else "SUPPRESSED",
        "suppression_reason": "LUNCH_BLACKOUT_TRIGGER_READY_ONLY" if lunch_phase else f"{phase}_TRIGGER",
        "lunch_caution": bool(lunch_phase),
        "lunch_blackout_ready": bool(lunch_phase),
        "blackout_trigger_price": round(metrics.price, 4),
        "blackout_trigger_time": now_text,
        "requires_fresh_trigger": True,
        "entry_warning": LUNCH_CAUTION_WARNING if lunch_phase else "",
        "event_risk_warning": combine_warning(out.get("event_risk_warning", ""), LUNCH_CAUTION_WARNING) if lunch_phase else out.get("event_risk_warning", ""),
        "reason": (
            "Trigger fired during lunch blackout. No automatic Active Signal generated; manual chart confirmation required."
            if lunch_phase else
            f"Trigger fired during {phase.lower().replace('_', ' ')}. No active signal generated."
        ),
        "last_checked": now_text,
        "updated_at": now_text,
        "market_phase": phase,
        "price": round(metrics.price, 4),
        "price_source": metrics.price_source,
        "price_updated_at": metrics.price_updated_at,
        "latest_bar_time": timestamp_to_et_iso(metrics.latest_bar_time),
        "bid": round(metrics.bid, 4),
        "ask": round(metrics.ask, 4),
        "quote_mid": round(metrics.quote_mid, 4),
        "quote_time": metrics.quote_time,
        "trade_price": round(metrics.trade_price, 4),
        "trade_time": metrics.trade_time,
        "vwap": round(metrics.vwap, 4),
        "hod": round(metrics.hod, 4),
    })

    append_suppressed_signal({
        "timestamp_et": now_text,
        "symbol": normalize_symbol(row.get("symbol")),
        "setup_type": safe_str(signal.get("setup_type"), ""),
        "scanner_score": safe_float(row.get("score"), 0),
        "live_signal_score": safe_float(signal.get("live_signal_score"), 0),
        "confidence": safe_float(signal.get("confidence"), 0),
        "entry_trigger": safe_float(signal.get("entry_trigger"), 0),
        "stop_loss": safe_float(signal.get("stop_loss"), 0),
        "target_1": safe_float(signal.get("target_1"), 0),
        "target_2": safe_float(signal.get("target_2"), 0),
        "reward_risk": safe_float(signal.get("reward_risk"), 0),
        "suppression_reason": f"{phase}_TRIGGER",
        "price_at_trigger": metrics.price,
        "vwap": metrics.vwap,
        "hod_distance_pct": metrics.hod_distance_pct,
        "vwap_distance_pct": metrics.vwap_dist_pct,
        "sector_status": safe_str(row.get("sector_status"), ""),
        "market_regime": safe_str(regime.get("label"), ""),
        "notes": (
            "Trigger surfaced during lunch as caution-only; no automatic Active Signal."
            if is_lunch_blackout_phase(phase) else
            "Trigger suppressed by Signal Desk v1 blackout rule."
        ),
    })

    decision_log(
        normalize_symbol(row.get("symbol")),
        "TRIGGER_READY_LUNCH_CAUTION" if is_lunch_blackout_phase(phase) else "TRIGGER_SUPPRESSED_BLACKOUT",
        phase=phase,
        price=metrics.price,
        trigger=signal.get("entry_trigger"),
        reason=f"{phase}_TRIGGER",
    )

    return out



def trigger_ready_reassessment(
    existing: Dict[str, Any],
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    quote: Dict[str, Any],
    regime: Dict[str, Any],
    phase: str,
) -> Tuple[bool, Dict[str, Any], str]:
    """
    Reassess protected TRIGGER_READY signals on every refresh.

    This prevents stale Ready states from surviving after:
      - late-day confidence threshold rises,
      - EMA9 turns down,
      - MACD weakens,
      - volume fades,
      - setup structure is no longer ready.
    """
    setup_type = safe_str(existing.get("setup_type"), "")
    plan = build_trade_plan(row, metrics, setup_type, quote)
    live = live_signal_score(row, metrics, plan, regime, phase)
    conf = final_confidence(
        safe_float(row.get("score"), safe_float(existing.get("scanner_score"), 0)),
        live,
    )

    setup_ready_type, setup_reason, setup_fail_reasons = choose_setup(metrics, plan, conf, phase, row)
    existing_reclaim_lifecycle = is_reclaim_lifecycle_setup(existing)
    if existing_reclaim_lifecycle and not setup_ready_type:
        holding, hold_reason = reclaim_lifecycle_holding(existing, metrics)
        if holding:
            setup_ready_type = "RECLAIM_PULLBACK_HOLDING"
            setup_reason = hold_reason
        else:
            setup_fail_reasons.append(hold_reason)

    ready_min = setup_ready_conf_required(setup_ready_type, phase) if setup_ready_type else ready_confidence_required(phase)

    fail_reasons: List[str] = []

    if not plan.get("valid"):
        fail_reasons.append(f"Plan invalid: {safe_str(plan.get('rejection_reason'), 'Invalid plan')}")

    if safe_float(plan.get("reward_risk"), 0) < MIN_RR:
        fail_reasons.append(f"R/R {safe_float(plan.get('reward_risk'), 0):.2f} < minimum 1.5")

    if conf < ready_min:
        fail_reasons.append(f"Confidence {conf:.1f} < ready minimum {ready_min:.0f}")

    if existing_reclaim_lifecycle or setup_ready_type in {"VWAP_RECLAIM_BREAKOUT", "RECLAIM_PULLBACK_HOLDING"}:
        # Do not kill reclaim-pullback setups for a normal EMA9 dip or routine
        # volume cooling. Only hard-fail genuine structure loss / bearish divergence.
        if metrics.bearish_momentum_divergence:
            fail_reasons.append("Bearish MACD/momentum divergence")
        if is_late_day() and not late_day_volume_confirmed(metrics):
            fail_reasons.append("Late-day reclaim setup needs fresh volume expansion")
    else:
        macd_ok, macd_reason = macd_ready_confirmation(metrics)
        if not macd_ok:
            fail_reasons.append(macd_reason)

        ema_ok, ema_reason = ema9_ready_confirmation(metrics)
        if not ema_ok:
            fail_reasons.append(ema_reason)

        if metrics.volume_fading_vs_morning:
            fail_reasons.append(volume_fade_label(metrics))

        if is_late_day() and not late_day_volume_confirmed(metrics):
            fail_reasons.append("Late-day setup needs volume expansion")

    if not setup_ready_type:
        fail_reasons.extend(setup_fail_reasons)

    if fail_reasons:
        warning_text = "; ".join(dict.fromkeys(fail_reasons))

        # v2.5 discipline:
        # TRIGGER_READY must mean the setup is actually ready, not merely forming.
        # If a protected VWAP/reclaim setup has not been touched yet and the new
        # strict hold/proximity checks fail, invalidate it visibly so it can form
        # a fresh WATCH/new-base later instead of lingering as a misleading Ready.
        if (
            is_reclaim_lifecycle_setup_type(setup_type)
            and not trigger_was_touched_recently(existing, metrics)
            and (
                "keep WATCH" in warning_text
                or "not confirmed" in warning_text
                or "too fresh" in warning_text
                or "has not reclaimed EMA9" in warning_text
                or "Trigger " in warning_text
            )
        ):
            decision_log(
                normalize_symbol(row.get("symbol")),
                "TRIGGER_READY_REMOVED_NOT_CONFIRMED",
                reason=warning_text,
                confidence=conf,
                price=metrics.price,
                phase=phase,
            )
            return False, {}, warning_text

        # Protected Trigger Ready fallback:
        # Hard non-lifecycle warnings remain visible, but lifecycle readiness
        # failures above are no longer allowed to masquerade as Ready.
        out = dict(existing)
        out.update({
            "signal_status": "TRIGGER_READY",
            "actionable": bool(existing.get("actionable", False)),
            "actionability": safe_str(existing.get("actionability"), "TRIGGER_READY") or "TRIGGER_READY",
            "last_checked": iso_now_et(),
            "updated_at": iso_now_et(),
            "market_phase": phase,
            "price": round(metrics.price, 4) if metrics.has_data else out.get("price", 0),
            "price_source": metrics.price_source if metrics.has_data else out.get("price_source", ""),
            "price_updated_at": metrics.price_updated_at if metrics.has_data else out.get("price_updated_at", ""),
            "latest_bar_time": timestamp_to_et_iso(metrics.latest_bar_time) if metrics.has_data else out.get("latest_bar_time", ""),
            "bid": round(metrics.bid, 4) if metrics.has_data else out.get("bid", 0),
            "ask": round(metrics.ask, 4) if metrics.has_data else out.get("ask", 0),
            "quote_mid": round(metrics.quote_mid, 4) if metrics.has_data else out.get("quote_mid", 0),
            "quote_time": metrics.quote_time if metrics.has_data else out.get("quote_time", ""),
            "trade_price": round(metrics.trade_price, 4) if metrics.has_data else out.get("trade_price", 0),
            "trade_time": metrics.trade_time if metrics.has_data else out.get("trade_time", ""),
            "vwap": round(metrics.vwap, 4) if metrics.has_data else out.get("vwap", 0),
            "hod": round(metrics.hod, 4) if metrics.has_data else out.get("hod", 0),
            "vwap_dist_pct": round(metrics.vwap_dist_pct, 2) if metrics.has_data else out.get("vwap_dist_pct", 0),
            "hod_distance_pct": round(metrics.hod_distance_pct, 2) if metrics.has_data else out.get("hod_distance_pct", 0),
            "ema9": round(metrics.ema9, 4) if metrics.has_data else out.get("ema9", 0),
            "price_above_ema9": metrics.price_above_ema9 if metrics.has_data else out.get("price_above_ema9", False),
            "ema9_status": metrics.ema9_status if metrics.has_data else out.get("ema9_status", ""),
            "macd_value": round(metrics.macd_value, 4) if metrics.has_data else out.get("macd_value", 0),
            "macd_signal": round(metrics.macd_signal, 4) if metrics.has_data else out.get("macd_signal", 0),
            "macd_histogram": round(metrics.macd_histogram, 4) if metrics.has_data else out.get("macd_histogram", 0),
            "macd_status": metrics.macd_status if metrics.has_data else out.get("macd_status", ""),
            "momentum_status": metrics.momentum_status if metrics.has_data else out.get("momentum_status", ""),
            "confidence": round(conf, 1),
            "live_signal_score": round(live, 1),
            "not_ready_reasons": list(dict.fromkeys(fail_reasons)),
            "protected_ready_warning": warning_text,
            "reason": f"Protected Trigger Ready. Still locked; warning: {warning_text}",
        })
        out["ready_since"] = existing.get("ready_since") or existing.get("detected_at") or iso_now_et()
        out["detected_at"] = existing.get("detected_at") or out["ready_since"]

        decision_log(
            normalize_symbol(row.get("symbol")),
            "TRIGGER_READY_HELD_WITH_WARNINGS",
            reason=warning_text,
            confidence=conf,
            price=metrics.price,
            phase=phase,
        )
        return True, out, ""

    exact_plan = build_trade_plan(row, metrics, setup_ready_type, quote)
    exact_live = live_signal_score(row, metrics, exact_plan, regime, phase)
    exact_conf = final_confidence(
        safe_float(row.get("score"), safe_float(existing.get("scanner_score"), 0)),
        exact_live,
    )

    exact_ready_min = setup_ready_conf_required(setup_ready_type, phase)
    event_ok, event_reasons = earnings_reaction_requirements(row, metrics, exact_plan, exact_conf)
    if (
        not exact_plan.get("valid")
        or safe_float(exact_plan.get("reward_risk"), 0) < MIN_RR
        or exact_conf < exact_ready_min
        or not event_ok
    ):
        fail = []
        if not exact_plan.get("valid"):
            fail.append(f"Plan invalid: {safe_str(exact_plan.get('rejection_reason'), 'Invalid plan')}")
        if safe_float(exact_plan.get("reward_risk"), 0) < MIN_RR:
            fail.append(f"R/R {safe_float(exact_plan.get('reward_risk'), 0):.2f} < minimum 1.5")
        if exact_conf < exact_ready_min:
            fail.append(f"Confidence {exact_conf:.1f} < ready minimum {exact_ready_min:.0f}")
        if not event_ok:
            fail.extend(event_reasons)

        warning_text = "; ".join(dict.fromkeys(fail))
        if (
            is_reclaim_lifecycle_setup_type(setup_type)
            and not trigger_was_touched_recently(existing, metrics)
        ):
            decision_log(
                normalize_symbol(row.get("symbol")),
                "TRIGGER_READY_REMOVED_EXACT_RECHECK_FAILED",
                reason=warning_text,
                confidence=exact_conf,
                price=metrics.price,
                phase=phase,
            )
            return False, {}, warning_text

        out = dict(existing)
        out.update({
            "signal_status": "TRIGGER_READY",
            "last_checked": iso_now_et(),
            "updated_at": iso_now_et(),
            "market_phase": phase,
            "price": round(metrics.price, 4) if metrics.has_data else out.get("price", 0),
            "price_source": metrics.price_source if metrics.has_data else out.get("price_source", ""),
            "price_updated_at": metrics.price_updated_at if metrics.has_data else out.get("price_updated_at", ""),
            "latest_bar_time": timestamp_to_et_iso(metrics.latest_bar_time) if metrics.has_data else out.get("latest_bar_time", ""),
            "bid": round(metrics.bid, 4) if metrics.has_data else out.get("bid", 0),
            "ask": round(metrics.ask, 4) if metrics.has_data else out.get("ask", 0),
            "quote_mid": round(metrics.quote_mid, 4) if metrics.has_data else out.get("quote_mid", 0),
            "quote_time": metrics.quote_time if metrics.has_data else out.get("quote_time", ""),
            "trade_price": round(metrics.trade_price, 4) if metrics.has_data else out.get("trade_price", 0),
            "trade_time": metrics.trade_time if metrics.has_data else out.get("trade_time", ""),
            "vwap": round(metrics.vwap, 4) if metrics.has_data else out.get("vwap", 0),
            "hod": round(metrics.hod, 4) if metrics.has_data else out.get("hod", 0),
            "vwap_dist_pct": round(metrics.vwap_dist_pct, 2) if metrics.has_data else out.get("vwap_dist_pct", 0),
            "hod_distance_pct": round(metrics.hod_distance_pct, 2) if metrics.has_data else out.get("hod_distance_pct", 0),
            "ema9": round(metrics.ema9, 4) if metrics.has_data else out.get("ema9", 0),
            "price_above_ema9": metrics.price_above_ema9 if metrics.has_data else out.get("price_above_ema9", False),
            "ema9_status": metrics.ema9_status if metrics.has_data else out.get("ema9_status", ""),
            "macd_value": round(metrics.macd_value, 4) if metrics.has_data else out.get("macd_value", 0),
            "macd_signal": round(metrics.macd_signal, 4) if metrics.has_data else out.get("macd_signal", 0),
            "macd_histogram": round(metrics.macd_histogram, 4) if metrics.has_data else out.get("macd_histogram", 0),
            "macd_status": metrics.macd_status if metrics.has_data else out.get("macd_status", ""),
            "momentum_status": metrics.momentum_status if metrics.has_data else out.get("momentum_status", ""),
            "confidence": round(exact_conf, 1),
            "live_signal_score": round(exact_live, 1),
            "not_ready_reasons": list(dict.fromkeys(fail)),
            "protected_ready_warning": warning_text,
            "reason": f"Protected Trigger Ready. Still locked; warning: {warning_text}",
        })
        out["ready_since"] = existing.get("ready_since") or existing.get("detected_at") or iso_now_et()
        out["detected_at"] = existing.get("detected_at") or out["ready_since"]

        decision_log(
            normalize_symbol(row.get("symbol")),
            "TRIGGER_READY_HELD_WITH_EXACT_WARNINGS",
            reason=warning_text,
            confidence=exact_conf,
            price=metrics.price,
            phase=phase,
        )
        return True, out, ""

    out = signal_base(
        row,
        metrics,
        exact_plan,
        "TRIGGER_READY",
        setup_ready_type,
        exact_conf,
        exact_live,
        setup_reason,
        phase,
        regime,
    )
    out["ready_since"] = existing.get("ready_since") or iso_now_et()
    out["detected_at"] = existing.get("detected_at") or existing.get("ready_since") or iso_now_et()
    decision_log(
        normalize_symbol(row.get("symbol")),
        "TRIGGER_READY_RECONFIRMED",
        setup=setup_ready_type,
        confidence=exact_conf,
        trigger=exact_plan.get("entry_trigger"),
        price=metrics.price,
        phase=phase,
    )
    return True, out, ""



def locked_plan_from_signal(signal: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a trade plan from the protected TRIGGER_READY state.

    This prevents ACTIVE promotion from rebuilding entry/stop/targets after price
    already moved. The trigger-ready plan is the locked execution plan.
    """
    entry = safe_float(signal.get("entry_trigger"), 0)
    stop = safe_float(signal.get("stop_loss"), 0)
    target_1 = safe_float(signal.get("target_1"), 0)
    target_2 = safe_float(signal.get("target_2"), 0)
    rr = safe_float(signal.get("reward_risk"), 0)

    valid = entry > 0 and stop > 0 and target_1 > entry and stop < entry and rr >= MIN_RR

    return {
        "valid": valid,
        "rejection_reason": "" if valid else "Locked Trigger Ready plan is invalid",
        "entry_trigger": round(entry, 4),
        "stop_loss": round(stop, 4),
        "target_1": round(target_1, 4),
        "target_2": round(target_2, 4),
        "reward_risk": round(rr, 2),
        "stop_distance_pct": safe_float(signal.get("stop_distance_pct"), 0),
        "support_level": safe_float(signal.get("support_level"), 0),
        "structure_label": safe_str(signal.get("structure_label"), "LOCKED_TRIGGER_READY_PLAN"),
        "min_target_1": safe_float(signal.get("min_target_1"), target_1),
        "buffer_pct_used": safe_float(signal.get("buffer_pct_used"), 0),
        "spread_dollars": safe_float(signal.get("spread_dollars"), 0),
    }


def active_confidence_grade(confidence: float) -> str:
    if confidence >= 90:
        return "HIGH_CONVICTION"
    if confidence >= 85:
        return "STRONG"
    return "MODERATE"


def latest_execution_bar(metrics: IntradayMetrics) -> Dict[str, Any]:
    bars = clean_bars(metrics.execution_bars or [])
    return bars[-1] if bars else {}


def trigger_touch_age_seconds(signal: Dict[str, Any]) -> Optional[float]:
    touched_at = signal.get("trigger_touched_at")
    dt = parse_iso_dt(touched_at)
    if not dt:
        return None
    now = ny_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    if now.tzinfo and dt.tzinfo:
        dt = dt.astimezone(now.tzinfo)
    return max(0.0, (now - dt).total_seconds())


def trigger_hold_confirmation(
    existing: Dict[str, Any],
    metrics: IntradayMetrics,
    setup_type: str = "",
) -> Tuple[bool, str]:
    """
    Confirmation layer between TRIGGER_READY and ACTIVE_SIGNAL.

    A trigger touch alone is not enough. This prevents one-candle HOD taps from
    being marked ACTIVE and then invalidated immediately.
    """
    if not metrics.has_data:
        return False, "No intraday data available for trigger confirmation"

    trigger = safe_float(existing.get("entry_trigger"), 0)
    if trigger <= 0:
        return False, "No locked trigger price available"

    if metrics.price < trigger:
        return False, f"Current price {metrics.price:.2f} is back below trigger {trigger:.2f}"

    if not metrics.above_vwap:
        return False, "Price is not above VWAP after trigger touch"

    setup = safe_str(setup_type or existing.get("setup_type"), "").upper()
    reclaim_family = is_reclaim_lifecycle_setup_type(setup)

    # A trigger must survive at least one follow-up refresh before Active.
    age_seconds = trigger_touch_age_seconds(existing)
    min_confirm_seconds = RECLAIM_ACTIVE_CONFIRM_MIN_SECONDS if reclaim_family else TRIGGER_TOUCH_CONFIRM_MIN_SECONDS
    if age_seconds is not None and age_seconds < min_confirm_seconds:
        return False, (
            f"Trigger touched {age_seconds:.0f}s ago; waiting for next 1-minute confirmation "
            f"before Active Signal"
        )

    last_bar = latest_execution_bar(metrics)
    close = safe_float(last_bar.get("c"), metrics.price)
    high = safe_float(last_bar.get("h"), metrics.price)
    low = safe_float(last_bar.get("l"), metrics.price)

    if close > 0 and close < trigger:
        return False, f"Latest 1-minute close {close:.2f} did not hold above trigger {trigger:.2f}"

    if high > trigger and close > 0:
        wick_reject_pct = pct_change(high, max(close, trigger))
        if wick_reject_pct >= TRIGGER_WICK_REJECTION_PCT and close <= trigger * 1.002:
            return False, f"Breakout wick rejected near trigger ({wick_reject_pct:.2f}% upper rejection)"

    if not reclaim_family:
        if metrics.ema9 > 0 and not metrics.price_above_ema9:
            return False, "Price is below EMA9 after trigger touch"

        if metrics.volume_fading_vs_morning:
            return False, volume_fade_label(metrics) + " on trigger confirmation"

        if "HOD" in setup or "BASE" in setup:
            if not metrics.recent_volume_expanding and (
                metrics.avg_volume_1m_20 > 0 and metrics.avg_volume_1m_5 < metrics.avg_volume_1m_20 * 0.85
            ):
                return False, "Breakout trigger lacks fresh 1-minute volume expansion"

    else:
        close_ok, close_reason = reclaim_active_confirmation_quality(existing, metrics, setup)
        if not close_ok:
            return False, close_reason

        holding, hold_reason = reclaim_lifecycle_holding(existing, metrics)
        if not holding:
            return False, hold_reason

        macd_ok, macd_reason = vwap_lifecycle_macd_confirmation(metrics, setup)
        if not macd_ok:
            return False, macd_reason

    return True, "Trigger held above entry with valid VWAP/volume confirmation"


def trigger_reference_high(metrics: IntradayMetrics) -> float:
    """
    Highest recent price used to detect whether a locked trigger has already traded.

    Important: use recent high/current trade, not all-day HOD. A pullback setup can
    validly have an entry below an old morning HOD. The problem we are fixing is a
    *recently touched* entry being shown again as fresh Trigger Ready.
    """
    vals = [
        safe_float(getattr(metrics, "price_recent_high", 0), 0),
        safe_float(getattr(metrics, "price", 0), 0),
        safe_float(getattr(metrics, "trade_price", 0), 0),
        safe_float(getattr(metrics, "quote_mid", 0), 0),
    ]
    return max([v for v in vals if v > 0] or [0.0])


def trigger_was_touched_recently(signal: Dict[str, Any], metrics: IntradayMetrics) -> bool:
    entry = safe_float(signal.get("entry_trigger"), 0)
    if entry <= 0 or not metrics.has_data:
        return False

    eps_level = entry * (1.0 - TRIGGER_TOUCH_EPS_PCT / 100.0)
    return trigger_reference_high(metrics) >= eps_level


def trigger_is_above_current_price(signal: Dict[str, Any], metrics: IntradayMetrics) -> bool:
    entry = safe_float(signal.get("entry_trigger"), 0)
    return entry > 0 and metrics.price > 0 and metrics.price < entry * (1.0 - TRIGGER_TOUCH_EPS_PCT / 100.0)


def trigger_pullback_after_touch(signal: Dict[str, Any], metrics: IntradayMetrics) -> Tuple[bool, str]:
    """
    True when a trigger was touched/exceeded but the stock has since pulled away
    enough that the old entry is stale. This avoids RIG/BN behavior:
      Trigger Ready -> entry touched -> falls back to Watch / stale Ready.

    A stale touched trigger should become NEW_BASE_REQUIRED / REJECTED_TRIGGER.
    """
    entry = safe_float(signal.get("entry_trigger"), 0)
    if entry <= 0 or not metrics.has_data:
        return False, ""

    if metrics.price >= entry * (1.0 - TRIGGER_TOUCH_EPS_PCT / 100.0):
        return False, ""

    pullback_pct = pct_change(entry, metrics.price)
    bearish_confirmation = (
        (metrics.ema9 > 0 and not metrics.price_above_ema9)
        or metrics.macd_1m_curling_down
        or metrics.macd_histogram_falling
        or metrics.bearish_momentum_divergence
    )

    if pullback_pct >= TRIGGER_PULLBACK_REJECT_PCT:
        return True, (
            f"Trigger was touched/exceeded, then price pulled back {pullback_pct:.2f}% below entry; "
            "old trigger is stale. New base required."
        )

    if bearish_confirmation and pullback_pct >= TRIGGER_TOUCH_EPS_PCT:
        return True, (
            "Trigger was touched/exceeded, then price fell back below trigger with weakening EMA/MACD; "
            "new base required."
        )

    return False, ""


def keep_trigger_touched_pending(
    existing: Dict[str, Any],
    metrics: IntradayMetrics,
    phase: str,
    reason: str,
) -> Dict[str, Any]:
    """
    Keep a touched trigger visible as TRIGGER_TOUCHED instead of demoting it back
    to WATCH or recycled Trigger Ready. The next clean re-break/hold can still
    promote it; a failed pullback becomes NEW_BASE_REQUIRED.
    """
    now_text = iso_now_et()
    out = dict(existing)
    out.update({
        "signal_status": "TRIGGER_TOUCHED",
        "actionable": False,
        "actionability": "TRIGGER_TOUCHED",
        "last_checked": now_text,
        "updated_at": now_text,
        "market_phase": phase,
        "price": round(metrics.price, 4) if metrics.has_data else out.get("price", 0),
        "price_source": metrics.price_source if metrics.has_data else out.get("price_source", ""),
        "price_updated_at": metrics.price_updated_at if metrics.has_data else out.get("price_updated_at", ""),
        "latest_bar_time": timestamp_to_et_iso(metrics.latest_bar_time) if metrics.has_data else out.get("latest_bar_time", ""),
        "reason": f"Trigger touched; still pending confirmation: {reason}",
        "entry_warning": "Trigger already traded. Do not treat as fresh entry; wait for clean re-break/hold or new base.",
    })
    decision_log(
        normalize_symbol(out.get("symbol")),
        "TRIGGER_TOUCH_STILL_PENDING",
        trigger=out.get("entry_trigger"),
        price=metrics.price,
        phase=phase,
        reason=reason,
    )
    return out


def make_trigger_touched(
    existing: Dict[str, Any],
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    phase: str,
    reason: str = "",
) -> Dict[str, Any]:
    """
    First step after a Trigger Ready entry price is touched.

    This is a protected, non-actionable confirmation state. It stays visible in
    the Trigger Ready column but is not an Active Signal until a later refresh
    confirms hold/volume quality.
    """
    now_text = iso_now_et()
    out = dict(existing)
    symbol = normalize_symbol(existing.get("symbol") or row.get("symbol"))

    setup_type = safe_str(out.get("setup_type"), "")
    message = reason or "Entry trigger touched; waiting for 1-minute hold/volume confirmation before Active Signal."

    out.update({
        "symbol": symbol,
        "signal_status": "TRIGGER_TOUCHED",
        "actionable": False,
        "actionability": "TRIGGER_TOUCHED",
        "trigger_touched_at": out.get("trigger_touched_at") or now_text,
        "trigger_touched_price": round(metrics.price, 4) if metrics.has_data else out.get("trigger_touched_price", 0),
        "reason": message,
        "entry_warning": "Trigger touched; not Active yet. Waiting for confirmation to avoid one-candle fakeout.",
        "last_checked": now_text,
        "updated_at": now_text,
        "market_phase": phase,
        "price": round(metrics.price, 4) if metrics.has_data else out.get("price", 0),
        "price_source": metrics.price_source if metrics.has_data else out.get("price_source", ""),
        "price_updated_at": metrics.price_updated_at if metrics.has_data else out.get("price_updated_at", ""),
        "latest_bar_time": timestamp_to_et_iso(metrics.latest_bar_time) if metrics.has_data else out.get("latest_bar_time", ""),
        "bid": round(metrics.bid, 4) if metrics.has_data else out.get("bid", 0),
        "ask": round(metrics.ask, 4) if metrics.has_data else out.get("ask", 0),
        "quote_mid": round(metrics.quote_mid, 4) if metrics.has_data else out.get("quote_mid", 0),
        "quote_time": metrics.quote_time if metrics.has_data else out.get("quote_time", ""),
        "trade_price": round(metrics.trade_price, 4) if metrics.has_data else out.get("trade_price", 0),
        "trade_time": metrics.trade_time if metrics.has_data else out.get("trade_time", ""),
        "vwap": round(metrics.vwap, 4) if metrics.has_data else out.get("vwap", 0),
        "hod": round(metrics.hod, 4) if metrics.has_data else out.get("hod", 0),
        "vwap_dist_pct": round(metrics.vwap_dist_pct, 2) if metrics.has_data else out.get("vwap_dist_pct", 0),
        "hod_distance_pct": round(metrics.hod_distance_pct, 2) if metrics.has_data else out.get("hod_distance_pct", 0),
        "ema9": round(metrics.ema9, 4) if metrics.has_data else out.get("ema9", 0),
        "price_above_ema9": metrics.price_above_ema9 if metrics.has_data else out.get("price_above_ema9", False),
        "macd_value": round(metrics.macd_value, 4) if metrics.has_data else out.get("macd_value", 0),
        "macd_signal": round(metrics.macd_signal, 4) if metrics.has_data else out.get("macd_signal", 0),
        "macd_histogram": round(metrics.macd_histogram, 4) if metrics.has_data else out.get("macd_histogram", 0),
    })

    decision_log(
        symbol,
        "TRIGGER_TOUCHED_CONFIRMING",
        setup=setup_type,
        trigger=out.get("entry_trigger"),
        price=metrics.price,
        phase=phase,
        reason=message,
    )
    return out


def reset_to_trigger_ready_after_touch(
    existing: Dict[str, Any],
    metrics: IntradayMetrics,
    phase: str,
    reason: str,
) -> Dict[str, Any]:
    """
    If a touched trigger does not confirm but structure is still alive, return
    it to protected Trigger Ready instead of invalidating immediately.
    """
    now_text = iso_now_et()
    out = dict(existing)
    out.update({
        "signal_status": "TRIGGER_READY",
        "actionable": False,
        "actionability": "TRIGGER_READY",
        "trigger_touched_failed_at": now_text,
        "trigger_touched_failed_reason": reason,
        "reason": f"Trigger touched but did not confirm yet: {reason}. Setup remains protected while structure holds.",
        "entry_warning": "Prior trigger touch did not confirm. Wait for a fresh clean break/hold.",
        "last_checked": now_text,
        "updated_at": now_text,
        "market_phase": phase,
        "price": round(metrics.price, 4) if metrics.has_data else out.get("price", 0),
        "price_source": metrics.price_source if metrics.has_data else out.get("price_source", ""),
        "price_updated_at": metrics.price_updated_at if metrics.has_data else out.get("price_updated_at", ""),
        "latest_bar_time": timestamp_to_et_iso(metrics.latest_bar_time) if metrics.has_data else out.get("latest_bar_time", ""),
    })
    # Clear active confirmation wait so a new touch can be evaluated fresh.
    out.pop("trigger_touched_at", None)
    out.pop("trigger_touched_price", None)

    decision_log(
        normalize_symbol(out.get("symbol")),
        "TRIGGER_TOUCH_RESET_TO_READY",
        trigger=out.get("entry_trigger"),
        price=metrics.price,
        phase=phase,
        reason=reason,
    )
    return out


def touched_trigger_rejected(existing: Dict[str, Any], metrics: IntradayMetrics) -> Tuple[bool, str]:
    """
    Decide whether a TRIGGER_TOUCHED state failed badly enough to invalidate.
    Mild pullbacks simply reset to Trigger Ready.
    """
    trigger = safe_float(existing.get("entry_trigger"), 0)
    if trigger <= 0 or not metrics.has_data:
        return False, ""

    stop = safe_float(existing.get("stop_loss"), 0)
    if stop > 0 and metrics.price <= stop:
        return True, "Trigger touched but stop level was hit before active confirmation"

    if is_reclaim_lifecycle_setup(existing):
        # Do not reject a reclaim touch on one VWAP tick. Let
        # trigger_hold_confirmation decide whether the latest candle actually
        # confirmed; invalidate only on hard support failure.
        holding, hold_reason = reclaim_lifecycle_holding(existing, metrics)
        if not holding:
            return True, f"Trigger touched but reclaim structure failed: {hold_reason}"
        return False, ""

    if not metrics.above_vwap:
        return True, "Trigger touched but price lost VWAP before active confirmation"

    reject_level = trigger * (1.0 - TRIGGER_REJECT_BUFFER_PCT / 100.0)
    if metrics.price < reject_level and metrics.ema9 > 0 and not metrics.price_above_ema9:
        return True, (
            f"Trigger touched but rejected: price {metrics.price:.2f} fell below "
            f"trigger {trigger:.2f} and EMA9 confirmation failed"
        )

    return False, ""


def active_promotion_failure_reasons(
    plan: Dict[str, Any],
    confidence: float,
    metrics: IntradayMetrics,
    phase: str,
    setup_type: str = "",
    row: Optional[Dict[str, Any]] = None,
) -> List[str]:
    reasons: List[str] = []

    if not plan.get("valid"):
        reasons.append(safe_str(plan.get("rejection_reason"), "Plan invalid"))

    if confidence < MIN_CONF_ACTIVE:
        reasons.append(f"Confidence {confidence:.1f} < active minimum {MIN_CONF_ACTIVE:.0f}")

    if safe_float(plan.get("reward_risk"), 0) < MIN_RR:
        reasons.append(f"R/R {safe_float(plan.get('reward_risk'), 0):.2f} < minimum {MIN_RR:.1f}")

    if not metrics.above_vwap:
        reasons.append("Price is not above VWAP")

    reclaim_family = is_reclaim_lifecycle_setup_type(setup_type)

    if reclaim_family:
        # Reclaim active signals need candle-close confirmation. A tick above the
        # trigger is not enough.
        close_ok, close_reason = reclaim_active_confirmation_quality(plan, metrics, setup_type)
        if not close_ok:
            reasons.append(close_reason)

        macd_ok, macd_reason = vwap_lifecycle_macd_confirmation(metrics, setup_type)
        if not macd_ok:
            reasons.append(macd_reason)

        if metrics.ema9_falling and not metrics.reclaim_pullback_holding and metrics.vwap_dist_pct <= 0.35:
            reasons.append("EMA9 falling while reclaim support is not clearly holding")
    else:
        if metrics.ema9 > 0 and not metrics.price_above_ema9:
            reasons.append("Price is below EMA9")

        if metrics.ema9_falling:
            reasons.append("EMA9 is falling")

        macd_ok, macd_reason = macd_ready_confirmation(metrics)
        if not macd_ok:
            reasons.append(macd_reason)

        if metrics.volume_fading_vs_morning:
            reasons.append(volume_fade_label(metrics))

    if row is not None:
        event_ok, event_reasons = earnings_reaction_requirements(row, metrics, plan, confidence)
        if not event_ok:
            reasons.extend(event_reasons)

    if is_late_day() and not late_day_volume_confirmed(metrics):
        reasons.append("Late-day active signal needs volume expansion")

    if not is_valid_signal_phase(phase):
        reasons.append(f"Market phase {phase} does not allow new active signals")

    return list(dict.fromkeys([r for r in reasons if r]))


def promote_trigger_ready_to_active(
    existing: Dict[str, Any],
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    regime: Dict[str, Any],
    phase: str,
) -> Tuple[bool, Dict[str, Any], str]:
    """
    Promote TRIGGER_READY to ACTIVE_SIGNAL using the locked plan.

    This is intentionally evaluated before stale/missed-window invalidation.
    A valid trigger should not be marked MISSED_WINDOW only because price has
    already moved above the trigger by the time the 5-minute refresh runs.
    """
    plan = locked_plan_from_signal(existing)
    live = live_signal_score(row, metrics, plan, regime, phase)
    recomputed_conf = final_confidence(
        safe_float(row.get("score"), safe_float(existing.get("scanner_score"), 0)),
        live,
    )
    conf = max(safe_float(existing.get("confidence"), 0), recomputed_conf)

    failure_reasons = active_promotion_failure_reasons(plan, conf, metrics, phase, safe_str(existing.get("setup_type"), ""), row)
    if failure_reasons:
        decision_log(
            normalize_symbol(row.get("symbol")),
            "PROMOTION_BLOCKED",
            reason="; ".join(failure_reasons),
            confidence=conf,
            trigger=plan.get("entry_trigger"),
            price=metrics.price,
            phase=phase,
        )
        return False, {}, "; ".join(failure_reasons)

    now_text = iso_now_et()
    setup_type = safe_str(existing.get("setup_type"), "")

    out = signal_base(
        row,
        metrics,
        plan,
        "ACTIVE_SIGNAL",
        setup_type,
        conf,
        live,
        f"Trigger fired and still holds above VWAP. Promoted from protected Trigger Ready.",
        phase,
        regime,
    )
    out["triggered_at"] = now_text
    out["detected_at"] = existing.get("detected_at") or existing.get("ready_since") or now_text
    out["ready_since"] = existing.get("ready_since") or now_text
    out["active_grade"] = active_confidence_grade(conf)
    out["trigger_source"] = metrics.price_source

    entry = safe_float(plan.get("entry_trigger"), 0)
    target_1 = safe_float(plan.get("target_1"), 0)
    target_2 = safe_float(plan.get("target_2"), 0)

    if target_2 > 0 and metrics.price >= target_2:
        out["entry_warning"] = "Triggered but current price is already at/above Target 2. No chase; wait for a fresh pullback."
        out["actionability"] = "ACTIVE_EXTENDED"
    elif target_1 > 0 and metrics.price >= target_1:
        out["entry_warning"] = "Triggered but current price is already at/above Target 1. Manual confirmation required; avoid chasing."
        out["actionability"] = "ACTIVE_EXTENDED"
    elif entry > 0 and pct_change(metrics.price, entry) > 2.0:
        out["entry_warning"] = "Triggered but price is more than 2% above the trigger. Manual confirmation required; avoid chasing."

    decision_log(
        normalize_symbol(row.get("symbol")),
        "PROMOTED_TO_ACTIVE_SIGNAL",
        setup=setup_type,
        grade=out.get("active_grade"),
        confidence=conf,
        trigger=entry,
        price=metrics.price,
        target_1=target_1,
        target_2=target_2,
        actionability=out.get("actionability"),
        phase=phase,
    )

    return True, out, ""

def process_existing_signal(
    existing: Dict[str, Any],
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    quote: Dict[str, Any],
    regime: Dict[str, Any],
    phase: str,
) -> Dict[str, Any]:
    status = normalize_status(existing.get("signal_status"))
    now_text = iso_now_et()

    if is_previous_session(existing):
        return make_invalidated(existing, row, metrics, "Previous session signal expired.", "MISSED_WINDOW", phase)

    if status == "ACTIVE_SIGNAL":
        invalid, reason, category = active_invalidated(existing, metrics)
        if invalid:
            return make_invalidated(existing, row, metrics, reason, category, phase)

        out = dict(existing)
        out.update({
            "last_checked": now_text,
            "updated_at": now_text,
            "market_phase": phase,
            "price": round(metrics.price, 4),
            "price_source": metrics.price_source,
            "price_updated_at": metrics.price_updated_at,
            "latest_bar_time": timestamp_to_et_iso(metrics.latest_bar_time),
            "bid": round(metrics.bid, 4),
            "ask": round(metrics.ask, 4),
            "quote_mid": round(metrics.quote_mid, 4),
            "quote_time": metrics.quote_time,
            "trade_price": round(metrics.trade_price, 4),
            "trade_time": metrics.trade_time,
            "vwap": round(metrics.vwap, 4),
            "hod": round(metrics.hod, 4),
            "vwap_dist_pct": round(metrics.vwap_dist_pct, 2),
            "hod_distance_pct": round(metrics.hod_distance_pct, 2),
            "ema9": round(metrics.ema9, 4),
            "price_above_ema9": metrics.price_above_ema9,
            "ema9_status": metrics.ema9_status,
            "macd_value": round(metrics.macd_value, 4),
            "macd_signal": round(metrics.macd_signal, 4),
            "macd_histogram": round(metrics.macd_histogram, 4),
            "macd_status": metrics.macd_status,
            "momentum_status": metrics.momentum_status,
            "actionable": True,
            "actionability": safe_str(existing.get("actionability"), "ACTIVE") or "ACTIVE",
        })
        return out

    if status == "TRIGGER_READY":
        fired_now = trigger_fired(existing, metrics)
        touched_recently = trigger_was_touched_recently(existing, metrics)

        if touched_recently and not fired_now:
            stale, stale_reason = trigger_pullback_after_touch(existing, metrics)
            if stale:
                return make_invalidated(existing, row, metrics, stale_reason, "NEW_BASE_REQUIRED", phase)

            return make_trigger_touched(
                existing,
                row,
                metrics,
                phase,
                "Entry was already touched recently; waiting for renewed hold/volume confirmation before Active Signal.",
            )

        if fired_now:
            decision_log(
                normalize_symbol(row.get("symbol")),
                "TRIGGER_CROSSED",
                trigger=existing.get("entry_trigger"),
                price=metrics.price,
                phase=phase,
                price_source=metrics.price_source,
            )

        # Blackout remains strict: a trigger during blackout is suppressed,
        # never retroactively promoted.
        if fired_now and not is_valid_signal_phase(phase):
            return suppress_trigger_during_blackout(existing, row, metrics, phase, regime)

        # New confirmation layer:
        # Price touching the entry does NOT become ACTIVE immediately. It first
        # becomes TRIGGER_TOUCHED, then the next refresh must confirm hold/volume.
        if fired_now and is_valid_signal_phase(phase):
            return make_trigger_touched(
                existing,
                row,
                metrics,
                phase,
                "Entry trigger touched; waiting for the next refresh to confirm hold/volume before Active Signal.",
            )

        invalid, reason, category = ready_invalidated(existing, metrics)
        if invalid:
            return make_invalidated(existing, row, metrics, reason, category, phase)

        ready_ok, reassessed_signal, reassess_reason = trigger_ready_reassessment(
            existing,
            row,
            metrics,
            quote,
            regime,
            phase,
        )

        if not ready_ok:
            if reassessed_signal:
                return reassessed_signal

            return make_invalidated(
                existing,
                row,
                metrics,
                f"Trigger-ready setup no longer valid: {reassess_reason}",
                "FAILED_SETUP",
                phase,
            )

        # Not triggered yet; return refreshed Trigger Ready payload.
        return reassessed_signal

    if status == "TRIGGER_TOUCHED":
        if not is_valid_signal_phase(phase):
            return suppress_trigger_during_blackout(existing, row, metrics, phase, regime)

        stale, stale_reason = trigger_pullback_after_touch(existing, metrics)
        if stale:
            return make_invalidated(existing, row, metrics, stale_reason, "NEW_BASE_REQUIRED", phase)

        invalid, reason, category = ready_invalidated(existing, metrics)
        if invalid:
            return make_invalidated(existing, row, metrics, reason, category, phase)

        rejected, reject_reason = touched_trigger_rejected(existing, metrics)
        if rejected:
            return make_invalidated(existing, row, metrics, reject_reason, "FAILED_TRIGGER_CONFIRMATION", phase)

        confirmed, confirm_reason = trigger_hold_confirmation(
            existing,
            metrics,
            safe_str(existing.get("setup_type"), ""),
        )

        if confirmed:
            promoted, active_signal, fail_reason = promote_trigger_ready_to_active(
                existing,
                row,
                metrics,
                regime,
                phase,
            )

            if promoted:
                active_signal["trigger_touched_at"] = existing.get("trigger_touched_at")
                active_signal["trigger_confirmation_reason"] = confirm_reason
                return active_signal

            age_seconds = trigger_touch_age_seconds(existing)
            if age_seconds is not None and age_seconds / 60.0 >= TRIGGER_TOUCH_MAX_MINUTES:
                return make_invalidated(
                    existing,
                    row,
                    metrics,
                    f"Trigger touched but failed Active confirmation within {TRIGGER_TOUCH_MAX_MINUTES} minutes: {fail_reason}",
                    "FAILED_TRIGGER_CONFIRMATION",
                    phase,
                )

            out = dict(existing)
            out.update({
                "last_checked": now_text,
                "updated_at": now_text,
                "market_phase": phase,
                "price": round(metrics.price, 4),
                "price_source": metrics.price_source,
                "price_updated_at": metrics.price_updated_at,
                "latest_bar_time": timestamp_to_et_iso(metrics.latest_bar_time),
                "reason": f"Trigger touched; Active confirmation still pending: {fail_reason}",
                "entry_warning": "Trigger touched but active quality is not confirmed yet. Manual review only.",
            })
            decision_log(
                normalize_symbol(row.get("symbol")),
                "TRIGGER_TOUCH_CONFIRMATION_PENDING",
                trigger=existing.get("entry_trigger"),
                price=metrics.price,
                phase=phase,
                reason=fail_reason,
            )
            return out

        age_seconds = trigger_touch_age_seconds(existing)
        if age_seconds is not None and age_seconds / 60.0 >= TRIGGER_TOUCH_MAX_MINUTES:
            if is_reclaim_lifecycle_setup(existing):
                holding, hold_reason = reclaim_lifecycle_holding(existing, metrics)
                if holding:
                    return keep_trigger_touched_pending(existing, metrics, phase, f"{confirm_reason}; {hold_reason}")

            return make_invalidated(
                existing,
                row,
                metrics,
                f"Trigger touched but failed hold/volume confirmation within {TRIGGER_TOUCH_MAX_MINUTES} minutes: {confirm_reason}",
                "FAILED_TRIGGER_CONFIRMATION",
                phase,
            )

        out = dict(existing)
        out.update({
            "last_checked": now_text,
            "updated_at": now_text,
            "market_phase": phase,
            "price": round(metrics.price, 4) if metrics.has_data else out.get("price", 0),
            "price_source": metrics.price_source if metrics.has_data else out.get("price_source", ""),
            "price_updated_at": metrics.price_updated_at if metrics.has_data else out.get("price_updated_at", ""),
            "latest_bar_time": timestamp_to_et_iso(metrics.latest_bar_time) if metrics.has_data else out.get("latest_bar_time", ""),
            "reason": f"Trigger touched; waiting for confirmation: {confirm_reason}",
            "entry_warning": "Touched trigger but not confirmed Active yet. Avoid same-candle fakeout.",
        })
        return out

    if status == "INVALIDATED":
        age = minutes_since(existing.get("invalidated_at") or existing.get("updated_at"))
        if age is not None and age <= RECENT_INVALIDATED_KEEP_MINUTES:
            out = dict(existing)
            out["last_checked"] = now_text
            return out

    return {}


def log_watch_evaluated(symbol: str, signal: Dict[str, Any], metrics: IntradayMetrics, phase: str) -> None:
    decision_log(
        symbol,
        "WATCH_EVALUATED",
        setup=signal.get("setup_type"),
        confidence=safe_float(signal.get("confidence"), 0),
        rr=signal.get("reward_risk"),
        price=metrics.price,
        vwap_dist_pct=metrics.vwap_dist_pct,
        hod_distance_pct=metrics.hod_distance_pct,
        reason=signal.get("reason"),
        not_ready="; ".join(signal.get("not_ready_reasons", [])[:4]) if isinstance(signal.get("not_ready_reasons"), list) else "",
        phase=phase,
    )


def log_watch_rejected(symbol: str, reasons: List[str], confidence: float, metrics: IntradayMetrics, phase: str) -> None:
    decision_log(
        symbol,
        "WATCH_REJECTED",
        reason="; ".join(dict.fromkeys([safe_str(x) for x in reasons if x])),
        confidence=confidence,
        price=metrics.price if metrics and metrics.has_data else 0,
        vwap_dist_pct=metrics.vwap_dist_pct if metrics and metrics.has_data else 0,
        phase=phase,
    )


def process_new_or_watch(
    row: Dict[str, Any],
    metrics: IntradayMetrics,
    quote: Dict[str, Any],
    regime: Dict[str, Any],
    phase: str,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """
    Returns:
      (signal, diagnostic)
    Exactly one can be non-empty.
    """
    symbol = normalize_symbol(row.get("symbol"))

    if not metrics.has_data:
        decision_log(symbol, "WATCH_REJECTED", reason="No intraday bars", phase=phase)
        return {}, None

    watch_ok, watch_reasons, rs_long = watch_criteria_pass(row, metrics, regime)

    setup_type, plan = choose_best_provisional_plan(row, metrics, quote)
    live = live_signal_score(row, metrics, plan, regime, phase)
    conf = final_confidence(safe_float(row.get("score"), 0), live)

    rejected_reasons = list(watch_reasons)

    if conf < MIN_CONF_WATCH:
        rejected_reasons.append(f"Confidence {conf:.1f} < WATCH minimum 60")

    if not plan.get("valid"):
        rejected_reasons.append(f"Plan invalid: {safe_str(plan.get('rejection_reason'), 'Invalid plan')}")

    if plan.get("valid") and safe_float(plan.get("reward_risk"), 0) < MIN_RR_WATCH:
        rejected_reasons.append(f"R/R {safe_float(plan.get('reward_risk'), 0):.2f} < WATCH minimum 0.75")

    # Premarket: monitor only, but still require usable plan for WATCH.
    if phase == "PREMARKET":
        if watch_ok and plan.get("valid") and safe_float(plan.get("reward_risk"), 0) >= MIN_RR_WATCH and conf >= MIN_CONF_WATCH:
            out = signal_base(
                row,
                metrics,
                plan,
                "WATCH",
                "PREMARKET_MONITOR",
                conf,
                live,
                "Premarket monitoring only. No trigger-ready or active signals before regular session.",
                phase,
                regime,
            )
            out["detected_at"] = iso_now_et()
            log_watch_evaluated(symbol, out, metrics, phase)
            return out, None

        log_watch_rejected(symbol, rejected_reasons, conf, metrics, phase)
        return {}, diagnostic_candidate(row, metrics, plan, live, conf, rejected_reasons, phase, regime)

    # Outside regular market open phases: no new signals.
    if not is_market_open_phase(phase):
        final_reasons = rejected_reasons or [f"Market phase {phase}"]
        log_watch_rejected(symbol, final_reasons, conf, metrics, phase)
        return {}, diagnostic_candidate(row, metrics, plan, live, conf, final_reasons, phase, regime)

    # Lunch blackout: allow WATCH -> TRIGGER_READY for valid VWAP reclaim/pullback
    # setups, but never allow automatic ACTIVE_SIGNAL. This surfaces the ticker
    # for manual review without violating the "no lunch auto-entry" rule.
    if is_lunch_blackout_phase(phase):
        if not watch_ok or not plan.get("valid") or safe_float(plan.get("reward_risk"), 0) < MIN_RR_WATCH or conf < MIN_CONF_WATCH:
            log_watch_rejected(symbol, rejected_reasons, conf, metrics, phase)
            return {}, diagnostic_candidate(row, metrics, plan, live, conf, rejected_reasons, phase, regime)

        setup_ready_type, setup_reason, setup_fail_reasons = choose_setup(metrics, plan, conf, phase, row)

        if setup_ready_type and is_lunch_trigger_ready_allowed_setup(setup_ready_type):
            exact_plan = build_trade_plan(row, metrics, setup_ready_type, quote)
            exact_live = live_signal_score(row, metrics, exact_plan, regime, phase)
            exact_conf = final_confidence(safe_float(row.get("score"), 0), exact_live)
            exact_ready_min_conf = setup_ready_conf_required(setup_ready_type, phase)
            event_ok, event_reasons = earnings_reaction_requirements(row, metrics, exact_plan, exact_conf)

            lunch_ready_failures: List[str] = []
            if not exact_plan.get("valid"):
                lunch_ready_failures.append(f"Plan invalid: {safe_str(exact_plan.get('rejection_reason'), 'Invalid plan')}")
            if safe_float(exact_plan.get("reward_risk"), 0) < MIN_RR:
                lunch_ready_failures.append(f"R/R {safe_float(exact_plan.get('reward_risk'), 0):.2f} < minimum {MIN_RR:.1f}")
            if exact_conf < exact_ready_min_conf:
                lunch_ready_failures.append(f"Confidence {exact_conf:.1f} < ready minimum {exact_ready_min_conf:.0f}")
            if not event_ok:
                lunch_ready_failures.extend(event_reasons)

            if not lunch_ready_failures:
                out = signal_base(
                    row,
                    metrics,
                    exact_plan,
                    "TRIGGER_READY",
                    setup_ready_type,
                    exact_conf,
                    exact_live,
                    setup_reason,
                    phase,
                    regime,
                )
                now_text = iso_now_et()
                out["ready_since"] = now_text
                out["detected_at"] = now_text
                out = apply_lunch_caution_fields(out, phase)

                if trigger_was_touched_recently(out, metrics):
                    stale, stale_reason = trigger_pullback_after_touch(out, metrics)
                    if stale:
                        invalid = make_invalidated(out, row, metrics, stale_reason, "NEW_BASE_REQUIRED", phase)
                        return invalid, None
                    touched = make_trigger_touched(
                        out,
                        row,
                        metrics,
                        phase,
                        "Lunch-caution trigger was already touched recently; manual confirmation only.",
                    )
                    touched = apply_lunch_caution_fields(touched, phase)
                    return touched, None

                decision_log(
                    symbol,
                    "BECAME_TRIGGER_READY_LUNCH_CAUTION",
                    setup=setup_ready_type,
                    confidence=exact_conf,
                    trigger=exact_plan.get("entry_trigger"),
                    stop=exact_plan.get("stop_loss"),
                    target_1=exact_plan.get("target_1"),
                    target_2=exact_plan.get("target_2"),
                    rr=exact_plan.get("reward_risk"),
                    price=metrics.price,
                    phase=phase,
                    reason=LUNCH_CAUTION_WARNING,
                )
                return out, None

            setup_fail_reasons.extend(lunch_ready_failures)

        # Valid but not lunch-ready. Keep as WATCH with explicit lunch context.
        out = signal_base(
            row,
            metrics,
            plan,
            "WATCH",
            "LUNCH_BLACKOUT_MONITOR",
            conf,
            live,
            "Lunch blackout monitor only. VWAP reclaim/pullback can become Trigger Ready with caution, but no automatic Active Signal.",
            phase,
            regime,
        )
        out["detected_at"] = iso_now_et()
        out["lunch_caution"] = True
        out["event_risk_warning"] = combine_warning(out.get("event_risk_warning", ""), LUNCH_CAUTION_WARNING)
        out["not_ready_reasons"] = setup_fail_reasons or not_ready_reasons(metrics, plan, conf)
        log_watch_evaluated(symbol, out, metrics, phase)
        return out, None

    # Blackout phases other than lunch: WATCH only if usable; no READY.
    if not is_valid_signal_phase(phase):
        if watch_ok and plan.get("valid") and safe_float(plan.get("reward_risk"), 0) >= MIN_RR_WATCH and conf >= MIN_CONF_WATCH:
            out = signal_base(
                row,
                metrics,
                plan,
                "WATCH",
                "BLACKOUT_MONITOR",
                conf,
                live,
                f"Valid monitor candidate, but {phase.lower().replace('_', ' ')} prevents new signals.",
                phase,
                regime,
            )
            out["detected_at"] = iso_now_et()
            log_watch_evaluated(symbol, out, metrics, phase)
            return out, None

        log_watch_rejected(symbol, rejected_reasons, conf, metrics, phase)
        return {}, diagnostic_candidate(row, metrics, plan, live, conf, rejected_reasons, phase, regime)

    # Valid signal phase:
    # If the plan is invalid, it must NOT show as WATCH.
    if not watch_ok or not plan.get("valid") or safe_float(plan.get("reward_risk"), 0) < MIN_RR_WATCH or conf < MIN_CONF_WATCH:
        debug(f"  {symbol}: rejected: {rejected_reasons}")
        log_watch_rejected(symbol, rejected_reasons, conf, metrics, phase)
        return {}, diagnostic_candidate(row, metrics, plan, live, conf, rejected_reasons, phase, regime)

    # Decide whether it is trigger-ready.
    ready_min_conf = ready_confidence_required(phase)
    early_ready_min_conf = min(ready_min_conf, MIN_CONF_RECLAIM_READY if metrics.vwap_reclaim_recent else ready_min_conf)
    if plan.get("valid") and safe_float(plan.get("reward_risk"), 0) >= MIN_RR and conf >= early_ready_min_conf:
        setup_ready_type, setup_reason, setup_fail_reasons = choose_setup(metrics, plan, conf, phase, row)

        if setup_ready_type:
            # Rebuild plan using the exact ready setup type.
            exact_plan = build_trade_plan(row, metrics, setup_ready_type, quote)
            exact_live = live_signal_score(row, metrics, exact_plan, regime, phase)
            exact_conf = final_confidence(safe_float(row.get("score"), 0), exact_live)

            exact_ready_min_conf = setup_ready_conf_required(setup_ready_type, phase)
            event_ok, event_reasons = earnings_reaction_requirements(row, metrics, exact_plan, exact_conf)
            if exact_plan.get("valid") and safe_float(exact_plan.get("reward_risk"), 0) >= MIN_RR and exact_conf >= exact_ready_min_conf and event_ok:
                out = signal_base(
                    row,
                    metrics,
                    exact_plan,
                    "TRIGGER_READY",
                    setup_ready_type,
                    exact_conf,
                    exact_live,
                    setup_reason,
                    phase,
                    regime,
                )
                now_text = iso_now_et()
                out["ready_since"] = now_text
                out["detected_at"] = now_text

                if trigger_was_touched_recently(out, metrics):
                    stale, stale_reason = trigger_pullback_after_touch(out, metrics)
                    if stale:
                        invalid = make_invalidated(out, row, metrics, stale_reason, "NEW_BASE_REQUIRED", phase)
                        return invalid, None
                    return make_trigger_touched(
                        out,
                        row,
                        metrics,
                        phase,
                        "Entry was already touched recently; waiting for renewed hold/volume confirmation before Active Signal.",
                    ), None

                decision_log(
                    symbol,
                    "BECAME_TRIGGER_READY",
                    setup=setup_ready_type,
                    confidence=exact_conf,
                    trigger=exact_plan.get("entry_trigger"),
                    stop=exact_plan.get("stop_loss"),
                    target_1=exact_plan.get("target_1"),
                    target_2=exact_plan.get("target_2"),
                    rr=exact_plan.get("reward_risk"),
                    price=metrics.price,
                    phase=phase,
                )
                return out, None

            if not event_ok:
                setup_fail_reasons.extend(event_reasons)

        # Good plan, but setup itself is not ready. WATCH.
        out = signal_base(
            row,
            metrics,
            plan,
            "WATCH",
            "MONITORING",
            conf,
            live,
            "Clean candidate with usable 5-min trade plan. Waiting for VWAP/EMA hold, higher-low/reclaim, base squeeze, or HOD breakout trigger confirmation.",
            phase,
            regime,
        )
        out["detected_at"] = iso_now_et()
        out["not_ready_reasons"] = setup_fail_reasons or not_ready_reasons(metrics, plan, conf)
        log_watch_evaluated(symbol, out, metrics, phase)
        return out, None

    # WATCH only if plan is valid and at least minimally usable.
    out = signal_base(
        row,
        metrics,
        plan,
        "WATCH",
        "MONITORING",
        conf,
        live,
        "Relative-strength long candidate with usable 5-min trade plan. Waiting for setup quality to improve.",
        phase,
        regime,
    )
    out["detected_at"] = iso_now_et()
    log_watch_evaluated(symbol, out, metrics, phase)
    return out, None


# ==============================================================
# MAIN ENGINE HELPERS
# ==============================================================

def build_row_lookup(focus: Dict[str, Dict[str, Any]], prior_state: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    rows = dict(focus)

    for sym, signal in prior_state.items():
        status = normalize_status(signal.get("signal_status"))
        if status not in {"TRIGGER_READY", "TRIGGER_TOUCHED", "ACTIVE_SIGNAL"}:
            continue

        if sym in rows:
            continue

        rows[sym] = {
            "symbol": sym,
            "score": safe_float(signal.get("scanner_score"), 0),
            "sector_status": safe_str(signal.get("sector_status"), "UNKNOWN"),
            "signal_source_bucket": "PROTECTED",
            "risk_category": safe_str(signal.get("risk_category"), "NORMAL"),
            "dollar_vol_M": safe_float(signal.get("dollar_vol_M"), MIN_AVG_DOLLAR_VOL_M),
            "atr_pct": safe_float(signal.get("atr_pct"), 3.0),
            "company_name": safe_str(signal.get("company_name"), sym),
        }

    return rows


def prepare_monitor_symbols(focus: Dict[str, Dict[str, Any]], prior_state: Dict[str, Dict[str, Any]]) -> List[str]:
    symbols = set(focus.keys())

    for sym, signal in prior_state.items():
        status = normalize_status(signal.get("signal_status"))
        if status in {"TRIGGER_READY", "TRIGGER_TOUCHED", "ACTIVE_SIGNAL"}:
            symbols.add(sym)

    return sorted(symbols)


def build_signal_outputs(new_state: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rank = {
        "ACTIVE_SIGNAL": 1,
        "ACTIVE": 1,
        "TRIGGER_READY": 2,
        "TRIGGER_TOUCHED": 2,
        "READY": 2,
        "WATCH": 3,
        "INVALIDATED": 4,
    }

    signals = [s for s in new_state.values() if s and normalize_status(s.get("signal_status")) != "WAIT"]

    signals.sort(
        key=lambda x: (
            rank.get(normalize_status(x.get("signal_status")), 9),
            -safe_float(x.get("confidence"), 0),
            -safe_float(x.get("reward_risk"), 0),
            safe_str(x.get("symbol"), ""),
        )
    )

    return signals


def summarize_signals(signals: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {
        "active": 0,
        "trigger_ready": 0,
        "watch": 0,
        "invalidated": 0,
        "suppressed": 0,
    }

    for s in signals:
        status = normalize_status(s.get("signal_status"))
        actionability = safe_str(s.get("actionability"), "").upper()

        if status == "ACTIVE_SIGNAL":
            counts["active"] += 1
        elif status in {"TRIGGER_READY", "TRIGGER_TOUCHED"}:
            counts["trigger_ready"] += 1
        elif status == "WATCH":
            counts["watch"] += 1
        elif status == "INVALIDATED":
            counts["invalidated"] += 1

        if actionability == "SUPPRESSED" or s.get("suppression_reason"):
            counts["suppressed"] += 1

    return counts


# ==============================================================
# MAIN ENGINE
# ==============================================================

def run_signal_engine() -> None:
    now = ny_now()
    phase = get_market_phase(now)

    log("============================================================")
    log("Signal Desk v1 Engine")
    log("============================================================")
    log(f"Time: {now.isoformat(timespec='seconds')}")
    log(f"Market phase: {phase}")
    log(f"Alpaca feed: {DATA_FEED}")
    log(f"Execution timeframe: {EXECUTION_TIMEFRAME}")
    log(f"Structure timeframe: {STRUCTURE_TIMEFRAME}")

    focus = load_focus_candidates()
    prior_state = load_signal_state()
    regime = load_json(MARKET_REGIME_FILE, {})

    monitor_symbols = prepare_monitor_symbols(focus, prior_state)
    row_lookup = build_row_lookup(focus, prior_state)

    log(f"Focus universe: {len(focus)} current scanner names")
    log(f"Monitor universe: {len(monitor_symbols)} including protected signals")
    decision_log(
        "-",
        "RUN_START",
        phase=phase,
        focus_count=len(focus),
        monitor_count=len(monitor_symbols),
        feed=DATA_FEED,
    )

    market_data = AlpacaMarketData(DATA_FEED)

    bars_1m_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    bars_5m_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    quotes_by_symbol: Dict[str, Dict[str, Any]] = {}
    trades_by_symbol: Dict[str, Dict[str, Any]] = {}

    if market_data.available and monitor_symbols:
        bars_1m_by_symbol = market_data.fetch_bars(monitor_symbols, EXECUTION_TIMEFRAME)
        bars_5m_by_symbol = market_data.fetch_bars(monitor_symbols, STRUCTURE_TIMEFRAME)
        quotes_by_symbol = market_data.fetch_latest_quotes(monitor_symbols)
        trades_by_symbol = market_data.fetch_latest_trades(monitor_symbols)
    elif not market_data.available:
        log("  ⚠ No Alpaca credentials. signal_desk.json will be empty or historical only.")

    new_state: Dict[str, Dict[str, Any]] = {}
    rejected_candidates: List[Dict[str, Any]] = []

    for sym in monitor_symbols:
        row = row_lookup.get(sym, {"symbol": sym})
        metrics = analyze_execution_bars(sym, bars_1m_by_symbol.get(sym, []))
        metrics = enrich_structure_from_5min(metrics, bars_5m_by_symbol.get(sym, []))

        quote = quotes_by_symbol.get(sym, {})
        trade = trades_by_symbol.get(sym, {})
        metrics = apply_live_price_overlay(metrics, quote, trade)

        existing = prior_state.get(sym, {})
        existing_status = normalize_status(existing.get("signal_status"))

        # Market closed: no new signals; expire protected signals.
        if phase in {"CLOSED", "AFTERHOURS"}:
            if existing_status in {"TRIGGER_READY", "TRIGGER_TOUCHED", "ACTIVE_SIGNAL"}:
                invalid = make_invalidated(
                    existing,
                    row,
                    metrics,
                    f"Market phase {phase}; signal expired.",
                    "MISSED_WINDOW",
                    phase,
                )
                new_state[sym] = invalid
            continue

        if existing_status in {"TRIGGER_READY", "TRIGGER_TOUCHED", "ACTIVE_SIGNAL", "INVALIDATED"}:
            processed = process_existing_signal(existing, row, metrics, quote, regime, phase)
            if processed:
                processed = apply_state_transition_guard(
                    sym,
                    existing,
                    processed,
                    metrics,
                    phase,
                    reason="existing_signal_processing",
                )
                new_state[sym] = processed
                continue

            # Defensive guard: protected states must never fall through into
            # new/watch processing. If no processed result was produced, keep
            # the locked state visible and log the blocked implicit demotion.
            if existing_status in {"TRIGGER_READY", "TRIGGER_TOUCHED", "ACTIVE_SIGNAL"}:
                retained = retain_locked_signal_after_block(
                    existing,
                    metrics,
                    phase,
                    f"STATE_LOCK: Blocked {existing_status} -> WATCH | reason: process_existing_signal returned empty",
                )
                decision_log(
                    sym,
                    "STATE_TRANSITION_BLOCKED",
                    old_status=existing_status,
                    attempted_status="WATCH",
                    kept_status=existing_status,
                    reason="process_existing_signal returned empty",
                )
                new_state[sym] = retained
                continue

        # WATCH is not protected. If it fell out of current focus, remove it.
        in_current_focus = sym in focus
        if not in_current_focus:
            if existing_status == "WATCH":
                decision_log(sym, "WATCH_REMOVED", reason="Ticker fell out of current scanner focus", phase=phase)
            continue

        signal, diagnostic = process_new_or_watch(row, metrics, quote, regime, phase)

        if signal:
            signal = apply_state_transition_guard(
                sym,
                existing,
                signal,
                metrics,
                phase,
                reason="new_or_watch_processing",
            )
            new_state[sym] = signal
        elif diagnostic:
            if existing_status == "WATCH":
                decision_log(
                    sym,
                    "WATCH_REMOVED",
                    reason="No longer passes WATCH criteria",
                    rejected_reasons="; ".join(diagnostic.get("rejected_reasons", [])[:5]) if isinstance(diagnostic.get("rejected_reasons"), list) else "",
                    phase=phase,
                )
            rejected_candidates.append(diagnostic)

    outcome_summary = update_signal_outcomes(new_state, prior_state)
    signals = build_signal_outputs(new_state)
    counts = summarize_signals(signals)

    rejected_candidates.sort(
        key=lambda x: (
            -safe_float(x.get("confidence"), 0),
            -safe_float(x.get("reward_risk"), 0),
            safe_str(x.get("symbol"), ""),
        )
    )

    signal_desk_payload = {
        "generated_at_et": iso_now_et(),
        "market_phase": phase,
        "alpaca_feed": DATA_FEED,
        "execution_timeframe": EXECUTION_TIMEFRAME,
        "structure_timeframe": STRUCTURE_TIMEFRAME,
        "universe": {
            "potential_limit": POTENTIAL_LIMIT,
            "active_limit": ACTIVE_LIMIT,
            "current_focus_count": len(focus),
            "monitor_count": len(monitor_symbols),
        },
        "counts": counts,
        "outcomes": outcome_summary,
        "signals": signals,
        "rejected_candidates": rejected_candidates,
    }

    write_json(SIGNAL_DESK_FILE, signal_desk_payload)

    write_signal_state(
        new_state,
        {
            "market_phase": phase,
            "alpaca_feed": DATA_FEED,
            "execution_timeframe": EXECUTION_TIMEFRAME,
            "structure_timeframe": STRUCTURE_TIMEFRAME,
            "current_focus_count": len(focus),
            "monitor_count": len(monitor_symbols),
            "counts": counts,
            "rejected_count": len(rejected_candidates),
        },
    )

    decision_log(
        "-",
        "RUN_END",
        phase=phase,
        active=counts["active"],
        trigger_ready=counts["trigger_ready"],
        watch=counts["watch"],
        invalidated=counts["invalidated"],
        suppressed=counts["suppressed"],
        rejected=len(rejected_candidates),
    )
    prune_decision_log()

    log("Signal Desk output written:")
    log(f"  {SIGNAL_DESK_FILE}")
    log(f"  {SIGNAL_STATE_FILE}")
    log(f"  {SIGNAL_DECISION_LOG_FILE}")
    log(f"  {SIGNAL_OUTCOMES_FILE}")
    log(f"Counts: {counts}")
    log(f"Rejected diagnostics: {len(rejected_candidates)}")

    if not signals:
        log("No signal at this moment. Scanner is monitoring top candidates.")


if __name__ == "__main__":
    run_signal_engine()
