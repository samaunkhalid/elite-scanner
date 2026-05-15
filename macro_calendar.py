"""
macro_calendar.py
-----------------
Standalone macro-event risk file builder for Elite Scanner.

Creates:
  macro_calendar.json

Design:
- Does NOT modify elite_scanner.py or scanner scoring.
- Uses New York Fed Economic Indicators Calendar as primary free calendar source.
- Keeps today's already-released high/medium macro events visible until market close.
- Marks released events as RELEASED with Bullish/Bearish/Neutral/Unknown market reaction.
- Reaction sentiment is based on SPY / QQQ / IWM / VIX price reaction after release.
- Uses only Python standard library.
- If source fetch fails, dashboard still works and shows UNKNOWN.

Important:
- Released sentiment is MARKET REACTION sentiment, not actual/forecast economic surprise.
- Actual vs forecast requires a separate economic-data API and is intentionally not guessed here.

Source:
  https://www.newyorkfed.org/research/calendars/nationalecon_cal
"""

import json
import re
import html
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


NYFED_URL = "https://www.newyorkfed.org/research/calendars/nationalecon_cal"

LOOKAHEAD_DAYS = 7

# Released events remain visible in macro_calendar.json until this ET time.
RELEASED_VISIBLE_UNTIL_HOUR_ET = 20
RELEASED_VISIBLE_UNTIL_MINUTE_ET = 1

# Reaction window after a release. If the event just came out, use latest
# available data and mark reaction_status as DEVELOPING.
REACTION_WINDOW_MINUTES = 15
REACTION_MINIMUM_MINUTES = 3

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1m&includePrePost=true"

REACTION_SYMBOLS = {
    "SPY": "SPY",
    "QQQ": "QQQ",
    "IWM": "IWM",
    "VIX": "^VIX",
}

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


def ny_tz():
    if ZoneInfo:
        return ZoneInfo("America/New_York")
    return None


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


def parse_event_dt(event):
    try:
        dt = datetime.fromisoformat(event["datetime_et"])
        if dt.tzinfo is None and ZoneInfo:
            dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
        return dt
    except Exception:
        return None


def market_close_visibility_time(now):
    tz = ny_tz()
    if tz:
        return datetime(
            now.year,
            now.month,
            now.day,
            RELEASED_VISIBLE_UNTIL_HOUR_ET,
            RELEASED_VISIBLE_UNTIL_MINUTE_ET,
            tzinfo=tz,
        )
    return datetime(now.year, now.month, now.day, RELEASED_VISIBLE_UNTIL_HOUR_ET, RELEASED_VISIBLE_UNTIL_MINUTE_ET)


def fetch_yahoo_intraday(symbol):
    """
    Fetch 1-minute Yahoo chart data for today.
    Returns rows:
      [{"ts": aware datetime UTC, "close": float}, ...]
    """
    encoded = urllib.parse.quote(symbol, safe="")
    url = YAHOO_CHART_URL.format(symbol=encoded)

    try:
        raw = fetch_url(url)
        data = json.loads(raw)
        result = (data.get("chart", {}).get("result") or [None])[0]
        if not result:
            return []

        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []

        rows = []
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            try:
                rows.append({
                    "ts": datetime.fromtimestamp(int(ts), timezone.utc),
                    "close": float(close),
                })
            except Exception:
                continue

        return rows
    except Exception:
        return []


def price_at_or_before(rows, target_dt):
    if not rows or not target_dt:
        return None

    target_utc = target_dt.astimezone(timezone.utc) if target_dt.tzinfo else target_dt.replace(tzinfo=timezone.utc)
    best = None

    for row in rows:
        if row["ts"] <= target_utc:
            best = row

    return best


def price_at_or_after(rows, target_dt):
    if not rows or not target_dt:
        return None

    target_utc = target_dt.astimezone(timezone.utc) if target_dt.tzinfo else target_dt.replace(tzinfo=timezone.utc)

    for row in rows:
        if row["ts"] >= target_utc:
            return row

    return rows[-1]


def pct_change(start, end):
    try:
        if start is None or end is None or start == 0:
            return None
        return (float(end) - float(start)) / float(start) * 100.0
    except Exception:
        return None


