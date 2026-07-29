"""Build Slice: Live Voice Access — the shared realtime engine (ElevenLabs
Conversational AI), the cockpit Talk surface, backend tools, and the
phone-call wake channel. All HTTP mocked; no real sessions anywhere."""
import json
import os
import tempfile
import unittest

import httpx

from app.db.sqlite import SqliteDatabase
from app.voice.engine import AGENT_ID_KEY, VoiceEngine, build_agent_config
from app.voice.tools import VoiceTools
from tests.test_heartbeat import JobsHarness, at_local


class TestAgentConfig(unittest.TestCase):
    def test_persona_voice_and_tools(self):
        config = build_agent_config("https://jarvis.example", "VOICE123", "SECRET99")
        prompt = config["conversation_config"]["agent"]["prompt"]["prompt"]
        self.assertIn("Jarvis", prompt)
        self.assertIn("LIVE VOICE CALL", prompt)
        self.assertIn("must never log a run", prompt)   # negation invariant rides along
        self.assertEqual(config["conversation_config"]["tts"]["voice_id"], "VOICE123")
        tools = config["conversation_config"]["agent"]["prompt"]["tools"]
        names = {t["name"] for t in tools}
        self.assertIn("recall_memory", names)
        self.assertIn("mark_done", names)
        for t in tools:
            self.assertIn("/voice/tools/SECRET99/", t["api_schema"]["url"])
            # ElevenLabs' validator: POST requires a body schema with every
            # property described; parameterless tools must be plain GETs.
            schema = t["api_schema"].get("request_body_schema")
            if t["api_schema"]["method"] == "GET":
                self.assertIsNone(schema)
                continue
            self.assertEqual(t["api_schema"]["method"], "POST")
            self.assertTrue(schema["properties"])
            for prop in schema["properties"].values():
                self.assertIn("description", prop)

    def test_no_public_url_means_no_tools(self):
        config = build_agent_config("", "V", "S")
        self.assertEqual(config["conversation_config"]["agent"]["prompt"]["tools"], [])

    def test_interpreter_mode_config(self):
        config = build_agent_config("https://jarvis.example", "VOICE123", "S9", mode="interpreter")
        self.assertEqual(config["name"], "Jarvis (interpreter)")
        prompt = config["conversation_config"]["agent"]["prompt"]["prompt"]
        self.assertIn("INTERPRETER", prompt)
        self.assertIn("Portuguese rendition", prompt)
        self.assertIn("Jarvis here", prompt)              # marked context additions
        self.assertIn("don't take actions", prompt.lower().replace("’", "'"))
        # A stranger's Portuguese must never reach the action tools —
        # the interpreter carries memory recall ONLY.
        tools = config["conversation_config"]["agent"]["prompt"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["recall_memory"])
        self.assertEqual(config["conversation_config"]["tts"]["voice_id"], "VOICE123")

    def test_interpreter_without_public_url_has_no_tools(self):
        config = build_agent_config("", "V", "S", mode="interpreter")
        self.assertEqual(config["conversation_config"]["agent"]["prompt"]["tools"], [])

    def test_silence_is_thinking_in_both_modes(self):
        # Paul's rule: quiet on the call means he's THINKING — never fill it.
        for mode in ("assistant", "interpreter"):
            config = build_agent_config("https://j.example", "V", "S", mode=mode)
            cc = config["conversation_config"]
            self.assertEqual(cc["turn"]["turn_timeout"], 30)          # max patience
            self.assertIn("skip_turn", cc["agent"]["prompt"]["built_in_tools"])
            prompt = " ".join(cc["agent"]["prompt"]["prompt"].lower().split())
            self.assertIn("are you there", prompt)                    # named and banned
            self.assertIn("skip_turn", prompt)


