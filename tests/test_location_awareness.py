"""GPS awareness + daily working memory (7 Aug brief): place classification,
the confirm-before-assume airport flow, geofenced tasks, and the daily
working memory. No real network — the maps client is tested against a
mocked httpx transport like every other client here."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

import httpx

from app.clients.maps_client import MapsClient, leave_now_message
from app.core.store import SettingsStore
from app.db.sqlite import SqliteDatabase
from app.location import geo
from app.location.service import PENDING_KEY, LocationAwareness

LONDON_HOME = (51.5074, -0.1278)
HEATHROW = (51.4700, -0.4543)
NOWHERE = (10.0, 10.0)


class FakeDaily12:
    def __init__(self, rows):
        self._rows = rows

    async def plan(self, plan_date):
        return self._rows


class FakeGcal:
    def __init__(self, events):
        self._events = events

    async def today(self, tz):
        return self._events


class FakeMemory:
    async def search(self, query, k=3, min_score=0.2):
        return [{"content": f"notes about {query}"}]


class FakeLiving:
    def __init__(self):
        self.set_calls = []

    async def set(self, key, value, room="you"):
        self.set_calls.append((key, value, room))


class FakePrivateTrack:
    def __init__(self, message="Travel day — I'm with you."):
        self.message = message
        self.calls = []

    async def trigger_message(self, flags, today):
        self.calls.append((flags, today))
        return self.message


def event(title, location="", start=None, attendees=None):
    return {
        "title": title, "location": location, "attendees": attendees or [],
        "start": start or datetime.now(timezone.utc), "join_url": "",
    }


class TestGeo(unittest.TestCase):
    def test_haversine_zero_for_same_point(self):
        self.assertEqual(geo.haversine_m(51.5, -0.1, 51.5, -0.1), 0.0)

    def test_haversine_roughly_right_for_a_known_distance(self):
        # London to Paris is ~344km.
        d = geo.haversine_m(51.5074, -0.1278, 48.8566, 2.3522)
        self.assertTrue(330_000 < d < 360_000)

    def test_named_place_wins_over_airport_seed(self):
        named = [{"name": "My Heathrow Office", "kind": "office", "lat": HEATHROW[0], "lon": HEATHROW[1], "radius_m": 5000}]
        result = geo.classify(HEATHROW[0], HEATHROW[1], named)
        self.assertEqual(result["name"], "My Heathrow Office")

    def test_airport_seed_fires_without_a_named_place(self):
        result = geo.classify(HEATHROW[0], HEATHROW[1], [])
        self.assertEqual(result["kind"], "airport")

    def test_unknown_place_is_honest_not_invented(self):
        result = geo.classify(*NOWHERE, [])
        self.assertEqual(result["kind"], "unknown")
        self.assertEqual(result["name"], "")


class LocationBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.store = SettingsStore(self.db)
        self.sent = []

        async def send_text(text, essential=False):
            self.sent.append((text, essential))

        self._send_text = send_text
        self.living = FakeLiving()
        self.private_track = FakePrivateTrack()
        self.daily12 = FakeDaily12([])
        self.gcal = FakeGcal([])
        self.memory = FakeMemory()
        self.loc = LocationAwareness(
            self.db, self.store, self.living, self._send_text,
            gcal=self.gcal, daily12=self.daily12, memory=self.memory,
            private_track=self.private_track,
        )
        await self.store.set("current_timezone", "Europe/London")

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def _at_wake_hour(self):
        # Force a wake-hours-safe local time regardless of the container clock
        # by stubbing _within_wake_hours to True for tests that don't care.
        self.loc._within_wake_hours = lambda tz_name: True


class TestNamedPlaces(LocationBase):
    async def test_teach_list_forget(self):
        msg = await self.loc.teach_place("Home", "home", *LONDON_HOME)
        self.assertIn("Home", msg)
        places = await self.loc.list_places()
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["name"], "Home")

        # Re-teaching moves it rather than duplicating.
        await self.loc.teach_place("Home", "home", *HEATHROW)
        places = await self.loc.list_places()
        self.assertEqual(len(places), 1)

        msg = await self.loc.forget_place("Home")
        self.assertIn("Forgotten", msg)
        self.assertEqual(await self.loc.list_places(), [])


class TestRecordFix(LocationBase):
    async def test_history_written_and_classified(self):
        await self._at_wake_hour()
        await self.loc.teach_place("Home", "home", *LONDON_HOME)
        result = await self.loc.record_fix(*LONDON_HOME)
        self.assertEqual(result["place"], "Home")
        self.assertTrue(result["arrived"])
        rows = await self.db.fetch_all("SELECT * FROM location_history")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["place_name"], "Home")

    async def test_staying_put_is_not_a_repeat_arrival(self):
        await self._at_wake_hour()
        await self.loc.teach_place("Home", "home", *LONDON_HOME)
        first = await self.loc.record_fix(*LONDON_HOME)
        second = await self.loc.record_fix(*LONDON_HOME)
        self.assertTrue(first["arrived"])
        self.assertFalse(second["arrived"])

    async def test_outside_wake_hours_records_but_suppresses_nudges(self):
        self.loc._within_wake_hours = lambda tz_name: False
        await self.loc.teach_place("Home", "home", *LONDON_HOME)
        await self.loc.record_fix(*LONDON_HOME)
        rows = await self.db.fetch_all("SELECT * FROM location_history")
        self.assertEqual(len(rows), 1)   # still recorded
        self.assertEqual(self.sent, [])   # but nothing proactive fired


class TestAirportConfirm(LocationBase):
    async def test_arrival_asks_once_and_sets_pending(self):
        await self._at_wake_hour()
        await self.loc.record_fix(*HEATHROW)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("flying today", self.sent[0][0])
        pending = await self.store.get(PENDING_KEY, "")
        self.assertTrue(pending)

    async def test_second_fix_at_same_airport_does_not_ask_twice(self):
        await self._at_wake_hour()
        await self.loc.record_fix(*HEATHROW)
        await self.loc.record_fix(*HEATHROW)   # still there, no new arrival
        self.assertEqual(len(self.sent), 1)

    async def test_pending_note_appears_and_expires(self):
        await self._at_wake_hour()
        await self.loc.record_fix(*HEATHROW)
        note = await self.loc.pending_situation_note()
        self.assertIn("GPS ARRIVAL", note)
        self.assertIn("Heathrow", note)

        # Manually backdate it past the TTL.
        raw = json.loads(await self.store.get(PENDING_KEY))
        raw["since"] = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        await self.store.set(PENDING_KEY, json.dumps(raw))
        self.assertEqual(await self.loc.pending_situation_note(), "")

    async def test_confirm_travel_true_sets_travel_day_and_triggers_sobriety(self):
        await self._at_wake_hour()
        await self.loc.record_fix(*HEATHROW)
        msg = await self.loc.confirm_travel(True)
        self.assertIn("travel day", msg.lower())
        today = datetime.now(timezone.utc).date()
        self.assertTrue(await self.loc.is_travel_day(today))
        self.assertEqual(self.living.set_calls[0][0], "today.travel_day")
        self.assertEqual(len(self.private_track.calls), 1)
        # The sobriety heads-up itself was sent as its own message.
        self.assertTrue(any("Travel day" in t for t, _ in self.sent))
        # Pending cleared — no more nagging.
        self.assertEqual(await self.store.get(PENDING_KEY, ""), "")

    async def test_confirm_travel_false_just_clears(self):
        await self._at_wake_hour()
        await self.loc.record_fix(*HEATHROW)
        msg = await self.loc.confirm_travel(False)
        self.assertIn("Noted", msg)
        today = datetime.now(timezone.utc).date()
        self.assertFalse(await self.loc.is_travel_day(today))
        self.assertEqual(self.private_track.calls, [])


class TestGeofenceTasks(LocationBase):
    async def test_cannot_tag_an_untaught_place(self):
        msg = await self.loc.add_geofence_task("Warehouse", "check Harry's stock")
        self.assertIn("don't have", msg)
        self.assertIn("taught", msg.lower())

    async def test_fires_on_arrival_and_recurs(self):
        await self._at_wake_hour()
        await self.loc.teach_place("Warehouse", "warehouse", *LONDON_HOME)
        await self.loc.add_geofence_task("Warehouse", "check Harry's stock")

        await self.loc.record_fix(*LONDON_HOME)
        self.assertTrue(any("check Harry's stock" in t for t, _ in self.sent))

        # Leave (somewhere that isn't the curated airport seed — this test
        # is about geofence recurrence, not airport confirmation) and come
        # back — recurring, fires again.
        self.sent.clear()
        await self.loc.record_fix(*NOWHERE)
        await self.loc.record_fix(*LONDON_HOME)
        self.assertTrue(any("check Harry's stock" in t for t, _ in self.sent))

    async def test_removed_task_stops_firing(self):
        await self._at_wake_hour()
        await self.loc.teach_place("Warehouse", "warehouse", *LONDON_HOME)
        await self.loc.add_geofence_task("Warehouse", "check Harry's stock")
        await self.loc.remove_geofence_task("Warehouse", "Harry's stock")
        self.sent.clear()
        await self.loc.record_fix(*NOWHERE)
        await self.loc.record_fix(*LONDON_HOME)
        self.assertEqual(self.sent, [])


class TestDailyWorkingMemory(LocationBase):
    async def test_build_assembles_focus_events_meds_and_resources(self):
        self.daily12._rows = [
            {"position": 1, "done": 0, "company_slug": "prodermis", "task_id": 1,
             "title": "Send Harry the docs", "trello_id": "c1", "board_id": "b1",
             "due_date": "", "url": "https://trello.com/c/c1", "score": 1.0},
        ]
        self.gcal._events = [event("Call with BMI", location="Zoom")]
        memory = await self.loc.build_daily_memory(date(2026, 8, 7))
        self.assertEqual(len(memory["focus"]), 1)
        self.assertEqual(memory["focus"][0]["title"], "Send Harry the docs")
        self.assertEqual(len(memory["events"]), 1)
        self.assertIn("notes about Call with BMI", memory["events"][0]["resources"][0])
        self.assertTrue(memory["meds"])

    async def test_get_daily_memory_rebuilds_on_date_change(self):
        await self.loc.build_daily_memory(date(2026, 8, 6))
        self.assertEqual(await self.store.get("daily_working_memory_date"), "2026-08-06")
        memory = await self.loc.get_daily_memory()   # today != stored date → rebuild
        self.assertNotEqual(memory["date"], "2026-08-06")

    async def test_surfaces_event_once_on_arrival_at_its_location(self):
        await self._at_wake_hour()
        await self.loc.teach_place("Warehouse", "warehouse", *LONDON_HOME)
        self.gcal._events = [event("Stock check with Harry", location="Warehouse")]
        today = datetime.now(timezone.utc).date()
        await self.loc.build_daily_memory(today)

        await self.loc.record_fix(*LONDON_HOME)
        self.assertTrue(any("Stock check with Harry" in t for t, _ in self.sent))

        self.sent.clear()
        await self.loc.record_fix(*NOWHERE)
        await self.loc.record_fix(*LONDON_HOME)
        self.assertFalse(any("Stock check with Harry" in t for t, _ in self.sent))   # not twice


class TestMapsClient(unittest.IsolatedAsyncioTestCase):
    async def test_not_configured_without_key(self):
        client = MapsClient("")
        self.assertFalse(client.configured)
        client2 = MapsClient("KEY")
        self.assertTrue(client2.configured)

    async def test_eta_with_traffic_parses_the_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "status": "OK",
                "routes": [{"summary": "M4", "legs": [{
                    "duration": {"value": 1800},
                    "duration_in_traffic": {"value": 2400},
                }]}],
            })
        client = MapsClient("KEY", transport=httpx.MockTransport(handler))
        eta = await client.eta_with_traffic(51.5, -0.1, "Heathrow Airport")
        self.assertEqual(eta["duration_min"], 30)
        self.assertEqual(eta["duration_in_traffic_min"], 40)

    async def test_leave_now_message_uses_buffer(self):
        from datetime import timezone as _tz

        # Wide margin (not an exact minute string) — the function's own
        # datetime.now() call lands a few hundred ms after `start` is
        # computed, so an exact '20 min' assertion is a timing flake.
        start = (datetime.now(_tz.utc) + timedelta(minutes=60)).isoformat()
        msg = leave_now_message("BMI call", {"duration_in_traffic_min": 30}, start, buffer_min=10)
        self.assertIn("Leave in", msg)
        self.assertIn("min", msg)
        self.assertNotIn("Leave now", msg)

    async def test_leave_now_message_says_leave_now_when_tight(self):
        from datetime import timezone as _tz

        start = (datetime.now(_tz.utc) + timedelta(minutes=15)).isoformat()
        msg = leave_now_message("BMI call", {"duration_in_traffic_min": 30}, start, buffer_min=10)
        self.assertIn("Leave now", msg)


class TestRouterToolWiring(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx as _httpx

        from app.clients.anthropic_client import ClaudeClient
        from app.clients.deepgram_client import DeepgramClient
        from app.clients.elevenlabs_client import ElevenLabsClient
        from app.config import Settings
        from app.core.router import JarvisRouter
        from tests.test_wakeup import OWNER, FakePhone, Harness

        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = Harness(self.db, phone=FakePhone())
        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.router = JarvisRouter(
            settings=settings, db=self.db,
            telegram=self.h.jobs.telegram,
            claude=ClaudeClient("K", transport=_httpx.MockTransport(lambda r: _httpx.Response(500))),
            deepgram=DeepgramClient("K", transport=_httpx.MockTransport(lambda r: _httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=_httpx.MockTransport(lambda r: _httpx.Response(500))),
            heartbeat=self.h.jobs,
        )
        store = SettingsStore(self.db)

        async def send_text(text, essential=False):
            pass

        self.router.location = LocationAwareness(self.db, store, None, send_text)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_teach_place_anchors_to_latest_fix_not_invented_coords(self):
        from app.core.router import IncomingMessage

        msg = IncomingMessage(chat_id=1, message_id=1, from_name="Paul")
        no_fix = await self.router._dispatch_tool("location", {"action": "teach_place", "name": "Home"}, msg)
        self.assertIn("no GPS fix", no_fix)

        await self.db.execute(
            "INSERT INTO location_history (ts, lat, lon, place_name, place_kind, tz)"
            " VALUES ('2026-08-07T10:00:00+00:00', 51.5, -0.1, '', 'unknown', 'Europe/London')"
        )
        ok = await self.router._dispatch_tool(
            "location", {"action": "teach_place", "name": "Home", "kind": "home"}, msg
        )
        self.assertIn("Home", ok)
        places = await self.router.location.list_places()
        self.assertEqual(places[0]["name"], "Home")
        self.assertEqual(places[0]["kind"], "home")


if __name__ == "__main__":
    unittest.main()
