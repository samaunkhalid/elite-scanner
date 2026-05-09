"""
macro_calendar.py
-----------------
Standalone macro-event risk file builder for Elite Scanner.

Creates:
  macro_calendar.json

Design:
- Does NOT modify elite_scanner.py or scanner scoring.
- Uses New York Fed Economic Indicators Calendar as primary free source.
- Uses only Python standard library.
- If source fetch fails, dashboard still works and shows UNKNOWN.

Source:
  https://www.newyorkfed.org/research/calendars/nationalecon_cal
"""

import json
import re
import html
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


NYFED_URL = "https://www.newyorkfed.org/research/calendars/nationalecon_cal"

LOOKAHEAD_DAYS = 7

HIGH_IMPACT_KEYWORDS = [
    "consumer price index",
    "cpi",
    "producer price index",
    "ppi",
    "personal income and the pce deflator",
    "pce deflator",
    "employment situation",
    "nonfarm",
    "payroll",
    "gross domestic product",
    "gdp",
    "advance retail sales",
    "retail sales",
    "ism manufacturing",
    "ism non-manufacturing",
    "fomc",
    "fed funds",
    "interest rate",
    "powell",
]

MEDIUM_IMPACT_KEYWORDS = [
    "initial claims",
    "jolts",
    "adp national employment",
    "consumer confidence",
    "michigan consumer survey",
    "durable goods",
    "industrial production",
    "capacity utilization",
    "philadelphia fed",
    "empire state",
    "new residential construction",
    "new residential sales",
    "pending home sales",
    "existing home sales",
    "trade balance",
    "imports and exports",
    "productivity",
    "business inventories",
    "manufacturing, shipments, and orders",
]


def ny_now():
    now_utc = datetime.now(timezone.utc)
    if ZoneInfo:
        return now_utc.astimezone(ZoneInfo("America/New_York"))
    return datetime.now()


def fetch_url(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; EliteScannerMacroCalendar/1.0; "
                "+https://github.com/samaunkhalid/elite-scanner)"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="replace")


def html_to_lines(raw_html):
    text = re.sub(r"(?is)<script.*?</script>", " ", raw_html)
    text = re.sub(r"(?is)<style.*?</style>", " ", text)

    # Force common block tags onto separate lines.
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</?(div|p|tr|td|th|li|h1|h2|h3|h4|section|article)[^>]*>", "\n", text)

    # Remove remaining tags and normalize.
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")

    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return lines


def clean_event_name(name):
    name = re.sub(r"\bImage\s*:\s*PDF\b", "", name, flags=re.I)
    name = re.sub(r"\bImage\b", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" -–—")
    return name


def classify_impact(name):
    n = name.lower()

    for kw in HIGH_IMPACT_KEYWORDS:
        if kw in n:
            return "HIGH"

    for kw in MEDIUM_IMPACT_KEYWORDS:
        if kw in n:
            return "MEDIUM"

    return "LOW"


def parse_time_to_hour_min(time_text):
    """
    NY Fed prints times like (08:30), (10:00), (12:45), occasionally (02:00).
    Economic releases shown as 02:00/03:30 are usually afternoon ET.
    """
    m = re.search(r"(\d{1,2}):(\d{2})", time_text)
    if not m:
        return 9, 30

    hour = int(m.group(1))
    minute = int(m.group(2))

    # Interpret very small hours as PM for market calendar context.
    # Example: 02:00 = 2:00 PM ET, not 2:00 AM.
    if 1 <= hour <= 4:
        hour += 12

    return hour, minute


def parse_nyfed_events(raw_html, source_url=NYFED_URL):
    lines = html_to_lines(raw_html)

    # Find month/year label like "May 2026".
    month_year = None
    month_idx = None

    for i, line in enumerate(lines):
        if re.fullmatch(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}",
            line,
        ):
            month_year = line
            month_idx = i
            break

    if not month_year:
        return []

    month_dt = datetime.strptime(month_year, "%B %Y")
    year = month_dt.year
    month = month_dt.month

    events = []
    current_day = None
    last_event_name = None

    useful_lines = lines[month_idx:]

    for line in useful_lines:
        if line.lower().startswith("key:"):
            break

        # Calendar day number.
        if re.fullmatch(r"\d{1,2}", line):
            day = int(line)
            if 1 <= day <= 31:
                current_day = day
                last_event_name = None
            continue

        # Time line like "(08:30)".
        if re.fullmatch(r"\(?\d{1,2}:\d{2}\)?", line):
            if current_day and last_event_name:
                hour, minute = parse_time_to_hour_min(line)
                event_dt = datetime(year, month, current_day, hour, minute)

                if ZoneInfo:
                    event_dt = event_dt.replace(tzinfo=ZoneInfo("America/New_York"))

                name = clean_event_name(last_event_name)
                if name:
                    impact = classify_impact(name)
                    events.append({
                        "name": name,
                        "date": event_dt.strftime("%Y-%m-%d"),
                        "time_et": event_dt.strftime("%H:%M"),
                        "datetime_et": event_dt.isoformat(),
                        "impact": impact,
                        "source": "New York Fed",
                        "source_url": source_url,
                    })
            continue

        # Ignore page chrome.
        ignore = {
            "economic indicators calendar",
            "printer version",
            "previous month",
            "next month",
            "monday tuesday wednesday thursday friday",
            "top of page",
        }
        if line.lower() in ignore:
            continue

        if current_day:
            candidate = clean_event_name(line)
            if candidate and not re.fullmatch(r"\d{1,2}", candidate):
                last_event_name = candidate

    return events


