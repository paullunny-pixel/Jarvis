"""Read-only calendar via Google Calendar's secret iCal URL — zero OAuth for
Phase 1 (two-way Calendar is Phase 2 per Plan §9). Paul pastes the 'Secret
address in iCal format' from calendar settings; Jarvis reads the day's events.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)


def _unescape(value: str) -> str:
    return value.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").strip()


def _parse_dt(value: str, tz: ZoneInfo) -> tuple[datetime | None, bool]:
    """Returns (start, all_day). Handles UTC 'Z', local-with-TZID and date-only."""
    value = value.strip()
    if re.fullmatch(r"\d{8}", value):  # all-day
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=tz), True
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC")), False
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=tz), False
    except ValueError:
        return None, False


def parse_ics_events(ics_text: str, day: date, tz: ZoneInfo) -> list[dict]:
    """Extract the day's events: [{'time': '14:00' | 'all day', 'title': ...}]."""
    # Unfold continuation lines (RFC 5545)
    unfolded = re.sub(r"\r?\n[ \t]", "", ics_text)
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfolded, re.DOTALL):
        summary = ""
        start: datetime | None = None
        all_day = False
        for line in block.splitlines():
            if line.startswith("SUMMARY"):
                summary = _unescape(line.split(":", 1)[-1])
            elif line.startswith("DTSTART"):
                raw = line.split(":", 1)[-1]
                tzid = re.search(r"TZID=([^:;]+)", line)
                event_tz = ZoneInfo(tzid.group(1)) if tzid else tz
                start, all_day = _parse_dt(raw, event_tz)
        if start is None or not summary:
            continue
        local = start.astimezone(tz)
        if local.date() == day:
            events.append(
                {
                    "time": "all day" if all_day else local.strftime("%H:%M"),
                    "title": summary,
                    "sort": "00:00" if all_day else local.strftime("%H:%M"),
                }
            )
    events.sort(key=lambda e: e["sort"])
    return [{"time": e["time"], "title": e["title"]} for e in events]


TRAVEL_HINT = re.compile(
    r"\b(flight|fly|airport|dxb|lhr|ema|man\b|gate|boarding|trade ?show|expo|exhibition|conference)\b",
    re.IGNORECASE,
)


def travel_or_event_flags(events: list[dict]) -> list[str]:
    """Titles that look like flights/trade shows — used by the private track
    to get ahead of known triggers (never named in business output)."""
    return [e["title"] for e in events if TRAVEL_HINT.search(e["title"])]


class IcsCalendar:
    def __init__(self, ics_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._url = ics_url
        self._client = httpx.AsyncClient(transport=transport, timeout=30.0, follow_redirects=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def events_for(self, day: date, tz: ZoneInfo) -> list[dict]:
        if not self._url:
            return []
        try:
            response = await self._client.get(self._url)
            response.raise_for_status()
            return parse_ics_events(response.text, day, tz)
        except Exception:
            logger.exception("Calendar fetch failed — continuing without")
            return []
