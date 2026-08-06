"""Brazil Portuguese course (7 Aug brief, ⏳ 28 Aug): the coach's ledger,
the deterministic scoring, and the voice-led lesson lane through the router."""
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
from app.db.sqlite import SqliteDatabase
from app.lessons.portuguese import (
    EDNEI_SPEECH,
    SURVIVAL_SET,
    PortugueseCoach,
    score_attempt,
)
from tests.test_telegram_client import text_update

OWNER = 111
TODAY = date(2026, 8, 7)


class TestScoring(unittest.TestCase):
    def test_perfect_match_scores_100(self):
        self.assertEqual(score_attempt("Eu amo muito a sua filha.", "eu amo muito a sua filha"), 100)

    def test_accents_and_punctuation_forgiven(self):
        # STT is inconsistent with accents — the ledger must not punish that.
        self.assertEqual(score_attempt("Que delícia!", "que delicia"), 100)

    def test_empty_attempt_is_zero(self):
        self.assertEqual(score_attempt("Oi, tudo bem?", ""), 0)

    def test_close_attempt_passes_garbage_fails(self):
        close = score_attempt(
            "Eu amo muito a sua filha.", "eu amo a sua filha"
        )
        self.assertGreaterEqual(close, 70)
        garbage = score_attempt("Eu amo muito a sua filha.", "hello there mate")
        self.assertLess(garbage, 40)


class TestCoach(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.coach = PortugueseCoach(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_seed_is_idempotent(self):
        await self.coach.ensure_seeded()
        await self.coach.ensure_seeded()
        rows = await self.db.fetch_all("SELECT kind FROM pt_phrases")
        self.assertEqual(
            len(rows), len(EDNEI_SPEECH) + len(SURVIVAL_SET)
        )

    async def test_first_lesson_starts_at_line_one_plus_new_survival(self):
        turn = await self.coach.start_lesson(TODAY)
        self.assertEqual(turn["line"], EDNEI_SPEECH[0][0])
        state = await self.coach.active()
        # line 1 (the frontier) + 3 fresh survival phrases
        self.assertEqual(len(state["queue"]), 4)

    async def test_pass_advances_fail_retries_three_times(self):
        await self.coach.start_lesson(TODAY)
        flop = await self.coach.attempt(TODAY, "completely wrong words entirely")
        self.assertTrue(flop["retry"])
        self.assertFalse(flop["passed"])
        # Two more flops and the coach moves on gently — never a grind.
        await self.coach.attempt(TODAY, "still wrong")
        third = await self.coach.attempt(TODAY, "nope")
        self.assertFalse(third["retry"])
        self.assertIsNotNone(third["next"])

    async def test_full_lesson_completes_and_ledger_fills(self):
        await self.coach.start_lesson(TODAY)
        result = {"done": False}
        # Answer every drill with the exact target line — a perfect lesson.
        while not result["done"]:
            state = await self.coach.active()
            row = await self.db.fetch_one(
                "SELECT text FROM pt_phrases WHERE id = ?",
                (state["queue"][state["index"]],),
            )
            result = await self.coach.attempt(TODAY, row["text"])
        summary = result["summary"]
        self.assertTrue(summary["counts_for_streak"])
        self.assertEqual(summary["avg_score"], 100)
        self.assertIsNone(await self.coach.active())
        # Frontier advances: next lesson starts from line 1 again but the
        # queue now reaches line 2 (line 1 has a pass, not yet mastery).
        state2_turn = await self.coach.start_lesson(TODAY)
        state2 = await self.coach.active()
        speech_ids = [
            r["id"] for r in await self.db.fetch_all(
                "SELECT id FROM pt_phrases WHERE kind='speech' ORDER BY position LIMIT 2"
            )
        ]
        self.assertEqual(state2["queue"][:2], speech_ids)
        self.assertEqual(state2_turn["line"], EDNEI_SPEECH[0][0])

    async def test_readiness_moves_with_mastery(self):
        fresh = await self.coach.readiness(TODAY)
        self.assertEqual(fresh["speech_pct"], 0)
        self.assertEqual(fresh["days_left"], 21)
        await self.db.execute(
            "UPDATE pt_phrases SET passes = 3 WHERE kind = 'speech'"
        )
        done = await self.coach.readiness(TODAY)
        self.assertEqual(done["speech_pct"], 100)
        self.assertEqual(done["overall_pct"], 70)   # survival still at zero

    async def test_end_early_with_work_still_counts(self):
        await self.coach.start_lesson(TODAY)
        await self.coach.attempt(TODAY, EDNEI_SPEECH[0][0])
        summary = await self.coach.end_lesson(TODAY)
        self.assertTrue(summary["counts_for_streak"])
        self.assertIsNone(await self.coach.active())

    async def test_steph_phrase_prefers_what_paul_learned(self):
        before = await self.coach.steph_phrase(TODAY)
        self.assertEqual(before["text"], SURVIVAL_SET[0][0])
        await self.coach.ensure_seeded()
        await self.db.execute(
            "UPDATE pt_phrases SET introduced = 1 WHERE kind = 'survival'"
        )
        after = await self.coach.steph_phrase(TODAY)
        self.assertIn(after["text"], [t for t, _ in SURVIVAL_SET])


class LessonHarness:
    def __init__(self, db):
        self.telegram_calls = []

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            self.telegram_calls.append((request.url.path.split("/")[-1], request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})

        def claude_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "Lovely — that lands."}]}
            )

        def eleven_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"MP3")

        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.router = JarvisRouter(
            settings=settings,
            db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler)),
            claude=ClaudeClient("K", transport=httpx.MockTransport(claude_handler)),
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(eleven_handler)),
        )

    def texts(self):
        return [
            parse_qs(body.decode())["text"][0]
            for method, body in self.telegram_calls
            if method == "sendMessage"
        ]

    def voice_sends(self):
        return [m for m, _ in self.telegram_calls if m == "sendVoice"]


