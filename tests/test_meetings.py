"""Meetings Layer A (6 Aug): 'start a new Zoom meeting' → both links on
Telegram; scheduled variants parse Paul's words; honest lines when the keys
aren't in Render or Zoom refuses."""
import json
import os
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.zoom_client import ZoomClient, ZoomError
from app.config import Settings
from app.core.router import JarvisRouter
from app.db.sqlite import SqliteDatabase
from app.meetings import MeetingMaker, mentions_zoom, parse_when, topic_from
from tests.test_telegram_client import text_update
from tests.test_wakeup import OWNER, FakePhone, Harness

TZ = ZoneInfo("Asia/Dubai")
NOW = datetime(2026, 8, 6, 17, 30, tzinfo=TZ)   # a Thursday evening


def zoom_transport(captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth/token" in str(request.url):
            captured["token_calls"] = captured.get("token_calls", 0) + 1
            return httpx.Response(200, json={"access_token": "TOK123", "expires_in": 3600})
        captured["create"] = json.loads(request.content)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(201, json={
            "id": 987, "topic": captured["create"]["topic"],
            "join_url": "https://zoom.us/j/987", "start_url": "https://zoom.us/s/987?zak=abc",
            "password": "villa1",
        })

    return httpx.MockTransport(handler)


class TestZoomClient(unittest.IsolatedAsyncioTestCase):
    async def test_instant_meeting_join_before_host(self):
        captured = {}
        zoom = ZoomClient("ACC", "ID", "SECRET", transport=zoom_transport(captured))
        meeting = await zoom.create_meeting(topic="Paul's meeting")
        self.assertEqual(meeting["join_url"], "https://zoom.us/j/987")
        self.assertEqual(meeting["start_url"], "https://zoom.us/s/987?zak=abc")
        self.assertEqual(captured["create"]["type"], 1)
        self.assertTrue(captured["create"]["settings"]["join_before_host"])
        self.assertEqual(captured["auth"], "Bearer TOK123")

    async def test_scheduled_meeting_carries_time_and_tz(self):
        captured = {}
        zoom = ZoomClient("ACC", "ID", "SECRET", transport=zoom_transport(captured))
        await zoom.create_meeting(topic="Villa", start_iso="2026-08-07T14:00:00", tz_name="Asia/Dubai")
        self.assertEqual(captured["create"]["type"], 2)
        self.assertEqual(captured["create"]["start_time"], "2026-08-07T14:00:00")
        self.assertEqual(captured["create"]["timezone"], "Asia/Dubai")

    async def test_token_is_cached(self):
        captured = {}
        zoom = ZoomClient("ACC", "ID", "SECRET", transport=zoom_transport(captured))
        await zoom.create_meeting()
        await zoom.create_meeting()
        self.assertEqual(captured["token_calls"], 1)

    async def test_refusal_raises_honestly(self):
        zoom = ZoomClient("ACC", "ID", "SECRET", transport=httpx.MockTransport(
            lambda r: httpx.Response(400, text="bad")
        ))
        with self.assertRaises(ZoomError):
            await zoom.create_meeting()


class TestWhenParser(unittest.TestCase):
    def test_no_schedule_words_means_instant(self):
        self.assertIsNone(parse_when("start a new zoom meeting", NOW))

    def test_tomorrow_at_two_is_two_pm(self):
        when = parse_when("set up a zoom for tomorrow at 2", NOW)
        self.assertEqual((when.day, when.hour, when.minute), (7, 14, 0))

    def test_am_pm_and_minutes_respected(self):
        self.assertEqual(parse_when("zoom tomorrow at 9am", NOW).hour, 9)
        self.assertEqual(parse_when("zoom tomorrow at 4.30pm", NOW).hour, 16)
        self.assertEqual(parse_when("zoom tomorrow at 4.30pm", NOW).minute, 30)

    def test_weekday_lands_on_next_occurrence(self):
        when = parse_when("book a zoom for Friday at 10am", NOW)
        self.assertEqual(when.weekday(), 4)
        self.assertGreater(when, NOW)

    def test_bare_time_already_passed_rolls_to_tomorrow(self):
        when = parse_when("set up a zoom at 2", NOW)   # said at 17:30
        self.assertEqual((when.day, when.hour), (7, 14))

    def test_gate_matches_zoom_asks_only(self):
        self.assertTrue(mentions_zoom("start a new zoom meeting"))
        self.assertTrue(mentions_zoom("can you set up a Zoom for tomorrow at 2"))
        self.assertFalse(mentions_zoom("the zoom on that photo is terrible"))
        self.assertFalse(mentions_zoom("what's on the board today"))

    def test_topic_only_when_stated(self):
        self.assertEqual(topic_from("zoom about the villa handover"), "The Villa Handover")
        self.assertEqual(topic_from("start a new zoom meeting"), "Paul's meeting")


class TestRouterLane(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = Harness(self.db, phone=FakePhone())
        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.router = JarvisRouter(
            settings=settings,
            db=self.db,
            telegram=self.h.jobs.telegram,
            claude=self.h.jobs.claude,
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            heartbeat=self.h.jobs,
        )

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_unwired_zoom_answers_honestly(self):
        await self.router.handle_update(text_update("start a new zoom meeting", OWNER))
        [reply] = self.h.texts()
        self.assertIn("ZOOM_ACCOUNT_ID", reply)

    async def test_wired_zoom_hands_over_both_links(self):
        captured = {}
        self.router.meetings = MeetingMaker(
            ZoomClient("ACC", "ID", "SECRET", transport=zoom_transport(captured))
        )
        await self.router.handle_update(text_update("start a new zoom meeting", OWNER))
        [reply] = self.h.texts()
        self.assertIn("https://zoom.us/s/987?zak=abc", reply)   # host link
        self.assertIn("https://zoom.us/j/987", reply)           # join link
        self.assertIn("villa1", reply)                          # passcode
        self.assertEqual(captured["create"]["type"], 1)

    async def test_zoom_refusal_is_an_honest_failure(self):
        self.router.meetings = MeetingMaker(
            ZoomClient("ACC", "ID", "SECRET", transport=httpx.MockTransport(
                lambda r: httpx.Response(500, text="down")
            ))
        )
        await self.router.handle_update(text_update("start a new zoom meeting", OWNER))
        [reply] = self.h.texts()
        self.assertIn("did NOT get created", reply)

    async def test_status_shows_the_zoom_wire(self):
        await self.router.handle_update(text_update("status", OWNER))
        report = next(t for t in self.h.texts() if "SYSTEM CHECK" in t)
        self.assertIn("Zoom: not wired", report)


if __name__ == "__main__":
    unittest.main()
