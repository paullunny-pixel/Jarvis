"""Regression tests for the 'I have NOT done my run' bug: activity logging must
be negation-aware, model-confirmed, and correctable."""
import json
import os
import tempfile
import unittest
from datetime import date
from urllib.parse import parse_qs

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.telegram_client import TelegramClient
from app.config import Settings
from app.core.router import JarvisRouter
from app.heartbeat.streaks import Streaks, detect_activities, looks_negated
from app.db.sqlite import SqliteDatabase
from tests.test_telegram_client import text_update

OWNER = 111


class NegationHarness:
    def __init__(self, db, verdicts=None, haiku_fails=False):
        self.telegram_calls = []
        self.brain_called = False

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            self.telegram_calls.append((request.url.path.split("/")[-1], request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})

        def claude_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if "haiku" in payload["model"]:
                if haiku_fails:
                    return httpx.Response(500, text="down")
                return httpx.Response(
                    200,
                    json={"content": [{"type": "text", "text": json.dumps(
                        verdicts or {"run": "na", "workout": "na", "meals": "na"}
                    )}]},
                )
            self.brain_called = True
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Shoes on. Go."}]})

        self.router = JarvisRouter(
            settings=Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None),
            db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler)),
            claude=ClaudeClient("K", transport=httpx.MockTransport(claude_handler)),
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        )

    def texts(self):
        return [
            parse_qs(body.decode())["text"][0]
            for method, body in self.telegram_calls
            if method == "sendMessage"
        ]


class TestNegationDetection(unittest.TestCase):
    def test_negated_phrases_flagged(self):
        for phrase in (
            "I have not done my run",
            "haven't done my run yet",
            "didn't do the run today",
            "missed my run this morning",
            "I'll get the run done later",
            "still need to do my run",
            "gonna do my run in a bit",
        ):
            self.assertTrue(looks_negated(phrase), phrase)

    def test_positive_phrases_not_flagged(self):
        for phrase in ("run done, felt strong", "just ran the 5k", "smashed the workout"):
            self.assertFalse(looks_negated(phrase), phrase)

    def test_the_original_bug_phrase_still_nominates_a_candidate(self):
        # detection still fires (so the confirmer gets a say) but must not log
        self.assertEqual(detect_activities("I have not done my run"), ["run"])


class TestNegationFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_not_done_run_is_never_logged_and_coach_responds(self):
        h = NegationHarness(self.db, verdicts={"run": "not_done", "workout": "na", "meals": "na"})
        await h.router.handle_update(text_update("I have not done my run today", OWNER))
        rows = await self.db.fetch_all("SELECT * FROM runs")
        self.assertEqual(rows, [])
        streak = await self.db.fetch_one("SELECT * FROM streaks WHERE type = 'run'")
        self.assertIsNone(streak)
        self.assertTrue(h.brain_called)  # Jarvis coached instead of logging
        self.assertTrue(any("Shoes on" in t for t in h.texts()))

    async def test_confirmed_done_still_logs(self):
        h = NegationHarness(self.db, verdicts={"run": "done", "workout": "na", "meals": "na"})
        await h.router.handle_update(text_update("run done, felt great", OWNER))
        self.assertTrue(any("Run streak: 1" in t for t in h.texts()))
        rows = await self.db.fetch_all("SELECT * FROM runs")
        self.assertEqual(len(rows), 1)

    async def test_correction_unlogs_todays_wrong_run(self):
        # First a wrong log (as happened in production)...
        today = None
        h1 = NegationHarness(self.db, verdicts={"run": "done", "workout": "na", "meals": "na"})
        await h1.router.handle_update(text_update("done my run", OWNER))
        # ...then Paul corrects it.
        h2 = NegationHarness(self.db, verdicts={"run": "not_done", "workout": "na", "meals": "na"})
        await h2.router.handle_update(text_update("no I have NOT done my run", OWNER))
        self.assertTrue(any("unlogged" in t for t in h2.texts()))
        streak = await self.db.fetch_one("SELECT * FROM streaks WHERE type = 'run'")
        self.assertEqual(streak["current_count"], 0)
        self.assertEqual(streak["best_count"], 0)
        rows = await self.db.fetch_all("SELECT * FROM runs")
        self.assertEqual(rows, [])
        self.assertTrue(h2.brain_called)  # and the coach still answers the moment

    async def test_haiku_down_negated_message_does_not_log(self):
        h = NegationHarness(self.db, haiku_fails=True)
        await h.router.handle_update(text_update("haven't done my run yet", OWNER))
        self.assertIsNone(await self.db.fetch_one("SELECT * FROM streaks WHERE type = 'run'"))
        self.assertTrue(h.brain_called)

    async def test_haiku_down_clean_positive_still_logs(self):
        h = NegationHarness(self.db, haiku_fails=True)
        await h.router.handle_update(text_update("run done", OWNER))
        self.assertTrue(any("Run streak: 1" in t for t in h.texts()))


class TestUnrecord(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.streaks = Streaks(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_unrecord_mid_streak_preserves_chain(self):
        for d in (23, 24, 25):
            await self.streaks.record("run", date(2026, 7, d))
        self.assertTrue(await self.streaks.unrecord("run", date(2026, 7, 25)))
        snap = await self.streaks.snapshot(date(2026, 7, 25))
        self.assertEqual(snap["run"]["current"], 2)
        self.assertFalse(snap["run"]["done_today"])
        # re-recording today continues the chain correctly
        result = await self.streaks.record("run", date(2026, 7, 25))
        self.assertEqual(result["current"], 3)

    async def test_unrecord_wrong_day_is_noop(self):
        await self.streaks.record("run", date(2026, 7, 24))
        self.assertFalse(await self.streaks.unrecord("run", date(2026, 7, 25)))


if __name__ == "__main__":
    unittest.main()
