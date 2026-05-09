"""
macro_calendar.py
-----------------
Standalone macro-event risk detector for Elite Scanner.

Creates:
  macro_calendar.json

No scanner logic is changed.

Primary source:
  Trading Economics Calendar API
  Optional GitHub secret:
    TRADING_ECONOMICS_KEY

If no key is provided, the script tries the public guest credential:
    guest:guest

Dashboard use:
  elite_dashboard.py can read macro_calendar.json and show a macro risk banner.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


# ==============================================================
# CONFIG
# ==============================================================

OUTPUT_FILE = "macro_calendar.json"

COUNTRY = "united states"
LOOKAHEAD_DAYS = 3

HIGH_IMPACT_KEYWORDS = [
    "fomc",
    "fed interest rate decision",
    "federal funds rate",
    "interest rate decision",
    "fed press conference",
    "fomc minutes",
    "powell",
    "cpi",
    "consumer price index",
    "inflation rate",
    "core inflation",
    "ppi",
    "producer price index",
    "pce",
    "core pce",
    "personal consumption expenditures",
    "non farm payrolls",
    "nonfarm payrolls",
    "nfp",
    "unemployment rate",
    "average hourly earnings",
    "initial jobless claims",
    "jobless claims",
    "gdp",
    "gross domestic product",
    "retail sales",
    "ism manufacturing",
    "ism services",
    "jolts",
    "treasury refunding",
    "10-year note auction",
    "30-year bond auction",
]

MEDIUM_IMPACT_KEYWORDS = [
    "durable goods",
    "consumer confidence",
    "michigan consumer sentiment",
    "housing starts",
    "building permits",
    "existing home sales",
    "new home sales",
    "industrial production",
    "philadelphia fed",
    "empire state",
    "beige book",
    "fed speech",
    "fed ",
]


# ==============================================================
# TIME HELPERS
# ==============================================================

def now_utc():
    return datetime.now(timezone.utc)


def get_ny_tz():
    if ZoneInfo:
        return ZoneInfo("America/New_York")
    return timezone.utc


def to_ny(dt):
    return dt.astimezone(get_ny_tz())


def parse_event_datetime(value):
    """
    Trading Economics usually returns ISO-like strings in UTC.
    This parser is defensive.
    """
    if not value:
        return None

    raw = str(value).strip()

    # Normalize common ISO variants.
    raw = raw.replace("Z", "+00:00")

    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass

    # Fallback formats.
    for fmt in [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
    ]:
        try:
            dt = datetime.strptime(raw[:19], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue

    return None


def et_time_str(dt_utc):
    if not dt_utc:
        return "TBD"
    return to_ny(dt_utc).strftime("%Y-%m-%d %H:%M ET")


# ==============================================================
# DATA FETCH
# ==============================================================

def http_get_json(url, timeout=25):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "EliteScannerMacroCalendar/1.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", errors="replace")
        return json.loads(body)


def fetch_trading_economics_calendar():
    """
    Fetch US economic calendar from Trading Economics.

    Optional secret:
      TRADING_ECONOMICS_KEY

    If missing:
      guest:guest
    """
    api_key = os.getenv("TRADING_ECONOMICS_KEY", "guest:guest").strip() or "guest:guest"

    start = now_utc().date()
    end = start + timedelta(days=LOOKAHEAD_DAYS)

    country = urllib.parse.quote(COUNTRY)

    # Date-range endpoint. If this fails, fallback to country snapshot.
    urls = [
        (
            "https://api.tradingeconomics.com/calendar/country/"
            f"{country}/from/{start.isoformat()}/to/{end.isoformat()}"
            f"?c={urllib.parse.quote(api_key)}&format=json"
        ),
        (
            "https://api.tradingeconomics.com/calendar/country/"
            f"{country}?c={urllib.parse.quote(api_key)}&format=json"
        ),
    ]

    last_error = None

    for url in urls:
        try:
            data = http_get_json(url)
            if isinstance(data, list):
                return data, "Trading Economics Calendar API"
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                return data["data"], "Trading Economics Calendar API"
        except Exception as e:
            last_error = str(e)

    raise RuntimeError(f"Trading Economics fetch failed: {last_error}")


# ==============================================================
# EVENT SCORING
# ==============================================================

def normalize_text(value):
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9\s\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def event_name(event):
    for key in ["Event", "event", "Name", "name", "Category", "category"]:
        val = event.get(key)
        if val:
            return str(val)
    return "Unknown event"


def event_importance(event):
    """
    Trading Economics may return importance as int, string, or missing.
    """
    for key in ["Importance", "importance", "ImportanceLevel"]:
        val = event.get(key)
        if val is None or val == "":
            continue

        try:
            return int(float(val))
        except Exception:
            txt = str(val).lower()
            if "high" in txt:
                return 3
            if "medium" in txt:
                return 2
            if "low" in txt:
                return 1

    return 0


def classify_event(event):
    name = event_name(event)
    category = str(event.get("Category", "") or event.get("category", ""))
    reference = str(event.get("Reference", "") or event.get("reference", ""))
    full_text = normalize_text(f"{name} {category} {reference}")

    if any(k in full_text for k in HIGH_IMPACT_KEYWORDS):
        return "HIGH", "High-impact macro event"

    if event_importance(event) >= 3:
        return "HIGH", "High-importance calendar event"

    if any(k in full_text for k in MEDIUM_IMPACT_KEYWORDS):
        return "MEDIUM", "Medium-impact macro event"

    if event_importance(event) == 2:
        return "MEDIUM", "Medium-importance calendar event"

    return "LOW", "Low-impact event"


def market_status_for_time(dt_ny):
    minutes = dt_ny.hour * 60 + dt_ny.minute
    weekday = dt_ny.weekday()

    if weekday >= 5:
        return "WEEKEND"
    if minutes < 4 * 60:
        return "CLOSED"
    if minutes < 9 * 60 + 30:
        return "PRE-MARKET"
    if minutes < 16 * 60:
        return "MARKET HOURS"
    if minutes < 20 * 60:
        return "AFTER-HOURS"
    return "CLOSED"


def risk_action(risk_level, top_event):
    if risk_level == "HIGH":
        name = top_event.get("event", "macro event") if top_event else "macro event"
        return (
            f"High macro risk around {name}. Reduce size or wait 15–30 minutes "
            "after the release/announcement before taking new trades."
        )

    if risk_level == "MEDIUM":
        return "Macro caution. Confirm market reaction, VWAP, spread, and sector direction before entry."

    if risk_level == "EVENT_SOON":
        return "Major event is approaching. Avoid initiating marginal setups before the event."

    return "No major macro event flagged in the lookahead window."


def build_macro_payload():
    generated_utc = now_utc()
    generated_ny = to_ny(generated_utc)

    try:
        raw_events, source = fetch_trading_economics_calendar()
        fetch_error = ""
    except Exception as e:
        raw_events = []
        source = "Unavailable"
        fetch_error = str(e)

    start_utc = generated_utc - timedelta(hours=2)
    end_utc = generated_utc + timedelta(days=LOOKAHEAD_DAYS)

    parsed_events = []

    for e in raw_events:
        if not isinstance(e, dict):
            continue

        dt_raw = (
            e.get("Date")
            or e.get("date")
            or e.get("Datetime")
            or e.get("datetime")
            or e.get("LastUpdate")
        )

        dt_utc = parse_event_datetime(dt_raw)

        # Keep undated major events but mark TBD.
        if dt_utc and not (start_utc <= dt_utc <= end_utc):
            continue

        impact, reason = classify_event(e)
        name = event_name(e)

        if impact == "LOW":
            continue

        dt_ny = to_ny(dt_utc) if dt_utc else None

        parsed_events.append({
            "event": name,
            "impact": impact,
            "reason": reason,
            "time_et": et_time_str(dt_utc),
            "date_utc": dt_utc.isoformat() if dt_utc else "",
            "category": str(e.get("Category", "") or e.get("category", "")),
            "country": str(e.get("Country", "") or e.get("country", "")),
            "importance": event_importance(e),
            "actual": str(e.get("Actual", "") or ""),
            "forecast": str(e.get("Forecast", "") or ""),
            "previous": str(e.get("Previous", "") or ""),
            "market_session": market_status_for_time(dt_ny) if dt_ny else "TBD",
        })

    # Sort by time, then impact.
    parsed_events.sort(
        key=lambda x: (
            x["date_utc"] or "9999",
            0 if x["impact"] == "HIGH" else 1,
        )
    )

    # Determine dashboard risk level.
    risk_level = "LOW"
    top_event = None

    for event in parsed_events:
        dt_utc = parse_event_datetime(event.get("date_utc"))
        hours_to = None
        if dt_utc:
            hours_to = (dt_utc - generated_utc).total_seconds() / 3600.0

        if event["impact"] == "HIGH":
            top_event = event
            # Already happened in last 2 hours or upcoming within 24 hours.
            if hours_to is None or -2 <= hours_to <= 24:
                risk_level = "HIGH"
                break
            risk_level = "EVENT_SOON"
            break

    if risk_level == "LOW":
        for event in parsed_events:
            if event["impact"] == "MEDIUM":
                top_event = event
                risk_level = "MEDIUM"
                break

    if fetch_error and not parsed_events:
        risk_level = "UNKNOWN"
        headline = "Macro Risk: UNKNOWN — calendar unavailable"
        action = "Manually check CPI/FOMC/jobs/PCE/PPI calendar before trading."
        risk_class = "macro-unknown"
    elif risk_level == "HIGH":
        headline = f"Macro Risk: HIGH — {top_event['event']} at {top_event['time_et']}"
        action = risk_action("HIGH", top_event)
        risk_class = "macro-high"
    elif risk_level == "EVENT_SOON":
        headline = f"Macro Risk: EVENT SOON — {top_event['event']} at {top_event['time_et']}"
        action = risk_action("EVENT_SOON", top_event)
        risk_class = "macro-event-soon"
    elif risk_level == "MEDIUM":
        headline = f"Macro Risk: MEDIUM — {top_event['event']} at {top_event['time_et']}"
        action = risk_action("MEDIUM", top_event)
        risk_class = "macro-medium"
    else:
        headline = "Macro Risk: LOW — no major event flagged"
        action = risk_action("LOW", None)
        risk_class = "macro-low"

    return {
        "generated_at_utc": generated_utc.isoformat(),
        "generated_at_et": generated_ny.strftime("%Y-%m-%d %H:%M ET"),
        "source": source,
        "fetch_error": fetch_error,
        "risk_level": risk_level,
        "risk_class": risk_class,
        "headline": headline,
        "action": action,
        "lookahead_days": LOOKAHEAD_DAYS,
        "events": parsed_events[:12],
    }


def main():
    print("\n" + "=" * 70)
    print("BUILDING MACRO CALENDAR RISK FILE")
    print("=" * 70)

    payload = build_macro_payload()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"  Macro risk: {payload['risk_level']}")
    print(f"  Headline:   {payload['headline']}")
    print(f"  Events:     {len(payload['events'])}")
    print(f"  Source:     {payload['source']}")

    if payload.get("fetch_error"):
        print(f"  ⚠ Fetch error: {payload['fetch_error'][:250]}")

    print(f"  ✓ Saved: {OUTPUT_FILE}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Never break the whole workflow because of macro calendar.
        fallback = {
            "generated_at_utc": now_utc().isoformat(),
            "generated_at_et": to_ny(now_utc()).strftime("%Y-%m-%d %H:%M ET"),
            "source": "Fallback",
            "fetch_error": str(e),
            "risk_level": "UNKNOWN",
            "risk_class": "macro-unknown",
            "headline": "Macro Risk: UNKNOWN — script failed",
            "action": "Manually check the economic calendar before trading.",
            "lookahead_days": LOOKAHEAD_DAYS,
            "events": [],
        }

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(fallback, f, indent=2)

        print(f"  ⚠ Macro calendar failed but fallback was saved: {e}")
        sys.exit(0)
