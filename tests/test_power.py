"""The master switch (6 Aug): 'Jarvis off' deactivates him completely — no
replies, no calls, no reminders, no jobs — and 'Jarvis on' brings him back."""
import json
import os
import tempfile
import unittest
from datetime import datetime

import httpx

from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.config import Settings
from app.core.power import ON_ACK, POWER_KEY, POWER_OFF_RE, POWER_ON_RE
from app.core.router import JarvisRouter
from app.db.sqlite import SqliteDatabase
from app.heartbeat.scheduler import Heartbeat
from app.heartbeat.wakeup import STATE_KEY
from tests.test_telegram_client import text_update
from tests.test_wakeup import OWNER, TZ, FakePhone, Harness


class TestSwitchRegexes(unittest.TestCase):
    def test_off_phrases_match_whole_message_only(self):
        for phrase in (
            "jarvis off", "Jarvis, off", "off", "shut down", "shutdown",
            "deactivate", "Jarvis deactivate", "power off", "power down",
            "switch off", "turn yourself off", "go offline",
            "jarvis shut down completely please", "deactive jarvis",
        ):
            self.assertIsNotNone(POWER_OFF_RE.match(phrase), phrase)

    def test_ordinary_off_talk_never_trips_it(self):
        for phrase in (
            "turn off the water reminders",
            "can we switch off the digest today",
            "the power is off at the villa",
            "I turned off the aircon",
            "jarvis turn off the morning brief",
        ):
            self.assertIsNone(POWER_OFF_RE.match(phrase), phrase)

    def test_on_phrases_wake_him(self):
        for phrase in (
            "jarvis on", "Jarvis, on", "on", "power on", "power up",
            "switch back on", "turn on", "activate", "reactivate",
            "wake up", "come back", "back online", "jarvis are you back on",
        ):
            self.assertIsNotNone(POWER_ON_RE.search(phrase), phrase)

    def test_casual_words_stay_asleep(self):
        for phrase in ("hang on", "later on that day", "I'm on it", "carry on then"):
            self.assertIsNone(POWER_ON_RE.search(phrase), phrase)


class PowerBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.phone = FakePhone()
        self.h = Harness(self.db, phone=self.phone)
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


class TestMasterSwitch(PowerBase):
    async def test_jarvis_off_powers_down_and_says_how_to_return(self):
        await self.router.handle_update(text_update("jarvis off", OWNER))
        self.assertEqual(await self.h.jobs.store.get(POWER_KEY), "off")
        ack = self.h.texts()[-1]
        self.assertIn("Jarvis on", ack)

    async def test_while_off_everything_is_silence(self):
        await self.h.jobs.store.set(POWER_KEY, "off")
        await self.router.handle_update(text_update("status", OWNER))
        await self.router.handle_update(text_update("what's on the board today", OWNER))
        self.assertEqual(self.h.texts(), [])

    async def test_strangers_get_silence_too(self):
        await self.h.jobs.store.set(POWER_KEY, "off")
        await self.router.handle_update(text_update("hello?", 999888))
        self.assertEqual(self.h.texts(), [])

    async def test_jarvis_on_brings_him_back(self):
        await self.h.jobs.store.set(POWER_KEY, "off")
        await self.router.handle_update(text_update("jarvis on", OWNER))
        self.assertEqual(await self.h.jobs.store.get(POWER_KEY), "")
        self.assertIn(ON_ACK, self.h.texts())
        # And he's genuinely alive again — a deterministic lane answers.
        await self.router.handle_update(text_update("set wake 05:00", OWNER))
        self.assertTrue(any("Locked" in t for t in self.h.texts()))

    async def test_off_talk_about_other_things_stays_live(self):
        await self.router.handle_update(text_update("turn off the water reminders", OWNER))
        self.assertNotEqual(await self.h.jobs.store.get(POWER_KEY), "off")

    async def test_power_off_stands_down_a_live_wake_sequence(self):
        now = datetime.now(TZ)
        await self.h.jobs.store.set(STATE_KEY, json.dumps({
            "date": now.date().isoformat(), "phase": "up", "test": False,
            "started": now.isoformat(timespec="seconds"),
            "last_action": now.isoformat(timespec="seconds"),
            "calls": 1, "silences": 0, "pitch_i": 0,
        }))
        await self.router.handle_update(text_update("jarvis off", OWNER))
        state = json.loads(await self.h.jobs.store.get(STATE_KEY))
        self.assertEqual(state["phase"], "stood_down")

    async def test_heartbeat_funnel_blocks_even_essential_sends(self):
        await self.h.jobs.store.set(POWER_KEY, "off")
        await self.h.jobs._send_text("💊 Meds time", essential=True)
        await self.h.jobs._send_voice("Morning brief", essential=True)
        self.assertEqual(self.h.texts(), [])

    async def test_scheduler_wrapper_gates_every_job(self):
        ran = []

        async def job():
            ran.append(1)

        wrapped = Heartbeat(self.h.jobs)._powered(job)
        await self.h.jobs.store.set(POWER_KEY, "off")
        await wrapped()
        self.assertEqual(ran, [])
        await self.h.jobs.store.set(POWER_KEY, "")
        await wrapped()
        self.assertEqual(ran, [1])

    async def test_inbound_phone_call_gets_one_line_and_a_hangup(self):
        await self.h.jobs.store.set(POWER_KEY, "off")
        reply = await self.router.phone_turn("hello jarvis you there")
        self.assertTrue(reply.startswith("[[bye]]"))
        self.assertIn("Jarvis on", reply)


if __name__ == "__main__":
    unittest.main()
