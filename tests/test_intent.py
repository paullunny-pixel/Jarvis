"""The understanding layer: when no exact phrase matches, the fast model reads
Paul's message (dyslexia/typos/garble tolerant) and known commands execute
through the same deterministic machinery; everything else reaches the brain."""
import json
import os
import tempfile
import unittest

import httpx

from app.core.intent import TRIAGE_SYSTEM, classify
from tests.test_heartbeat import JobsHarness, at_local
from tests.test_router import OWNER, RouterHarness
from tests.test_telegram_client import text_update


def intent_json(**kw):
    base = {"intent": "none", "hour": None, "wish": None, "place": None, "confident": False}
    base.update(kw)
    return json.dumps(base)


class TestClassify(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_carries_dyslexia_and_context(self):
        from app.clients.anthropic_client import ClaudeClient

        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": intent_json(intent="quiet_day", confident=True)}]}
            )

        claude = ClaudeClient("K", transport=httpx.MockTransport(handler))
        data = await classify(claude, "Qwiet dya", recent="Jarvis: morning brief…")
        self.assertEqual(data["intent"], "quiet_day")
        self.assertEqual(requests[-1]["model"], "claude-haiku-4-5")   # fast model, not the brain
        self.assertIn("dyslexia", requests[-1]["system"])
        self.assertIn("morning brief", requests[-1]["messages"][0]["content"])
        self.assertIn("Qwiet dya", requests[-1]["messages"][0]["content"])

    async def test_unconfident_none_and_garbage_all_yield_none(self):
        from app.clients.anthropic_client import ClaudeClient

        for text in (
            intent_json(intent="quiet_day", confident=False),   # unsure → not a command
            intent_json(intent="none", confident=True),
            "not json at all",
            intent_json(intent="made_up_intent", confident=True),
        ):
            claude = ClaudeClient("K", transport=httpx.MockTransport(
                lambda r, t=text: httpx.Response(200, json={"content": [{"type": "text", "text": t}]})
            ))
            self.assertIsNone(await classify(claude, "whatever"), text)

    def test_triage_knows_its_place(self):
        # Task/email/logging talk must stay with their own machinery.
        self.assertIn('ALWAYS "none"', TRIAGE_SYSTEM)
        self.assertIn("dyslexia", TRIAGE_SYSTEM)


class TestRouterTriage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db.sqlite import SqliteDatabase

        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    def harness(self, claude_reply):
        h = RouterHarness(self.db, claude_reply=claude_reply)
        jobs_harness = JobsHarness(self.db)
        h.router.heartbeat = jobs_harness.jobs
        return h, jobs_harness

    def texts(self, h):
        from urllib.parse import parse_qs

        return " ".join(
            parse_qs(body.decode())["text"][0]
            for method, body in h.telegram_calls
            if method == "sendMessage"
        )

    async def test_mangled_quiet_day_still_goes_quiet(self):
        h, jobs = self.harness(intent_json(intent="quiet_day", confident=True))
        await h.router.handle_update(text_update("Qwiet dya pls", OWNER))
        self.assertTrue(await jobs.jobs.quiet_today())
        self.assertIn("quiet for the rest of today", self.texts(h))
        # One fast-model call decided it; the brain never ran.
        self.assertEqual(len(h.claude_requests), 1)
        await jobs.jobs.move_water_nudge(at_local("15:05"))
        self.assertEqual(jobs.telegram_calls, [])   # and the switch is real

    async def test_wake_delay_intent_moves_tomorrow(self):
        from datetime import timedelta as td

        h, jobs = self.harness(intent_json(intent="wake_delay", hour=6, confident=True))
        await h.router.handle_update(text_update("dont wke me at 5 tomorow, 6 is fine", OWNER))
        tomorrow = (await jobs.jobs._today()) + td(days=1)
        self.assertEqual(await jobs.jobs.store.get("wake_delay"), f"{tomorrow.isoformat()}:6")
        self.assertIn("06:00", self.texts(h))

    async def test_build_list_add_is_no_longer_a_triage_intent(self):
        # 4 Aug: 'add to Trello' garbled to 'Activate' and triage confidently
        # filed it as a build-list wish. Additions are brain-only now — even
        # a model that answers build_list_add gets rejected and the message
        # falls through to the brain with full context.
        from app.core.store import SettingsStore

        h, _ = self.harness(
            intent_json(intent="build_list_add", wish="Activate to buy a second number", confident=True)
        )
        await h.router.handle_update(text_update("Activate to buy a second number", OWNER))
        self.assertEqual(await SettingsStore(self.db).get("build_list", ""), "")  # nothing filed
        models = [r["model"] for r in h.claude_requests]
        self.assertIn("claude-opus-5", models)   # the brain took it instead

    async def test_none_falls_through_to_the_brain(self):
        h, _ = self.harness(intent_json(intent="none", confident=True))
        await h.router.handle_update(text_update("how are the kids' school dates looking", OWNER))
        # Triage ran, said none, and the brain answered on top.
        self.assertGreaterEqual(len(h.claude_requests), 2)
        models = [r["model"] for r in h.claude_requests]
        self.assertIn("claude-haiku-4-5", models)
        self.assertIn("claude-opus-5", models)

    async def test_long_messages_skip_triage(self):
        h, _ = self.harness("Long answer.")
        await h.router.handle_update(text_update("so here's the thing " * 15, OWNER))
        models = [r["model"] for r in h.claude_requests]
        self.assertNotIn("claude-haiku-4-5", models)   # straight to the brain

    async def test_exact_phrases_never_reach_triage(self):
        # The deterministic fast-path still wins — no model call at all.
        h, jobs = self.harness("should not be called")
        await h.router.handle_update(text_update("quiet day", OWNER))
        self.assertTrue(await jobs.jobs.quiet_today())
        self.assertEqual(h.claude_requests, [])

    async def test_persona_carries_the_dyslexia_rule(self):
        from app.persona import build_system_prompt

        prompt = build_system_prompt()
        self.assertIn("dyslexia", prompt)
        self.assertIn("never comment on or correct his spelling", prompt)


if __name__ == "__main__":
    unittest.main()
