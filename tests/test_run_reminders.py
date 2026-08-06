"""Run reminders OFF (Paul, 6 Aug: 'cancel the reminder for the run — I have
high blood pressure and need to work on that first'). Nothing chases, calls
or guilts about running; meds chasing is untouched; runs still log."""
import os
import tempfile
import unittest
from datetime import datetime

import httpx

from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.config import Settings
from app.core.router import JarvisRouter
from app.db.sqlite import SqliteDatabase
from app.heartbeat.gates import GateKeeper
from app.heartbeat.streaks import Streaks
from tests.test_telegram_client import text_update
from tests.test_wakeup import OWNER, TZ, FakePhone, Harness


class TestRunRemindersOff(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = Harness(self.db, phone=FakePhone())

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_the_flag_defaults_off(self):
        self.assertFalse(Settings(telegram_bot_token="TOK", _env_file=None).run_reminders_enabled)

    async def test_run_gate_leaves_the_chase_meds_stay(self):
        gates = GateKeeper(self.db, Streaks(self.db), run_reminders=False)
        ids = [g["id"] for g in await gates.config()]
        self.assertNotIn("run", ids)
        self.assertIn("meds", ids)
        outstanding = await gates.outstanding(datetime.now(TZ).replace(hour=11, minute=0))
        self.assertNotIn("run", [g["id"] for g in outstanding])

    async def test_run_gate_returns_when_reenabled(self):
        gates = GateKeeper(self.db, Streaks(self.db), run_reminders=True)
        self.assertIn("run", [g["id"] for g in await gates.config()])

    async def test_run_protect_call_is_silent(self):
        # Settings default: run_reminders_enabled=False → 06:30 job says nothing.
        await self.h.jobs.run_protect(datetime.now(TZ).replace(hour=6, minute=30))
        self.assertEqual(self.h.texts(), [])

    async def test_gate_chaser_chases_meds_only(self):
        self.h.jobs.gates = GateKeeper(self.db, Streaks(self.db), run_reminders=False)
        await self.h.jobs.gate_chaser(datetime.now(TZ).replace(hour=11, minute=15))
        texts = self.h.texts()
        self.assertTrue(any("supplements" in t for t in texts), texts)   # meds chased
        self.assertFalse(any("run" in t.lower() for t in texts), texts)  # run never named

    async def test_status_shows_the_switch(self):
        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        router = JarvisRouter(
            settings=settings,
            db=self.db,
            telegram=self.h.jobs.telegram,
            claude=self.h.jobs.claude,
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            heartbeat=self.h.jobs,
        )
        await router.handle_update(text_update("status", OWNER))
        report = next(t for t in self.h.texts() if "SYSTEM CHECK" in t)
        self.assertIn("Run reminders: OFF", report)


if __name__ == "__main__":
    unittest.main()