class TestLessonLane(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = LessonHarness(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_start_command_opens_lesson_with_voice_model(self):
        await self.h.router.handle_update(text_update("portuguese lesson please", OWNER))
        [reply] = self.h.texts()
        self.assertIn("days to Brazil", reply)
        self.assertIn(EDNEI_SPEECH[0][0], reply)
        # The line is modelled aloud — the Brazilian-voice note.
        self.assertEqual(self.h.voice_sends(), ["sendVoice"])
        self.assertIsNotNone(await self.h.router.pt.active())

    async def test_attempt_scores_and_moves_on(self):
        await self.h.router.handle_update(text_update("practise my portuguese", OWNER))
        await self.h.router.handle_update(text_update(EDNEI_SPEECH[0][0], OWNER))
        reply = self.h.texts()[-1]
        self.assertIn("100/100", reply)
        self.assertIn("Lovely — that lands.", reply)   # the brain's coaching line

    async def test_stop_words_end_lesson_and_log_streak(self):
        await self.h.router.handle_update(text_update("portuguese practice", OWNER))
        await self.h.router.handle_update(text_update(EDNEI_SPEECH[0][0], OWNER))
        await self.h.router.handle_update(text_update("that's enough for now", OWNER))
        reply = self.h.texts()[-1]
        self.assertIn("Lesson done", reply)
        self.assertIn("Steph", reply)
        self.assertIsNone(await self.h.router.pt.active())
        lane_today = await self.h.router._pt_today()
        self.assertTrue(await self.h.router.streaks.done_today("portuguese", lane_today))

    async def test_no_lesson_no_hijack(self):
        # Ordinary talk mentioning Portugal must not open a lesson.
        await self.h.router.handle_update(text_update("what's the weather", OWNER))
        self.assertIsNone(await self.h.router.pt.active())


class TestDeepgramLanguageOverride(unittest.IsolatedAsyncioTestCase):
    async def test_per_call_language_rides_the_request(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["language"] = dict(request.url.params)["language"]
            return httpx.Response(200, json={
                "results": {"channels": [{"alternatives": [{"transcript": "oi"}]}]}
            })

        client = DeepgramClient("K", transport=httpx.MockTransport(handler))
        await client.transcribe(b"AUDIO", language="multi")
        self.assertEqual(seen["language"], "multi")
        await client.transcribe(b"AUDIO")
        self.assertEqual(seen["language"], "en-GB")
        await client.close()


class TestCockpitGauge(unittest.IsolatedAsyncioTestCase):
    async def test_gather_carries_the_readiness_gauge(self):
        from app.cockpit.service import CockpitService
        from app.memory.store import LivingFacts

        with tempfile.TemporaryDirectory() as tmp:
            db = SqliteDatabase(os.path.join(tmp, "t.db"))
            await db.init()
            try:
                data = await CockpitService(db, living=LivingFacts(db)).gather()
                self.assertIn("portuguese", data)
                self.assertIn("days_left", data["portuguese"])
                self.assertIn("overall_pct", data["portuguese"])
            finally:
                await db.close()


if __name__ == "__main__":
    unittest.main()