class TestVoiceEngine(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.calls = []
        self.created = 0

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            self.calls.append((request.method, path))
            if path.endswith("/convai/agents/create"):
                self.created += 1
                return httpx.Response(200, json={"agent_id": f"AG{self.created}"})
            if request.method == "PATCH" and "/convai/agents/" in path:
                return httpx.Response(200, json={})
            if path.endswith("/convai/conversation/get_signed_url"):
                return httpx.Response(200, json={"signed_url": "wss://live/session?token=T"})
            if path.endswith("/convai/twilio/outbound-call"):
                return httpx.Response(200, json={"success": True})
            return httpx.Response(404, text=path)

        self.engine = VoiceEngine(
            "KEY", "VOICE123", self.db,
            public_url="https://jarvis.example", tool_secret="S1",
            transport=httpx.MockTransport(handler),
        )

    async def asyncTearDown(self):
        await self.engine.close()
        await self.db.close()
        self._dir.cleanup()

    async def test_agent_created_once_then_refreshed(self):
        agent = await self.engine.ensure_agent()
        self.assertEqual(agent, "AG1")
        from app.core.store import SettingsStore

        self.assertEqual(await SettingsStore(self.db).get(AGENT_ID_KEY), "AG1")
        await self.engine.ensure_agent()
        creates = [c for c in self.calls if c[1].endswith("/agents/create")]
        patches = [c for c in self.calls if c[0] == "PATCH"]
        self.assertEqual(len(creates), 1)   # second call refreshes, not recreates
        self.assertEqual(len(patches), 1)

    async def test_signed_session_url(self):
        url = await self.engine.signed_session_url()
        self.assertTrue(url.startswith("wss://live/session"))

    async def test_interpreter_is_its_own_agent(self):
        from app.core.store import SettingsStore

        assistant = await self.engine.ensure_agent()
        interpreter = await self.engine.ensure_agent("interpreter")
        self.assertNotEqual(assistant, interpreter)
        store = SettingsStore(self.db)
        self.assertEqual(await store.get(AGENT_ID_KEY), assistant)
        self.assertEqual(await store.get("voice_interpreter_agent_id"), interpreter)
        # Both modes mint signed URLs; the interpreter refreshes, not recreates.
        url = await self.engine.signed_session_url(mode="interpreter")
        self.assertTrue(url.startswith("wss://live/session"))
        creates = [c for c in self.calls if c[1].endswith("/agents/create")]
        self.assertEqual(len(creates), 2)

    async def test_built_in_tools_rejection_falls_back_cleanly(self):
        # If ElevenLabs' schema shifts under skip_turn, the button must
        # still work — one retry without the optional field.
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/convai/agents/create"):
                payload = json.loads(request.content)
                has_built_in = "built_in_tools" in payload["conversation_config"]["agent"]["prompt"]
                attempts.append(has_built_in)
                if has_built_in:
                    return httpx.Response(422, text="unknown field built_in_tools")
                return httpx.Response(200, json={"agent_id": "AGX"})
            return httpx.Response(404)

        engine = VoiceEngine(
            "KEY", "V", self.db, public_url="https://j.example", tool_secret="S",
            transport=httpx.MockTransport(handler),
        )
        try:
            self.assertEqual(await engine.ensure_agent(), "AGX")
            self.assertEqual(attempts, [True, False])
        finally:
            await engine.close()

    async def test_outbound_call(self):
        self.assertTrue(await self.engine.outbound_call("PN1", "+447700900000"))
        self.assertIn(("POST", "/v1/convai/twilio/outbound-call"), self.calls)


class TestVoiceTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.jobs = JobsHarness(self.db).jobs
        self.tools = VoiceTools(self.db, jobs=self.jobs)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_log_water_via_tool(self):
        result = await self.tools.dispatch("log_water", {"ml": 500})
        self.assertIn("0.5L", result)

    async def test_unconnected_services_answer_honestly(self):
        self.assertIn("Trello", await self.tools.dispatch("todays_focus", {}))
        self.assertIn("Email", await self.tools.dispatch("inbox_overview", {}))
        self.assertIn("Memory", await self.tools.dispatch("recall_memory", {"query": "villa"}))

    async def test_unknown_tool(self):
        self.assertIn("Unknown tool", await self.tools.dispatch("format_hard_drive", {}))


class TestPhoneWakeEscalation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = JobsHarness(self.db)
        self.jobs = self.h.jobs
        self.phone_calls = []

        class FakePhone:
            name = "phone"

            async def escalate(inner, step, text):
                self.phone_calls.append(step)

        self.jobs.phone_wake = FakePhone()
        await self.jobs.set_wake_enabled(True)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_phone_joins_every_fifth_step(self):
        await self.jobs.wake_tick(at_local("05:06"))   # step 2 — Telegram only
        self.assertEqual(self.phone_calls, [])
        await self.jobs.wake_tick(at_local("05:15"))   # step 5 — the phone rings
        self.assertEqual(self.phone_calls, [5])
        telegram = [m for m, _ in self.h.telegram_calls if m in ("sendMessage", "sendVoice")]
        self.assertEqual(len(telegram), 2)             # Telegram never stops


class TestCockpitSurface(unittest.TestCase):
    def test_page_carries_the_talk_button_and_session_flow(self):
        from app.cockpit.page import render_page

        html = render_page("/cockpit/S/data")
        self.assertIn("talk-btn", html)
        self.assertIn("Talk to Jarvis", html)
        self.assertIn("/voice-url", html)
        self.assertIn("elevenlabs-convai", html)

    def test_page_carries_the_interpreter_button(self):
        from app.cockpit.page import render_page

        html = render_page("/cockpit/S/data")
        self.assertIn("interpret-btn", html)
        self.assertIn("Interpret EN⇄PT", html)
        self.assertIn("mode=", html)   # the mode rides the voice-url request


if __name__ == "__main__":
    unittest.main()
