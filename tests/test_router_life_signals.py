"""Milestone 4 through the router: activity logging, hound me, timezone moves."""
import os
import tempfile
import unittest
from urllib.parse import parse_qs

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.telegram_client import TelegramClient
from app.config import Settings
from app.core.router import TIMEZONE_KEY, JarvisRouter
from app.heartbeat.jobs import HeartbeatJobs
from app.db.sqlite import SqliteDatabase
from tests.test_telegram_client import text_update

OWNER = 111


class LifeHarness:
    def __init__(self, db):
        self.telegram_calls = []
        self.rescheduled = False

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            self.telegram_calls.append(
                (request.url.path.split("/")[-1], request.content)
            )
            return httpx.Response(200, json={"ok": True, "result": {}})

        def claude_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Noted."}]})

        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        telegram = TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler))
        claude = ClaudeClient("K", transport=httpx.MockTransport(claude_handler))
        jobs = HeartbeatJobs(settings=settings, db=db, telegram=telegram, claude=claude)

        async def on_tz():
            self.rescheduled = True

        self.router = JarvisRouter(
            settings=settings,
            db=db,
            telegram=telegram,
            claude=claude,
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            heartbeat=jobs,
            on_timezone_change=on_tz,
        )
        self.jobs = jobs

    def texts(self):
        return [
            parse_qs(body.decode())["text"][0]
            for method, body in self.telegram_calls
            if method == "sendMessage"
        ]


class TestLifeSignals(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = LifeHarness(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_run_done_logs_streak_and_run(self):
        await self.h.router.handle_update(text_update("run done, felt great", OWNER))
        [ack] = self.h.texts()
        self.assertIn("Run streak: 1", ack)
        self.assertIn("Keystone", ack)
        row = await self.db.fetch_one("SELECT * FROM runs")
        self.assertEqual(row["source"], "told")

    async def test_multi_activity_report(self):
        await self.h.router.handle_update(
            text_update("did my run, smashed the workout, meals on plan", OWNER)
        )
        [ack] = self.h.texts()
        self.assertIn("Run streak: 1", ack)
        self.assertIn("Workout streak: 1", ack)
        self.assertIn("Meals on-plan streak: 1", ack)

    async def test_hound_me_activates(self):
        await self.h.router.handle_update(text_update("hound me today", OWNER))
        self.assertTrue(await self.h.jobs.hound_active())
        [reply] = self.h.texts()
        self.assertIn("Hound mode on", reply)

    async def test_timezone_move_to_dubai_and_back(self):
        await self.h.router.handle_update(text_update("just landed in Dubai", OWNER))
        self.assertEqual(await self.h.router.store.get(TIMEZONE_KEY), "Asia/Dubai")
        self.assertTrue(self.h.rescheduled)
        await self.h.router.handle_update(text_update("I'm in the UK this week", OWNER))
        self.assertEqual(await self.h.router.store.get(TIMEZONE_KEY), "Europe/London")

    async def test_ordinary_message_falls_through_to_brain(self):
        await self.h.router.handle_update(text_update("morning, how are we?", OWNER))
        # Claude replied (voice failed → text fallback) — a reply happened and
        # no streaks were touched
        row = await self.db.fetch_one("SELECT COUNT(*) AS n FROM runs")
        self.assertEqual(row["n"], 0)
        self.assertTrue(self.h.texts())


if __name__ == "__main__":
    unittest.main()