def fetch_nyfed_events():
    raw = fetch_url(NYFED_URL)
    return parse_nyfed_events(raw, NYFED_URL)


def event_sort_key(event):
    return event.get("datetime_et", "")


def build_macro_risk(events, fetch_error=""):
    now = ny_now()
    lookahead_end = now + timedelta(days=LOOKAHEAD_DAYS)

    upcoming = []

    for event in events:
        try:
            dt = datetime.fromisoformat(event["datetime_et"])
            if dt.tzinfo is None and ZoneInfo:
                dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
        except Exception:
            continue

        if now <= dt <= lookahead_end and event.get("impact") in ["HIGH", "MEDIUM"]:
            minutes_until = int((dt - now).total_seconds() // 60)
            event["minutes_until"] = minutes_until
            upcoming.append(event)

    upcoming.sort(key=event_sort_key)

    if fetch_error and not events:
        return {
            "risk_level": "UNKNOWN",
            "headline": "Macro Risk: UNKNOWN — calendar unavailable",
            "action": "Manually check CPI/FOMC/jobs/PCE/PPI calendar before trading.",
            "events": [],
        }

    high_events = [e for e in upcoming if e.get("impact") == "HIGH"]
    medium_events = [e for e in upcoming if e.get("impact") == "MEDIUM"]

    if high_events:
        first = high_events[0]
        minutes = first.get("minutes_until", 999999)

        if minutes <= 36 * 60:
            risk = "HIGH"
            action = "Reduce size or wait until after the release/initial volatility clears."
        else:
            risk = "MEDIUM"
            action = "Major event upcoming this week. Keep position size conservative."

        headline = f"Macro Risk: {risk} — {first['name']} {first['date']} {first['time_et']} ET"

    elif medium_events:
        first = medium_events[0]
        risk = "MEDIUM"
        headline = f"Macro Risk: MEDIUM — {first['name']} {first['date']} {first['time_et']} ET"
        action = "Use caution around release time; confirm market reaction before entry."

    else:
        risk = "LOW"
        headline = "Macro Risk: LOW — no high/medium events flagged in lookahead window"
        action = "Normal macro backdrop. Still monitor headlines and Fed speakers."

    return {
        "risk_level": risk,
        "headline": headline,
        "action": action,
        "events": upcoming[:12],
    }


def main():
    print("\n" + "=" * 70)
    print("BUILDING MACRO CALENDAR RISK FILE")
    print("=" * 70)

    fetch_error = ""
    events = []
    source = "New York Fed Economic Indicators Calendar"

    try:
        events = fetch_nyfed_events()
    except Exception as e:
        fetch_error = f"New York Fed fetch failed: {e}"
        source = "Unavailable"

    risk = build_macro_risk(events, fetch_error=fetch_error)

    now = ny_now()

    output = {
        "risk_level": risk["risk_level"],
        "headline": risk["headline"],
        "action": risk["action"],
        "events": risk["events"],
        "source": source,
        "source_url": NYFED_URL if source != "Unavailable" else "",
        "generated_at_et": now.strftime("%Y-%m-%d %H:%M ET"),
        "generated_at": now.strftime("%Y-%m-%d %H:%M ET"),
        "lookahead_days": LOOKAHEAD_DAYS,
        "fetch_error": fetch_error,
    }

    with open("macro_calendar.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"  Macro risk: {output['risk_level']}")
    print(f"  Headline:   {output['headline']}")
    print(f"  Events:     {len(output['events'])}")
    print(f"  Source:     {output['source']}")
    if fetch_error:
        print(f"  ⚠ Fetch error: {fetch_error}")
    print("  ✓ Saved: macro_calendar.json")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
