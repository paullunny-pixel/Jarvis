"""Meetings, Layer A (6 Aug brief): 'start a new Zoom meeting' → one line
replaces the whole phone faff. Jarvis creates the meeting (join-before-host —
nobody waits on Paul), sends BOTH links on Telegram (host start link for him,
join link to paste to anyone), and drops them on a Trello card as backup.
'Set up a Zoom for tomorrow at 2' schedules one the same way.

Layer B (OtterPilot attend/transcribe → actions into Brain Dump) lives in
app/meetings_notetaker.py, riding Paul's existing Otter.ai; this module
only carries the consent-reminder line on the quick-start reply when it's
switched on.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# The command gate: an explicit Zoom ask, nothing subtle. Dyslexia rule —
# read for meaning ('zoon'/'soom' happen in voice transcripts).
ZOOM_ASK = re.compile(
    r"\b(start|spin\s*up|open|set\s*up|setup|create|make|book|get)\b"
    r".{0,24}\b(zoom|zoon|soom)\b"
    r"|\b(zoom|zoon|soom)\b.{0,16}\b(meeting|call|link)\b",
    re.IGNORECASE,
)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

_TOMORROW = re.compile(r"\btom+or+ow\b|\btomoro\b|\btmrw\b|\btomorrow\b", re.IGNORECASE)
_TODAY = re.compile(r"\btoday\b|\btonight\b", re.IGNORECASE)
_AT_TIME = re.compile(
    r"\b(?:at|for)\s+(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.IGNORECASE
)


def mentions_zoom(text: str) -> bool:
    return bool(ZOOM_ASK.search(text))


def parse_when(text: str, now: datetime) -> datetime | None:
    """A meeting time from Paul's words, or None = right now.
    'tomorrow at 2' → 14:00 tomorrow (1-7 bare = pm, 8-11 = am, 12 = noon);
    'Friday at 10am' → next Friday 10:00. No time named → None (instant)."""
    lowered = text.lower()
    day = None
    if _TOMORROW.search(lowered):
        day = now.date() + timedelta(days=1)
    elif _TODAY.search(lowered):
        day = now.date()
    else:
        for i, name in enumerate(WEEKDAYS):
            if re.search(rf"\b{name[:3]}\w*\b", lowered) and name[:3] in lowered:
                ahead = (i - now.weekday()) % 7 or 7
                day = now.date() + timedelta(days=ahead)
                break
    at = _AT_TIME.search(lowered)
    if not at and day is None:
        return None   # no schedule words at all — instant meeting
    hour = int(at.group(1)) if at else 10   # a day with no time: sensible 10:00
    minute = int(at.group(2) or 0) if at else 0
    meridiem = (at.group(3) or "").replace(".", "").lower() if at else ""
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and 1 <= hour <= 7:
        hour += 12   # bare '2' means 2pm, not 2am — Paul books afternoons
    if hour > 23 or minute > 59:
        return None
    if day is None:
        day = now.date()
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            day = day + timedelta(days=1)   # 'at 2' said at 5pm = tomorrow 2pm
    return now.replace(
        year=day.year, month=day.month, day=day.day,
        hour=hour, minute=minute, second=0, microsecond=0,
    )


def topic_from(text: str) -> str:
    """'set up a zoom with harry about the villa' → 'the villa' if stated,
    else a clean default. Never invents."""
    about = re.search(r"\babout\s+(.{3,60}?)(?:[.,!]|$)", text, re.IGNORECASE)
    if about:
        return about.group(1).strip().title()[:80]
    return "Paul's meeting"


class MeetingMaker:
    """Layer A service: create the meeting, compose the reply, back the
    links up onto Trello (best-effort — the reply never waits on Trello)."""

    def __init__(self, zoom, layer_factory=None, notetaker=None) -> None:
        self.zoom = zoom
        self._layer_factory = layer_factory   # async () -> TrelloLayer | None
        self.notetaker = notetaker            # MeetingNotetaker | None (Layer B)

    async def quick_start(self, text: str, now: datetime, tz_name: str) -> str:
        when = parse_when(text, now)
        topic = topic_from(text)
        meeting = await self.zoom.create_meeting(
            topic=topic,
            start_iso=when.strftime("%Y-%m-%dT%H:%M:%S") if when else None,
            tz_name=tz_name,
        )
        await self._backup_card(meeting, when)
        if when:
            shown = when.strftime("%A %H:%M")
            head = f"📅 Zoom's booked for {shown} ({tz_name}) — join-before-host is on."
        else:
            head = "🎥 Zoom's live — join-before-host is on, nobody waits for you."
        lines = [
            head,
            f"Your host link (start it from here):\n{meeting['start_url']}",
            f"Join link (paste this to whoever you like):\n{meeting['join_url']}",
        ]
        if meeting.get("password"):
            lines.append(f"Passcode if asked: {meeting['password']}")
        lines.append("Links are on a Brain Dump card too, as backup.")
        if self.notetaker is not None and await self.notetaker.enabled():
            lines.append(
                "Otter's watching this one too — worth telling the room it's being recorded."
            )
        return "\n\n".join(lines)

    async def _backup_card(self, meeting: dict, when: datetime | None) -> None:
        if self._layer_factory is None:
            return
        try:
            layer = await self._layer_factory()
            if layer is None:
                return
            stamp = when.strftime("%d %b %H:%M") if when else datetime.now().strftime("%d %b")
            await layer.create_card_full(
                board_key="master",
                list_name="Brain Dump",
                title=f"Zoom links — {meeting.get('topic', 'meeting')} ({stamp})",
                desc=(
                    f"Host/start link: {meeting.get('start_url', '')}\n\n"
                    f"Join link: {meeting.get('join_url', '')}\n\n"
                    f"Passcode: {meeting.get('password', '') or '—'}\n"
                    "Created by Jarvis (Zoom quick-start)."
                ),
            )
        except Exception:
            # Backup only — the links already reached Paul on Telegram.
            logger.exception("Zoom backup card failed (links delivered regardless)")
