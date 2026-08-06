"""Google Calendar service (6 Aug brief) — live read + guarded write.

Replaces the stale ICS feed as the primary calendar (ICS stays as fallback
until this is authorised). Everything is stored/requested in UTC and rendered
in PAUL'S CURRENT timezone; when an event was created in a different zone the
rendering says so out loud ("that's 09:00 UK, 13:00 your time").

WRITE GUARDRAILS (Part 6 — the most destructive permission Jarvis has):
creating is fine unprompted; destroying is not. Moving or deleting an event
Jarvis didn't create — or one Paul doesn't organise — parks the action as a
PENDING CONFIRMATION and nothing touches Google until Paul says the word.
Every write lands in calendar_log ('what did you change in my calendar
today?'), and 'undo' reverses the last one.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.clients.gcal_client import GoogleCalendarClient, ReauthNeeded
from app.core.store import SettingsStore, utc_now_iso

logger = logging.getLogger(__name__)

TOKEN_KEY = "google_refresh_token"
IGNORED_KEY = "gcal_ignored_calendars"     # JSON list of calendar names/ids
PENDING_KEY = "gcal_pending_write"         # parked destructive action
JARVIS_TAG = {"private": {"jarvis": "1"}}  # extendedProperties marker

TZ_LABELS = {
    "Europe/London": "UK", "Asia/Dubai": "Dubai", "America/Sao_Paulo": "Brazil",
}


class NeedsConfirm(Exception):
    """A destructive write on an event Jarvis doesn't own — park and ask."""


