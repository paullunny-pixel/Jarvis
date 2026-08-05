"""Timed reminder calls (5 Aug): 'call me in 40 minutes and remind me to call
Ella' — the brain schedules through its tool, the minute-beat rings on time."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.telegram_client import IncomingMessage, TelegramClient
from app.config import Settings
from app.core.router import JarvisRouter
from app.db.sqlite import SqliteDatabase
from tests.test_wakeup import OWNER, FakePhone, Harness

TZ = ZoneInfo("Europe/London")


class TestScheduledCalls(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.phone = FakePhone()
        self.h = Harness(self.db, phone=self.phone)
        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        mock = httpx.MockTransport(lambda r: httpx.Response(500))
        self.router = JarvisRouter(
            settings=settings, db=self.db,
            telegram=self.h.jobs.telegram,
            claude=ClaudeClient("K", transport=mock),
            deepgram=DeepgramClient("K", transport=mock),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=mock),
            heartbeat=self.h.jobs,
        )
        self.router.phone_channel = self.phone
        self.msg = IncomingMessage(chat_id=OWNER, message_id=1, from_name="Paul")

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def _dispatch(self, tool_input):
        return await self.router._dispatch_tool("schedule_call", tool_input, self.msg)

    async def test_minutes_from_now_locks_a_call(self):
        result = await self._dispatch({"minutes_from_now": 40, "reminder": "Call Ella."})
        self.assertIn("Locked", result)
        self.assertIn("Call Ella.", result)
        items = json.loads(await self.h.jobs.store.get("scheduled_calls"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["message"], "Call Ella.")

    async def test_past_clock_time_rolls_to_tomorrow(self):
        result = await self._dispatch({"at": "00:01", "reminder": "Early one."})
        self.assertIn("Locked", result)
        [item] = json.loads(await self.h.jobs.store.get("scheduled_calls"))
        self.assertGreater(datetime.fromisoformat(item["due"]), datetime.now(TZ))

    async def test_bad_input_fails_honestly(self):
        self.assertIn("SCHEDULE FAILED", await self._dispatch({"reminder": "no time given"}))

    async def test_no_phone_line_fails_honestly(self):
        self.router.phone_channel = None
        self.assertIn("no phone line", await self._dispatch({"minutes_from_now": 10}))

    async def test_list_and_cancel(self):
        await self._dispatch({"minutes_from_now": 30, "reminder": "Call Ella."})
        listing = await self._dispatch({"list": True})
        self.assertIn("Call Ella.", listing)
        self.assertIn("1 scheduled call(s) cancelled", await self._dispatch({"cancel": True}))
        self.assertEqual(await self._dispatch({"list": True}), "No calls scheduled.")

    async def test_tick_rings_on_time_and_clears(self):
        now = datetime.now(TZ)
        await self.h.jobs.store.set("scheduled_calls", json.dumps([
            {"due": (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
             "message": "Call Ella."},
            {"due": (now + timedelta(hours=2)).isoformat(timespec="seconds"),
             "message": "Later one."},
        ]))
        await self.h.jobs.scheduled_calls_tick(now)
        self.assertEqual(len(self.phone.calls), 1)
        self.assertIn("Call Ella.", self.phone.calls[0])
        remaining = json.loads(await self.h.jobs.store.get("scheduled_calls"))
        self.assertEqual([i["message"] for i in remaining], ["Later one."])
        self.assertTrue(any("Ringing you now" in t for t in self.h.texts()))

    async def test_domain_ruling_stores_and_lists(self):
        result = await self.router._dispatch_tool(
            "domain_ruling", {"alias": "BMI", "domain": "Prodermis"}, self.msg
        )
        self.assertIn("Ruled", result)
        listing = await self.router._dispatch_tool("domain_ruling", {"list": True}, self.msg)
        self.assertIn("bmi → Prodermis", listing)
        bad = await self.router._dispatch_tool(
            "domain_ruling", {"alias": "LPG", "domain": "NotADomain"}, self.msg
        )
        self.assertIn("RULING FAILED", bad)

    async def test_failed_call_is_reported_honestly(self):
        now = datetime.now(TZ)
        self.phone.configured = False
        await self.h.jobs.store.set("scheduled_calls", json.dumps([
            {"due": (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
             "message": "Call Ella."},
        ]))
        await self.h.jobs.scheduled_calls_tick(now)
        self.assertTrue(any("call failed" in t for t in self.h.texts()))
        self.assertEqual(json.loads(await self.h.jobs.store.get("scheduled_calls")), [])
