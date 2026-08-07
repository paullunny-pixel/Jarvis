"""LocationAwareness — §1/§2/§4/§5 of the GPS + daily working memory brief.
Sits ON TOP of app/heartbeat/location.py's existing timezone-following pipe
(record_fix is called AFTER apply_location, from the same webhook) rather
than replacing it. §3 (traffic-aware leave-now) lives in
app/clients/maps_client.py, dormant until a maps key exists.

Design note on §2's confirm-before-assume rule: an airport arrival sends
ONE Telegram question and stores a pending flag; nothing is ACTED on
(travel day, sobriety trigger) until the brain calls the location tool's
confirm_travel — the tool result is the ground truth, never an inferred
'he probably meant yes'.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.location.geo import classify

logger = logging.getLogger(__name__)

PENDING_KEY = "gps_pending_situation"
PENDING_TTL_HOURS = 3
TRAVEL_DAY_PREFIX = "gps_travel_day:"
DAILY_MEMORY_KEY = "daily_working_memory"
DAILY_MEMORY_DATE_KEY = "daily_working_memory_date"
WAKE_HOUR_START = 7
WAKE_HOUR_END = 22

# The day rhythm's meds (app/heartbeat/jobs.py::MED_SCHEDULE, duplicated in
# miniature here rather than imported — jobs.py wires THIS module, so the
# reverse import would be circular; this is display-only, not the source
# of truth for reminder timing).
MEDS = [
    {"label": "ADHD medication", "window": "09:30-10:00, after breakfast"},
    {"label": "Supplements", "window": "14:00-15:00, after food"},
    {"label": "TRT (weekly)", "window": "Saturday"},
]


class LocationAwareness:
    def __init__(
        self, db, store, living, send_text, gcal=None, daily12=None, memory=None,
        private_track=None, timezone_default: str = "Europe/London",
    ) -> None:
        self.db = db
        self.store = store
        self.living = living
        self._send_text = send_text   # async (text: str, essential: bool) -> None
        self.gcal = gcal
        self.daily12 = daily12
        self.memory = memory
        self.private_track = private_track
        self._tz_default = timezone_default

    async def _tz_name(self) -> str:
        return await self.store.get("current_timezone", self._tz_default)

    def _within_wake_hours(self, tz_name: str) -> bool:
        try:
            local = datetime.now(ZoneInfo(tz_name))
        except Exception:
            local = datetime.now(timezone.utc)
        return WAKE_HOUR_START <= local.hour < WAKE_HOUR_END

    # ------------------------------------------------------------ §1 named places

    async def latest_fix(self) -> dict | None:
        """The last GPS fix on file — teach_place anchors to THIS, never a
        coordinate the brain might invent."""
        return await self.db.fetch_one("SELECT lat, lon FROM location_history ORDER BY id DESC LIMIT 1")

    async def teach_place(self, name: str, kind: str, lat: float, lon: float, radius_m: int = 200) -> str:
        name = name.strip()
        if not name:
            return "Need a name for this place."
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        existing = await self.db.fetch_one("SELECT id FROM named_places WHERE LOWER(name) = ?", (name.lower(),))
        if existing:
            await self.db.execute(
                "UPDATE named_places SET kind=?, lat=?, lon=?, radius_m=? WHERE id=?",
                (kind or "other", lat, lon, radius_m, existing["id"]),
            )
            return f"Updated — {name} moved to where you're standing."
        await self.db.execute(
            "INSERT INTO named_places (name, kind, lat, lon, radius_m, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, kind or "other", lat, lon, radius_m, now),
        )
        return f"Got it — this is {name} now. I'll know when you're here."

    async def forget_place(self, name: str) -> str:
        await self.db.execute("DELETE FROM named_places WHERE LOWER(name) = ?", (name.strip().lower(),))
        return f"Forgotten — {name} is no longer a place I track."

    async def list_places(self) -> list[dict]:
        return await self.db.fetch_all("SELECT name, kind, radius_m FROM named_places ORDER BY name")

    async def _named_places(self) -> list[dict]:
        return await self.db.fetch_all("SELECT name, kind, lat, lon, radius_m FROM named_places")

    # ------------------------------------------------------------ §1/§2 the main entry point

    async def record_fix(self, lat: float, lon: float) -> dict:
        """Called AFTER app.heartbeat.location.apply_location() on every GPS
        post. Writes history, classifies, and — only on a genuine arrival,
        only in wake hours — fires the downstream consumers (§2 confirm,
        §4 geofence tasks, §5 surfacing)."""
        places = await self._named_places()
        current = classify(lat, lon, places)
        now = datetime.now(timezone.utc)
        tz_name = await self._tz_name()
        last = await self._last_classification()
        await self.db.execute(
            "INSERT INTO location_history (ts, lat, lon, place_name, place_kind, tz) VALUES (?, ?, ?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), lat, lon, current["name"], current["kind"], tz_name),
        )
        arrived = bool(current["name"]) and (last is None or last["place_name"] != current["name"])
        result = {"place": current["name"] or "unknown place", "kind": current["kind"], "arrived": arrived}
        if arrived and self._within_wake_hours(tz_name):
            try:
                await self._on_arrival(current, now)
            except Exception:
                logger.exception("Location arrival handling failed — history still recorded")
        return result

    async def _last_classification(self) -> dict | None:
        return await self.db.fetch_one(
            "SELECT place_name, place_kind FROM location_history ORDER BY id DESC LIMIT 1"
        )

    async def _on_arrival(self, place: dict, now: datetime) -> None:
        if place["kind"] == "airport":
            await self._confirm_airport(place, now)
        await self._fire_geofence_tasks(place["name"])
        await self._surface_daily_item(place["name"])

    # ------------------------------------------------------------ §2 situational awareness

    async def _confirm_airport(self, place: dict, now: datetime) -> None:
        existing = await self.store.get(PENDING_KEY, "")
        if existing:
            return   # already asked this visit, awaiting the answer
        await self.store.set(PENDING_KEY, json.dumps({
            "kind": "airport", "place": place["name"], "since": now.isoformat(timespec="seconds"),
        }))
        await self._send_text(f"You're at {place['name']} — flying today?", essential=False)

    async def pending_situation_note(self) -> str:
        """Injected into system_status (peek, not pop — clearing happens
        only when confirm_travel is actually called, so the brain always
        has the context to resolve it, however many turns it takes)."""
        raw = await self.store.get(PENDING_KEY, "")
        if not raw:
            return ""
        try:
            pending = json.loads(raw)
            since = datetime.fromisoformat(pending["since"])
        except Exception:
            await self.store.set(PENDING_KEY, "")
            return ""
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - since > timedelta(hours=PENDING_TTL_HOURS):
            await self.store.set(PENDING_KEY, "")
            return ""
        return (
            f"GPS ARRIVAL: Paul reached {pending['place']} and you asked if he's flying "
            "today but haven't heard back. If his next words answer that, call the "
            "location tool's confirm_travel action with flying=true/false — never just "
            "say it's noted without the tool confirming it."
        )

    async def confirm_travel(self, flying: bool) -> str:
        await self.store.set(PENDING_KEY, "")
        if not flying:
            return "Noted — not a flying day."
        today = datetime.now(timezone.utc).date()
        await self.store.set(f"{TRAVEL_DAY_PREFIX}{today.isoformat()}", "1")
        if self.living is not None:
            try:
                await self.living.set("today.travel_day", "true", room="you")
            except Exception:
                logger.exception("Travel-day living fact failed")
        heads_up = ""
        if self.private_track is not None:
            try:
                heads_up = await self.private_track.trigger_message(["gps_airport"], today)
            except Exception:
                logger.exception("Travel sobriety trigger failed")
        if heads_up:
            await self._send_text(heads_up, essential=True)
        return "Got it — travel day. I'll keep today light, and I've flagged it privately too."

    async def is_travel_day(self, today: date) -> bool:
        return await self.store.get(f"{TRAVEL_DAY_PREFIX}{today.isoformat()}", "") == "1"

    # ------------------------------------------------------------ §4 geofenced tasks

    async def add_geofence_task(self, place_name: str, text: str) -> str:
        places = await self._named_places()
        if not any(p["name"].lower() == place_name.strip().lower() for p in places):
            return (
                f"I don't have '{place_name}' taught as a place yet — stand there and "
                f"say \"this is {place_name}\" first."
            )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await self.db.execute(
            "INSERT INTO geofence_tasks (place_name, text, created_at) VALUES (?, ?, ?)",
            (place_name.strip(), text.strip(), now),
        )
        return f"Done — I'll remind you about '{text}' next time you're at {place_name}."

    async def remove_geofence_task(self, place_name: str, text: str) -> str:
        await self.db.execute(
            "UPDATE geofence_tasks SET active = 0 WHERE LOWER(place_name) = ? AND LOWER(text) LIKE ?",
            (place_name.strip().lower(), f"%{text.strip().lower()}%"),
        )
        return f"Cleared reminders about '{text}' at {place_name}."

    async def list_geofence_tasks(self) -> list[dict]:
        return await self.db.fetch_all(
            "SELECT place_name, text, fired_at FROM geofence_tasks WHERE active = 1 ORDER BY place_name"
        )

    async def _fire_geofence_tasks(self, place_name: str) -> None:
        rows = await self.db.fetch_all(
            "SELECT id, text FROM geofence_tasks WHERE place_name = ? AND active = 1", (place_name,)
        )
        if not rows:
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        message = f"At {place_name} — " + "; ".join(r["text"] for r in rows)
        await self._send_text(message, essential=False)
        for r in rows:
            await self.db.execute("UPDATE geofence_tasks SET fired_at = ? WHERE id = ?", (now, r["id"]))

    # ------------------------------------------------------------ §5 daily working memory

    async def build_daily_memory(self, plan_date: date | None = None) -> dict:
        tz_name = await self._tz_name()
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo(self._tz_default)
        plan_date = plan_date or datetime.now(tz).date()

        focus = []
        if self.daily12 is not None:
            try:
                rows = await self.daily12.plan(plan_date)
                focus = [
                    {"title": r["title"], "company": r["company_slug"], "due": r["due_date"],
                     "done": bool(r["done"]), "url": r["url"]}
                    for r in rows if r["position"] != 0
                ]
            except Exception:
                logger.exception("Daily memory: focus list failed")

        events = []
        if self.gcal is not None:
            try:
                for ev in await self.gcal.today(tz):
                    events.append({
                        "title": ev["title"],
                        "start": ev["start"].isoformat() if hasattr(ev["start"], "isoformat") else str(ev["start"]),
                        "location": ev.get("location", ""),
                        "attendees": ev.get("attendees", []),
                        "join_url": ev.get("join_url", ""),
                        "resources": await self._resources_for(ev.get("title", "")),
                    })
            except Exception:
                logger.exception("Daily memory: calendar failed")

        memory = {
            "date": plan_date.isoformat(), "focus": focus, "events": events, "meds": MEDS,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "surfaced": [],
        }
        await self.store.set(DAILY_MEMORY_KEY, json.dumps(memory))
        await self.store.set(DAILY_MEMORY_DATE_KEY, plan_date.isoformat())
        return memory

    async def _resources_for(self, title: str) -> list[str]:
        if self.memory is None or not title:
            return []
        try:
            chunks = await self.memory.search(title, k=3, min_score=0.2)
            return [c["content"][:200] for c in chunks]
        except Exception:
            return []

    async def get_daily_memory(self) -> dict:
        tz_name = await self._tz_name()
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo(self._tz_default)
        today = datetime.now(tz).date()
        stored_date = await self.store.get(DAILY_MEMORY_DATE_KEY, "")
        if stored_date != today.isoformat():
            return await self.build_daily_memory(today)
        try:
            return json.loads(await self.store.get(DAILY_MEMORY_KEY, "{}")) or await self.build_daily_memory(today)
        except Exception:
            return await self.build_daily_memory(today)

    async def _surface_daily_item(self, place_name: str) -> None:
        memory = await self.get_daily_memory()
        if not memory:
            return
        surfaced = set(memory.get("surfaced", []))
        for ev in memory.get("events", []):
            location = ev.get("location", "")
            if not location or place_name.lower() not in location.lower():
                continue
            if ev["title"] in surfaced:
                continue
            lines = [f"You're at {place_name} — \"{ev['title']}\" is here."]
            if ev.get("attendees"):
                lines.append(f"With: {', '.join(ev['attendees'][:4])}.")
            if ev.get("resources"):
                lines.append("Notes: " + " | ".join(ev["resources"][:2]))
            await self._send_text(" ".join(lines), essential=False)
            surfaced.add(ev["title"])
            memory["surfaced"] = list(surfaced)
            await self.store.set(DAILY_MEMORY_KEY, json.dumps(memory))
            break   # one at a time — don't flood on arrival