def classify_reaction(reaction):
    """
    Market reaction sentiment. This is not actual/forecast economic surprise.

    Bullish = broad equity reaction positive and VIX not rising sharply.
    Bearish = broad equity reaction negative and/or VIX rising.
    Neutral = mixed/small reaction.
    Unknown = insufficient market data.
    """
    spy = reaction.get("SPY_pct")
    qqq = reaction.get("QQQ_pct")
    iwm = reaction.get("IWM_pct")
    vix = reaction.get("VIX_pct")

    equity_moves = [x for x in [spy, qqq, iwm] if isinstance(x, (int, float))]
    if len(equity_moves) < 2:
        return "UNKNOWN", "Insufficient market reaction data"

    equity_avg = sum(equity_moves) / len(equity_moves)
    spy_val = spy if isinstance(spy, (int, float)) else equity_avg
    qqq_val = qqq if isinstance(qqq, (int, float)) else equity_avg
    vix_val = vix if isinstance(vix, (int, float)) else 0.0

    # Strong directional reaction.
    if equity_avg >= 0.25 and spy_val > 0 and qqq_val > 0 and vix_val <= 2.0:
        return "BULLISH", "Equity indexes rose after release and VIX did not spike"

    if equity_avg <= -0.25 and spy_val < 0 and qqq_val < 0:
        return "BEARISH", "Equity indexes sold off after release"

    # Moderate but coordinated move.
    if spy_val >= 0.15 and qqq_val >= 0.15 and vix_val <= 1.0:
        return "BULLISH", "SPY/QQQ reaction positive after release"

    if spy_val <= -0.15 and qqq_val <= -0.15:
        return "BEARISH", "SPY/QQQ reaction negative after release"

    # VIX spike can make mixed equity reaction bearish/caution.
    if vix_val >= 3.0 and equity_avg <= 0.10:
        return "BEARISH", "VIX rose after release and equity reaction was weak/mixed"

    return "NEUTRAL", "Reaction mixed or too small to classify"


