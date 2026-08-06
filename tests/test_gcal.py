"""Google Calendar slice (6 Aug): OAuth client, live reads in Paul's
timezone, guarded writes with confirm/undo/trail, and the connect flow."""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.clients.gcal_client import GcalError, GoogleCalendarClient, ReauthNeeded
from app.core.store import SettingsStore
from app.db.sqlite import SqliteDatabase
from app.heartbeat.gcalendar import TOKEN_KEY, GoogleCalendar

DUBAI = ZoneInfo("Asia/Dubai")
UTC = ZoneInfo("UTC")


def uk_event(hour=9, title="BMI review", **extra):
    """An event created in the UK (the timezone trap: read from Dubai)."""
    start = datetime(2026, 8, 7, hour, 0, tzinfo=ZoneInfo("Europe/London"))
    body = {
        "id": extra.pop("id", "ev1"),
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": "Europe/London"},
        "end": {"dateTime": (start + timedelta(hours=1)).isoformat(), "timeZone": "Europe/London"},
    }
    body.update(extra)
    return body


class FakeGoogle:
    """MockTransport handler covering token + calendar endpoints."""

    def __init__(self):
        self.events = [uk_event()]
        self.calendars = [{"id": "primary", "summary": "Paul", "primary": True}]
        self.requests = []
        self.token_status = 200
        self.deleted = []
        self.patched = {}
        self.created = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((request.method, url))
        if "oauth2.googleapis.com/token" in url:
            if self.token_status != 200:
                return httpx.Response(self.token_status, text='{"error": "invalid_grant"}')
            return httpx.Response(200, json={
                "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
            })
        if "/users/me/calendarList" in url:
            return httpx.Response(200, json={"items": self.calendars})
        if request.method == "GET" and "/events/" not in url and url.rstrip("?").split("?")[0].endswith("/events"):
            return httpx.Response(200, json={"items": self.events})
        if request.method == "POST" and "/events" in url:
            body = json.loads(request.content)
            body["id"] = f"new{len(self.created) + 1}"
            self.created.append(body)
            return httpx.Response(200, json=body)
        if request.method == "PATCH":
            self.patched[url.split("/events/")[-1]] = json.loads(request.content)
            return httpx.Response(200, json={"id": url.split("/events/")[-1]})
        if request.method == "DELETE":
            self.deleted.append(url.split("/events/")[-1])
            return httpx.Response(204)
        if request.method == "GET" and "/events/" in url:
            return httpx.Response(200, json=self.events[0])
        return httpx.Response(404, text="unhandled: " + url)


class GcalBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.google = FakeGoogle()
        self.client = GoogleCalendarClient(
            "CID", "CSECRET", transport=httpx.MockTransport(self.google)
        )
        self.store = SettingsStore(self.db)
        self.gcal = GoogleCalendar(self.client, self.db, self.store)
        await self.store.set(TOKEN_KEY, "RT")

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()


class TestClient(GcalBase):
    async def test_auth_url_carries_offline_consent_and_state(self):
        url = self.client.auth_url("https://x/google/callback", state="SEC")
        self.assertIn("access_type=offline", url)
        self.assertIn("prompt=consent", url)
        self.assertIn("state=SEC", url)
        self.assertIn("calendar", url)

    async def test_dead_refresh_token_raises_reauth(self):
        self.google.token_status = 400
        with self.assertRaises(ReauthNeeded):
            await self.gcal.next_up(DUBAI)

    async def test_no_token_at_all_raises_reauth(self):
        await self.store.set(TOKEN_KEY, "")
        gcal = GoogleCalendar(self.client, self.db, self.store)
        with self.assertRaises(ReauthNeeded):
            await gcal.next_up(DUBAI)


class TestReads(GcalBase):
    async def test_uk_event_reads_in_dubai_time_with_tz_note(self):
        # 09:00 UK = 12:00 Dubai (BST +1 vs +4). The trap test from the brief.
        self.google.events = [uk_event(hour=9)]
        week = await self.gcal.week(DUBAI)
        [ev] = week
        self.assertEqual(ev["time"], "12:00")
        self.assertIn("09:00 UK", ev.get("tz_note", ""))
        self.assertIn("12:00 your time", ev.get("tz_note", ""))

    async def test_next_up_skips_declined_and_all_day(self):
        self.google.events = [
            {"id": "allday", "summary": "Bank holiday",
             "start": {"date": "2026-08-07"}, "end": {"date": "2026-08-08"}},
            uk_event(hour=9, id="declined", title="Skipped one",
                     attendees=[{"self": True, "responseStatus": "declined"}]),
            uk_event(hour=11, id="real", title="The real one"),
        ]
        nxt = await self.gcal.next_up(DUBAI)
        self.assertEqual(nxt["title"], "The real one")
        self.assertGreaterEqual(nxt["minutes_until"], 0)

    async def test_join_url_found_in_conference_data(self):
        self.google.events = [uk_event(
            conferenceData={"entryPoints": [
                {"entryPointType": "video", "uri": "https://zoom.us/j/777"}
            ]}
        )]
        [ev] = await self.gcal.week(DUBAI)
        self.assertEqual(ev["join_url"], "https://zoom.us/j/777")

    async def test_events_for_matches_ics_shape(self):
        events = await self.gcal.events_for(
            datetime(2026, 8, 7, tzinfo=DUBAI).date(), DUBAI
        )
        self.assertEqual(events, [{"time": "12:00", "title": "BMI review"}])

    async def test_ignored_calendars_are_skipped(self):
        self.google.calendars = [
            {"id": "primary", "summary": "Paul", "primary": True},
            {"id": "noise@group.calendar.google.com", "summary": "Football", "primary": False},
        ]
        await self.gcal.ignore_calendar("Football")
        await self.gcal.week(DUBAI)
        fetched = [u for m, u in self.google.requests if "/events?" in u or u.endswith("/events")]
        self.assertTrue(all("noise" not in u for u in fetched), fetched)


