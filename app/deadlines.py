"""Deadline radar (7 Aug build slice): key dates Paul tells Jarvis about
(birthdays, the villa demand, move-out, renewals) plus Trello due dates,
counted down proactively at tunable lead times so nothing ambushes him.

Scope note: calendar events already have their own live surfaces (the
daily brief, the desktop "Next up" panel, 'what's on today') with their
own near-term countdowns — folding them in here would just duplicate that,
not cover a gap. The genuine gap this brief targets is the FAR horizon
(weeks out) nothing currently tracks at all: dates only Paul knows about,
and Trello due dates that scroll past without anyone counting down to them.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from app.core.store import utc_now_iso
from app.db.base import Database

logger = logging.getLogger(__name__)

# Tunable here (Paul's rule for thresholds like this — tell the engineer
# for a change, not a runtime setting): countdown reminders fire at these
# lead times, plus the day itself.
LEAD_DAYS = (28, 7, 3, 0)
TRELLO_LOOKAHEAD_DAYS = 60


class DeadlineRadar:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add_date(self, label: str, date_iso: str, recurring_yearly: bool = False) -> str:
        label = label.strip()
        date_iso = (date_iso or "")[:10]
        if not label or not date_iso:
            return "NOTHING ADDED — need both a label and a date."
        try:
            date.fromisoformat(date_iso)
        except ValueError:
            return f"'{date_iso}' isn't a date I can read — give me YYYY-MM-DD."
        twin = await self._db.fetch_one(
            "SELECT id FROM key_dates WHERE label = ? AND date = ?", (label, date_iso)
        )
        if twin:
            return f"Already tracking '{label}' on {date_iso} — not duplicating it."
        await self._db.execute(
            "INSERT INTO key_dates (label, date, recurring_yearly, created_at) VALUES (?, ?, ?, ?)",
            (label, date_iso, 1 if recurring_yearly else 0, utc_now_iso()),
        )
        return f"Tracking '{label}' — {date_iso}{' (repeats yearly)' if recurring_yearly else ''}."

    async def remove_date(self, label: str) -> str:
        row = await self._db.fetch_one(
            "SELECT id FROM key_dates WHERE label = ?", (label.strip(),)
        )
        if row is None:
            return f"Couldn't find '{label}' on the radar."
        await self._db.execute("DELETE FROM key_dates WHERE id = ?", (row["id"],))
        return f"'{label}' dropped from the radar."

    async def list_dates(self, today: date) -> list[dict[str, Any]]:
        return sorted(await self._key_dates(today), key=lambda d: d["date"])

    async def _key_dates(self, today: date) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT id, label, date, recurring_yearly FROM key_dates"
        )
        out = []
        for r in rows:
            try:
                d = date.fromisoformat(r["date"][:10])
            except ValueError:
                continue
            if r["recurring_yearly"]:
                try:
                    d = d.replace(year=today.year)
                except ValueError:
                    d = d.replace(year=today.year, day=28)  # 29 Feb on a non-leap year
                if d < today:
                    try:
                        d = d.replace(year=today.year + 1)
                    except ValueError:
                        d = d.replace(year=today.year + 1, day=28)
            out.append({"ref": f"key:{r['id']}", "label": r["label"], "date": d, "source": "told"})
        return out

    async def _trello_dates(self, today: date) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT id, title, due_date FROM tasks WHERE actionable = 1 AND due_date != ''"
        )
        cutoff = today + timedelta(days=TRELLO_LOOKAHEAD_DAYS)
        out = []
        for r in rows:
            try:
                d = datetime.fromisoformat(r["due_date"].replace("Z", "+00:00")).date()
            except ValueError:
                continue
            if today <= d <= cutoff:
                out.append({"ref": f"trello:{r['id']}", "label": r["title"], "date": d, "source": "trello"})
        return out

    async def upcoming(self, today: date) -> list[dict[str, Any]]:
        """Everything on the radar, nearest first."""
        items = (await self._key_dates(today)) + (await self._trello_dates(today))
        for item in items:
            item["days_until"] = (item["date"] - today).days
        return sorted(items, key=lambda i: i["date"])

    async def due_countdowns(self, today: date) -> list[dict[str, Any]]:
        """Items sitting on one of LEAD_DAYS today — the tick's worklist."""
        return [i for i in await self.upcoming(today) if i["days_until"] in LEAD_DAYS]

    @staticmethod
    def countdown_line(item: dict[str, Any]) -> str:
        days = item["days_until"]
        when = "today" if days == 0 else ("tomorrow" if days == 1 else f"in {days} days")
        return f"{item['label']} — {when} ({item['date'].strftime('%d %b')})."