class GoogleCalendar:
    def __init__(self, client: GoogleCalendarClient, db, store: SettingsStore) -> None:
        self.client = client
        self.db = db
        self.store = store
        self._token_loaded = False

    async def _ensure_token(self) -> None:
        if not self._token_loaded:
            self.client.refresh_token = await self.store.get(TOKEN_KEY, "")
            self._token_loaded = True
        if not self.client.refresh_token:
            raise ReauthNeeded("Google Calendar not connected yet")

    async def authorised(self) -> bool:
        return bool(await self.store.get(TOKEN_KEY, ""))

    async def store_token(self, refresh_token: str) -> None:
        await self.store.set(TOKEN_KEY, refresh_token)
        self.client.refresh_token = refresh_token
        self._token_loaded = True

    # ------------------------------------------------------------ reads

    async def _calendar_ids(self) -> list[str]:
        try:
            ignored = {s.lower() for s in json.loads(await self.store.get(IGNORED_KEY, "[]"))}
        except Exception:
            ignored = set()
        cals = await self.client.calendars()
        return [
            c["id"] for c in cals
            if c["id"].lower() not in ignored and c["name"].lower() not in ignored
        ] or ["primary"]

    async def ignore_calendar(self, name: str) -> str:
        try:
            ignored = json.loads(await self.store.get(IGNORED_KEY, "[]"))
        except Exception:
            ignored = []
        if name.lower() not in [i.lower() for i in ignored]:
            ignored.append(name)
            await self.store.set(IGNORED_KEY, json.dumps(ignored))
        return f"Ignoring the '{name}' calendar from now on."

    @staticmethod
    def _normalize(raw: dict, calendar_id: str, tz: ZoneInfo) -> dict | None:
        start_raw = raw.get("start", {})
        all_day = "date" in start_raw and "dateTime" not in start_raw
        try:
            if all_day:
                start = datetime.fromisoformat(start_raw["date"]).replace(tzinfo=tz)
                end = start + timedelta(days=1)
            else:
                start = datetime.fromisoformat(start_raw["dateTime"])
                end_raw = raw.get("end", {}).get("dateTime")
                end = datetime.fromisoformat(end_raw) if end_raw else start + timedelta(hours=1)
        except (KeyError, ValueError):
            return None
        declined = any(
            a.get("self") and a.get("responseStatus") == "declined"
            for a in raw.get("attendees", [])
        )
        join = raw.get("hangoutLink", "")
        if not join:
            for ep in (raw.get("conferenceData", {}).get("entryPoints") or []):
                if ep.get("entryPointType") == "video" and ep.get("uri"):
                    join = ep["uri"]
                    break
        if not join:
            import re as _re

            for field in (raw.get("location", ""), raw.get("description", "")):
                m = _re.search(r"https://[^\s<>\"]*(?:zoom\.us|meet\.google|teams\.microsoft)[^\s<>\"]*", field or "")
                if m:
                    join = m.group(0)
                    break
        organizer_self = bool(raw.get("organizer", {}).get("self"))
        jarvis_made = (raw.get("extendedProperties", {}).get("private", {}) or {}).get("jarvis") == "1"
        return {
            "id": raw.get("id", ""),
            "calendar_id": calendar_id,
            "title": raw.get("summary", "(no title)"),
            "start": start, "end": end, "all_day": all_day,
            "declined": declined,
            "location": raw.get("location", ""),
            "join_url": join,
            "attendees": [a.get("email", "") for a in raw.get("attendees", []) if not a.get("self")],
            "organizer_self": organizer_self or not raw.get("attendees"),
            "jarvis_made": jarvis_made,
            "origin_tz": start_raw.get("timeZone", ""),
        }

    async def _window(self, start_utc: datetime, end_utc: datetime, tz: ZoneInfo) -> list[dict]:
        await self._ensure_token()
        events: list[dict] = []
        for cal_id in await self._calendar_ids():
            try:
                for raw in await self.client.events(
                    cal_id,
                    start_utc.isoformat().replace("+00:00", "Z"),
                    end_utc.isoformat().replace("+00:00", "Z"),
                ):
                    ev = self._normalize(raw, cal_id, tz)
                    if ev:
                        events.append(ev)
            except ReauthNeeded:
                raise
            except Exception:
                logger.exception("Calendar %s read failed — continuing without it", cal_id)
        events.sort(key=lambda e: e["start"])
        return events

    def _shape(self, ev: dict, now: datetime, tz: ZoneInfo) -> dict:
        local = ev["start"].astimezone(tz)
        shaped = {
            "title": ev["title"],
            "start": local.isoformat(timespec="minutes"),
            "time": "all day" if ev["all_day"] else local.strftime("%H:%M"),
            "minutes_until": max(0, int((ev["start"] - now).total_seconds() // 60)),
            "location": ev["location"],
            "join_url": ev["join_url"],
            "attendees": ev["attendees"],
            "declined": ev["declined"],
            "all_day": ev["all_day"],
        }
        # The timezone trap (Part 3): an event born in another zone says so.
        if ev["origin_tz"] and not ev["all_day"]:
            try:
                origin = ZoneInfo(ev["origin_tz"])
                if origin.utcoffset(ev["start"]) != tz.utcoffset(ev["start"]):
                    origin_local = ev["start"].astimezone(origin)
                    label = TZ_LABELS.get(ev["origin_tz"], ev["origin_tz"])
                    shaped["tz_note"] = (
                        f"that's {origin_local.strftime('%H:%M')} {label}, "
                        f"{local.strftime('%H:%M')} your time"
                    )
            except Exception:
                pass
        return shaped

    async def next_up(self, tz: ZoneInfo) -> dict | None:
        """The next non-declined, non-all-day event with a live minutes-until."""
        now = datetime.now(ZoneInfo("UTC"))
        for ev in await self._window(now, now + timedelta(days=7), tz):
            if ev["declined"] or ev["all_day"]:
                continue
            if ev["start"] >= now:
                return self._shape(ev, now, tz)
        return None

    async def today(self, tz: ZoneInfo) -> list[dict]:
        now = datetime.now(ZoneInfo("UTC"))
        local_now = now.astimezone(tz)
        end = local_now.replace(hour=23, minute=59, second=59)
        return [
            self._shape(e, now, tz)
            for e in await self._window(now, end.astimezone(ZoneInfo("UTC")), tz)
            if not e["declined"]
        ]

    async def week(self, tz: ZoneInfo) -> list[dict]:
        now = datetime.now(ZoneInfo("UTC"))
        return [
            self._shape(e, now, tz)
            for e in await self._window(now, now + timedelta(days=7), tz)
            if not e["declined"]
        ]

    async def events_for(self, day: date, tz: ZoneInfo) -> list[dict]:
        """ICS-shaped [{'time','title'}] so the brief/jobs swap in seamlessly."""
        start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
        events = await self._window(
            start_local.astimezone(ZoneInfo("UTC")),
            (start_local + timedelta(days=1)).astimezone(ZoneInfo("UTC")),
            tz,
        )
        return [
            {"time": "all day" if e["all_day"] else e["start"].astimezone(tz).strftime("%H:%M"),
             "title": e["title"]}
            for e in events if not e["declined"]
        ]

    # ------------------------------------------------------------ writes

    async def _log(self, action: str, title: str, detail: str, calendar_id: str,
                   event_id: str, undo: dict) -> None:
        await self.db.execute(
            "INSERT INTO calendar_log (ts, action, title, detail, calendar_id, event_id, undo)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (utc_now_iso(), action, title[:200], detail[:500], calendar_id, event_id,
             json.dumps(undo)[:4000]),
        )

    async def create(self, title: str, start_local: datetime, duration_min: int = 60,
                     attendees: list[str] | None = None, description: str = "") -> str:
        await self._ensure_token()
        end_local = start_local + timedelta(minutes=duration_min)
        body = {
            "summary": title,
            "start": {"dateTime": start_local.isoformat(), "timeZone": str(start_local.tzinfo)},
            "end": {"dateTime": end_local.isoformat(), "timeZone": str(start_local.tzinfo)},
            "extendedProperties": JARVIS_TAG,
        }
        if description:
            body["description"] = description
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        created = await self.client.create_event("primary", body)
        await self._log(
            "created", title, f"at {start_local.strftime('%a %d %b %H:%M')} ({start_local.tzinfo})",
            "primary", created.get("id", ""), {"kind": "created"},
        )
        return (
            f"📅 '{title}' is in for {start_local.strftime('%A %H:%M')} "
            f"({duration_min} min){' with ' + ', '.join(attendees) if attendees else ''}."
        )

    async def _find(self, query: str, tz: ZoneInfo) -> dict | None:
        now = datetime.now(ZoneInfo("UTC"))
        candidates = [
            e for e in await self._window(now - timedelta(hours=2), now + timedelta(days=14), tz)
            if query.lower() in e["title"].lower()
        ]
        return candidates[0] if candidates else None

    def _guard(self, ev: dict, action: str) -> None:
        if not (ev["jarvis_made"] and ev["organizer_self"]):
            raise NeedsConfirm(
                f"'{ev['title']}' isn't one of mine"
                + ("" if ev["organizer_self"] else " — and you're not the organiser")
                + f". Say 'confirm calendar change' within 10 minutes and I'll {action} it."
            )

    async def _park(self, action: dict) -> None:
        action["expires"] = (datetime.now(ZoneInfo("UTC")) + timedelta(minutes=10)).isoformat()
        await self.store.set(PENDING_KEY, json.dumps(action, default=str))

    async def move(self, query: str, new_start_local: datetime, tz: ZoneInfo,
                   confirmed: bool = False) -> str:
        await self._ensure_token()
        ev = await self._find(query, tz)
        if ev is None:
            return f"I can't find an upcoming event matching '{query}' — nothing was touched."
        if not confirmed:
            try:
                self._guard(ev, "move")
            except NeedsConfirm as ask:
                await self._park({"kind": "move", "query": query,
                                  "new_start": new_start_local.isoformat(), "tz": str(tz)})
                return "⚠️ " + str(ask)
        old_start = ev["start"]
        duration = ev["end"] - ev["start"]
        body = {
            "start": {"dateTime": new_start_local.isoformat(), "timeZone": str(tz)},
            "end": {"dateTime": (new_start_local + duration).isoformat(), "timeZone": str(tz)},
        }
        await self.client.patch_event(ev["calendar_id"], ev["id"], body)
        await self._log(
            "moved", ev["title"],
            f"{old_start.astimezone(tz).strftime('%a %H:%M')} → {new_start_local.strftime('%a %H:%M')}",
            ev["calendar_id"], ev["id"],
            {"kind": "moved", "old_start": old_start.isoformat(),
             "old_end": ev["end"].isoformat(), "tz": str(tz)},
        )
        return f"📅 '{ev['title']}' moved to {new_start_local.strftime('%A %H:%M')}."

    async def delete(self, query: str, tz: ZoneInfo, confirmed: bool = False) -> str:
        await self._ensure_token()
        ev = await self._find(query, tz)
        if ev is None:
            return f"I can't find an upcoming event matching '{query}' — nothing was touched."
        if not confirmed:
            try:
                self._guard(ev, "delete")
            except NeedsConfirm as ask:
                await self._park({"kind": "delete", "query": query, "tz": str(tz)})
                return "⚠️ " + str(ask)
        raw = await self.client.get_event(ev["calendar_id"], ev["id"])
        await self.client.delete_event(ev["calendar_id"], ev["id"])
        raw.pop("id", None)
        await self._log(
            "deleted", ev["title"], ev["start"].astimezone(tz).strftime("%a %d %b %H:%M"),
            ev["calendar_id"], ev["id"], {"kind": "deleted", "body": raw},
        )
        return f"🗑 '{ev['title']}' is off the calendar."

    async def add_attendees(self, query: str, emails: list[str], tz: ZoneInfo) -> str:
        await self._ensure_token()
        ev = await self._find(query, tz)
        if ev is None:
            return f"I can't find an upcoming event matching '{query}'."
        raw = await self.client.get_event(ev["calendar_id"], ev["id"])
        current = raw.get("attendees", [])
        have = {a.get("email", "").lower() for a in current}
        current.extend({"email": e} for e in emails if e.lower() not in have)
        await self.client.patch_event(ev["calendar_id"], ev["id"], {"attendees": current})
        await self._log("invited", ev["title"], ", ".join(emails), ev["calendar_id"], ev["id"],
                        {"kind": "invited"})
        return f"📨 Invited {', '.join(emails)} to '{ev['title']}'."

    async def confirm_pending(self, tz: ZoneInfo) -> str:
        raw = await self.store.get(PENDING_KEY, "")
        await self.store.set(PENDING_KEY, "")
        if not raw:
            return "There's no calendar change waiting on a confirm."
        action = json.loads(raw)
        if datetime.fromisoformat(action["expires"]) < datetime.now(ZoneInfo("UTC")):
            return "That confirmation window closed — ask me again and I'll re-check."
        if action["kind"] == "move":
            return await self.move(
                action["query"], datetime.fromisoformat(action["new_start"]), tz, confirmed=True
            )
        if action["kind"] == "delete":
            return await self.delete(action["query"], tz, confirmed=True)
        return "That parked change isn't one I recognise — nothing was touched."

    async def undo_last(self, tz: ZoneInfo) -> str:
        row = await self.db.fetch_one(
            "SELECT * FROM calendar_log ORDER BY id DESC LIMIT 1"
        )
        if row is None:
            return "Nothing to undo — I haven't touched the calendar."
        undo = json.loads(row["undo"])
        kind = undo.get("kind")
        if kind == "created":
            await self.client.delete_event(row["calendar_id"], row["event_id"])
            result = f"Undone — '{row['title']}' removed again."
        elif kind == "moved":
            tzinfo = ZoneInfo(undo.get("tz", "UTC"))
            body = {
                "start": {"dateTime": undo["old_start"], "timeZone": str(tzinfo)},
                "end": {"dateTime": undo["old_end"], "timeZone": str(tzinfo)},
            }
            await self.client.patch_event(row["calendar_id"], row["event_id"], body)
            result = f"Undone — '{row['title']}' is back where it was."
        elif kind == "deleted":
            await self.client.create_event(row["calendar_id"], undo["body"])
            result = f"Undone — '{row['title']}' restored."
        else:
            return f"The last change ({row['action']} '{row['title']}') isn't reversible from here."
        await self.db.execute("DELETE FROM calendar_log WHERE id = ?", (row["id"],))
        return result

    async def changes_today(self, tz: ZoneInfo) -> str:
        day_start = datetime.now(tz).replace(hour=0, minute=0, second=0).astimezone(ZoneInfo("UTC"))
        rows = await self.db.fetch_all(
            "SELECT * FROM calendar_log WHERE ts >= ? ORDER BY id", (day_start.isoformat(),)
        )
        if not rows:
            return "I haven't changed anything in your calendar today."
        lines = ["Today's calendar changes:"]
        for r in rows:
            lines.append(f"• {r['action']} '{r['title']}' — {r['detail']}")
        return "\n".join(lines)