class TestWritesAndGuardrails(GcalBase):
    async def test_create_tags_the_event_and_logs(self):
        start = datetime(2026, 8, 8, 14, 0, tzinfo=DUBAI)
        reply = await self.gcal.create("Hour with Harry", start, 60, ["harry@x.com"])
        self.assertIn("Hour with Harry", reply)
        [created] = self.google.created
        self.assertEqual(created["extendedProperties"]["private"]["jarvis"], "1")
        self.assertEqual(created["attendees"], [{"email": "harry@x.com"}])
        row = await self.db.fetch_one("SELECT * FROM calendar_log")
        self.assertEqual(row["action"], "created")

    async def test_deleting_a_foreign_event_parks_a_confirmation(self):
        # The brief's guardrail test: not Jarvis-made → confirm, not deletion.
        self.google.events = [uk_event(title="Board meeting")]
        reply = await self.gcal.delete("board", DUBAI)
        self.assertIn("confirm calendar change", reply)
        self.assertEqual(self.google.deleted, [])            # nothing touched
        confirm = await self.gcal.confirm_pending(DUBAI)
        self.assertIn("off the calendar", confirm)
        self.assertEqual(self.google.deleted, ["ev1"])       # now it's real

    async def test_jarvis_made_events_move_without_ceremony(self):
        self.google.events = [uk_event(
            title="Gym block", extendedProperties={"private": {"jarvis": "1"}}
        )]
        new_start = datetime(2026, 8, 8, 16, 0, tzinfo=DUBAI)
        reply = await self.gcal.move("gym", new_start, DUBAI)
        self.assertIn("moved", reply)
        self.assertIn("ev1", self.google.patched)

    async def test_undo_reverses_a_create(self):
        start = datetime(2026, 8, 8, 14, 0, tzinfo=DUBAI)
        await self.gcal.create("Scratch block", start)
        reply = await self.gcal.undo_last(DUBAI)
        self.assertIn("Undone", reply)
        self.assertEqual(self.google.deleted, ["new1"])
        self.assertIsNone(await self.db.fetch_one("SELECT * FROM calendar_log"))

    async def test_changes_today_reads_the_trail(self):
        start = datetime(2026, 8, 8, 14, 0, tzinfo=DUBAI)
        await self.gcal.create("Block A", start)
        trail = await self.gcal.changes_today(DUBAI)
        self.assertIn("created 'Block A'", trail)

    async def test_expired_confirmation_declines_politely(self):
        await self.store.set("gcal_pending_write", json.dumps({
            "kind": "delete", "query": "x", "tz": "Asia/Dubai",
            "expires": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }))
        reply = await self.gcal.confirm_pending(DUBAI)
        self.assertIn("window closed", reply)
        self.assertEqual(self.google.deleted, [])


class TestEndpoints(unittest.IsolatedAsyncioTestCase):
    def test_connect_redirects_to_google_and_callback_stores_token(self):
        import app.main as app_main
        from app.config import Settings
        from fastapi.testclient import TestClient

        async def scaffold():
            db = SqliteDatabase(os.path.join(tmp.name, "t.db"))
            await db.init()
            return db

        tmp = tempfile.TemporaryDirectory()
        db = asyncio.run(scaffold())
        google = FakeGoogle()
        settings = Settings(
            telegram_bot_token="TOK", public_url="https://jarvis.example",
            google_oauth_client_id="CID", google_oauth_client_secret="CS", _env_file=None,
        )
        stub = type("Stub", (), {})()
        stub.settings = settings
        stub.store = SettingsStore(db)
        stub.gcal = GoogleCalendar(
            GoogleCalendarClient("CID", "CS", transport=httpx.MockTransport(google)),
            db, stub.store,
        )
        app_main.app.state.router = stub
        client = TestClient(app_main.app)
        try:
            secret = settings.effective_desktop_secret
            response = client.get(f"/google/connect/{secret}", follow_redirects=False)
            self.assertIn(response.status_code, (302, 307))
            self.assertIn("accounts.google.com", response.headers["location"])
            self.assertEqual(client.get("/google/connect/WRONG").status_code, 403)

            response = client.get(f"/google/callback?code=THECODE&state={secret}")
            self.assertEqual(response.status_code, 200)
            self.assertIn("connected", response.text)
            self.assertEqual(
                asyncio.run(stub.store.get(TOKEN_KEY)), "RT"
            )

            data = client.get(f"/desktop/{secret}/calendar?range=week").json()
            self.assertTrue(data["connected"])
        finally:
            del app_main.app.state.router
            asyncio.run(db.close())
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