def build_reaction_snapshot(event_dt, now, market_data):
    """
    Build SPY/QQQ/IWM/VIX reaction from 1 minute before release to 15 minutes
    after release, or latest available if the release is still developing.
    """
    elapsed_minutes = int((now - event_dt).total_seconds() // 60)
    status = "COMPLETE" if elapsed_minutes >= REACTION_WINDOW_MINUTES else "DEVELOPING"

    if elapsed_minutes < REACTION_MINIMUM_MINUTES:
        return {
            "sentiment": "UNKNOWN",
            "reaction_status": "DEVELOPING",
            "reaction_reason": "Release just occurred; waiting for enough market reaction data",
            "reaction_window_minutes": max(0, elapsed_minutes),
        }

    baseline_dt = event_dt - timedelta(minutes=1)
    reaction_dt = event_dt + timedelta(minutes=min(REACTION_WINDOW_MINUTES, max(REACTION_MINIMUM_MINUTES, elapsed_minutes)))

    reaction = {
        "reaction_status": status,
        "reaction_window_minutes": min(REACTION_WINDOW_MINUTES, max(REACTION_MINIMUM_MINUTES, elapsed_minutes)),
        "baseline_time_et": baseline_dt.astimezone(ny_tz()).strftime("%Y-%m-%d %H:%M ET") if baseline_dt.tzinfo and ny_tz() else baseline_dt.strftime("%Y-%m-%d %H:%M ET"),
        "reaction_time_et": reaction_dt.astimezone(ny_tz()).strftime("%Y-%m-%d %H:%M ET") if reaction_dt.tzinfo and ny_tz() else reaction_dt.strftime("%Y-%m-%d %H:%M ET"),
    }

    for label in ["SPY", "QQQ", "IWM", "VIX"]:
        rows = market_data.get(label, [])
        before = price_at_or_before(rows, baseline_dt)
        after = price_at_or_after(rows, reaction_dt)

        before_close = before["close"] if before else None
        after_close = after["close"] if after else None
        move = pct_change(before_close, after_close)

        if before_close is not None:
            reaction[f"{label}_before"] = round(before_close, 4)
        if after_close is not None:
            reaction[f"{label}_after"] = round(after_close, 4)
        if move is not None:
            reaction[f"{label}_pct"] = round(move, 3)

    sentiment, reason = classify_reaction(reaction)
    reaction["sentiment"] = sentiment
    reaction["reaction_reason"] = reason

    return reaction


def attach_release_status(events, now):
    """
    Adds:
      release_status: UPCOMING / RELEASED
      market_reaction: {...}
      reaction_label
    """
    visibility_end = market_close_visibility_time(now)

    high_medium = []
    for event in events:
        if event.get("impact") not in ["HIGH", "MEDIUM"]:
            continue

        dt = parse_event_dt(event)
        if not dt:
            continue

        item = dict(event)
        item["minutes_until"] = int((dt - now).total_seconds() // 60)

        if dt.date() == now.date() and dt <= now <= visibility_end:
            item["release_status"] = "RELEASED"
            high_medium.append(item)
        elif now <= dt <= now + timedelta(days=LOOKAHEAD_DAYS):
            item["release_status"] = "UPCOMING"
            high_medium.append(item)

    released = [e for e in high_medium if e.get("release_status") == "RELEASED"]
    if released:
        market_data = {}
        for label, symbol in REACTION_SYMBOLS.items():
            market_data[label] = fetch_yahoo_intraday(symbol)

        enriched = []
        for event in high_medium:
            if event.get("release_status") == "RELEASED":
                dt = parse_event_dt(event)
                reaction = build_reaction_snapshot(dt, now, market_data)
                sentiment = reaction.get("sentiment", "UNKNOWN")
                event["market_reaction"] = reaction
                event["reaction_sentiment"] = sentiment
                event["reaction_status"] = reaction.get("reaction_status", "UNKNOWN")
                event["reaction_reason"] = reaction.get("reaction_reason", "")
                event["reaction_label"] = f"RELEASED — {sentiment.title()} Reaction"
            else:
                event["reaction_sentiment"] = ""
                event["reaction_status"] = ""
                event["reaction_label"] = ""
            enriched.append(event)

        high_medium = enriched

    return high_medium


def build_macro_risk(events, fetch_error=""):
    now = ny_now()
    enriched_events = attach_release_status(events, now)

    released_today = [e for e in enriched_events if e.get("release_status") == "RELEASED"]
    upcoming = [e for e in enriched_events if e.get("release_status") == "UPCOMING"]

    released_today.sort(key=event_sort_key, reverse=True)
    upcoming.sort(key=event_sort_key)

    display_events = released_today + upcoming

    if fetch_error and not events:
        return {
            "risk_level": "UNKNOWN",
            "headline": "Macro Risk: UNKNOWN — calendar unavailable",
            "action": "Manually check CPI/FOMC/jobs/PCE/PPI calendar before trading.",
            "events": [],
            "released_events_today": [],
            "upcoming_events": [],
        }

    high_events = [e for e in upcoming if e.get("impact") == "HIGH"]
    medium_events = [e for e in upcoming if e.get("impact") == "MEDIUM"]
    bearish_released = [e for e in released_today if e.get("reaction_sentiment") == "BEARISH"]
    bullish_released = [e for e in released_today if e.get("reaction_sentiment") == "BULLISH"]

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

    elif bearish_released:
        first = bearish_released[0]
        risk = "MEDIUM"
        headline = f"Macro Risk: MEDIUM — released event bearish reaction: {first['name']}"
        action = "Released macro event reaction is bearish. Avoid forcing longs until indexes stabilize."

    elif bullish_released:
        first = bullish_released[0]
        risk = "LOW"
        headline = f"Macro Risk: LOW — released event bullish reaction: {first['name']}"
        action = "Released macro reaction is supportive, but continue confirming SPY/QQQ/VIX before entry."

    elif released_today:
        first = released_today[0]
        risk = "LOW"
        headline = f"Macro Risk: LOW — released event neutral/mixed reaction: {first['name']}"
        action = "No major unreleased high/medium event nearby. Continue monitoring market reaction."

    else:
        risk = "LOW"
        headline = "Macro Risk: LOW — no high/medium events flagged in lookahead window"
        action = "Normal macro backdrop. Still monitor headlines and Fed speakers."

    return {
        "risk_level": risk,
        "headline": headline,
        "action": action,
        "events": display_events[:14],
        "released_events_today": released_today[:6],
        "upcoming_events": upcoming[:12],
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
        "released_events_today": risk.get("released_events_today", []),
        "upcoming_events": risk.get("upcoming_events", []),
        "source": source,
        "source_url": NYFED_URL if source != "Unavailable" else "",
        "generated_at_et": now.strftime("%Y-%m-%d %H:%M ET"),
        "generated_at": now.strftime("%Y-%m-%d %H:%M ET"),
        "lookahead_days": LOOKAHEAD_DAYS,
        "released_sentiment_method": "Market reaction only: SPY/QQQ/IWM/VIX move after release; not actual vs forecast economic surprise.",
        "reaction_window_minutes": REACTION_WINDOW_MINUTES,
        "fetch_error": fetch_error,
    }

    with open("macro_calendar.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"  Macro risk: {output['risk_level']}")
    print(f"  Headline:   {output['headline']}")
    print(f"  Released:   {len(output.get('released_events_today', []))}")
    print(f"  Upcoming:   {len(output.get('upcoming_events', []))}")
    print(f"  Events:     {len(output['events'])}")
    print(f"  Source:     {output['source']}")
    if fetch_error:
        print(f"  ⚠ Fetch error: {fetch_error}")
    print("  ✓ Saved: macro_calendar.json")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
