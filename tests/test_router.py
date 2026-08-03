"""End-to-end Milestone 1 test: a Telegram update goes in, Jarvis's voice/text
reply comes out, and everything lands in the transcript log. All four external
services are mocked at the HTTP layer — the entire app code path is real."""
import json
import os
import tempfile
import unittest

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.telegram_client import TelegramClient
from app.config import Settings
from app.core.router import STRANGER_REPLY, JarvisRouter
from app.db.sqlite import SqliteDatabase
from tests.test_telegram_client import text_update, voice_update

OWNER = 111
STRANGER = 999


class RouterHarness:
    """Wires a real router to HTTP mocks and records outbound calls."""

    def __init__(self, db, claude_reply="Short answer. Go run.", tts_fails=False, claude_json=None):
        self.telegram_calls: list[tuple[str, bytes]] = []
        self.claude_requests: list[dict] = []

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.split("/")[-1]
            self.telegram_calls.append((method, request.content))
            if method == "getFile":
                return httpx.Response(
                    200, json={"ok": True, "result": {"file_path": "voice/note.oga"}}
                )
            if "/file/" in request.url.path:
                return httpx.Response(200, content=b"OGG_AUDIO")
            return httpx.Response(200, json={"ok": True, "result": {}})

        def claude_handler(request: httpx.Request) -> httpx.Response:
            self.claude_requests.append(json.loads(request.content))
            body = claude_json or {"content": [{"type": "text", "text": claude_reply}]}
            return httpx.Response(200, json=body)

        def deepgram_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"results": {"channels": [{"alternatives": [{"transcript": "voice words here"}]}]}},
            )

        def elevenlabs_handler(request: httpx.Request) -> httpx.Response:
            if tts_fails:
                return httpx.Response(500, text="tts down")
            return httpx.Response(200, content=b"MP3_REPLY")

        settings = Settings(
            telegram_bot_token="TOK",
            anthropic_api_key="K",
            deepgram_api_key="K",
            elevenlabs_api_key="K",
            _env_file=None,
        )
        self.router = JarvisRouter(
            settings=settings,
            db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler)),
            claude=ClaudeClient("K", transport=httpx.MockTransport(claude_handler)),
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(deepgram_handler)),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(elevenlabs_handler)),
        )

    def methods(self) -> list[str]:
        return [m for m, _ in self.telegram_calls]


class TestRouter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_brain_is_told_about_recently_cleared_cards(self):
        # The BMI-surveys bug: memory kept insisting closed cards were due.
        # The brain's system prompt must name what's cleared and carry the
        # staleness rule, so a remembered obligation can be contradicted.
        from datetime import datetime as dt, timezone as tz

        from tests.test_daily12_service import MultiBoardHarness

        h = RouterHarness(self.db)
        h.router.daily12 = MultiBoardHarness(self.db).service
        await self.db.execute(
            "INSERT INTO tasks (trello_id, title, list_name, board_id, board_name,"
            " actionable, score, synced_at) VALUES ('TX', 'BMI doctor surveys',"
            " 'Done', 'B1', 'Master Board', 0, 0, ?)",
            (dt.now(tz.utc).isoformat(timespec="seconds"),),
        )
        await h.router.handle_update(text_update("morning, how are we", OWNER))
        system = h.claude_requests[-1]["system"]
        self.assertIn("RECENTLY CLEARED", system)
        self.assertIn("BMI doctor surveys — DONE", system)
        self.assertIn("STALENESS RULE", system)

    async def test_task_parser_sees_the_conversation_it_is_replying_to(self):
        # The digest-reply bug: Paul answers a digest by voice ('add those
        # Trello candidates, I've replied to Jack's email') and the parser,
        # blind to the digest, hunted board cards and invented vague titles.
        from tests.test_daily12_service import MultiBoardHarness

        h = RouterHarness(self.db)
        h.router.daily12 = MultiBoardHarness(self.db).service
        digest = (
            "ACTION NEEDED FROM YOU\n"
            "- Reply today to Jack's birthday party reservation email.\n"
            "TRELLO CANDIDATES\n"
            "- Adriana: complete meso packaging check and IFU (priority one)."
        )
        await h.router.log.log("out", digest, chat_id=OWNER, meta={"heartbeat": True})
        await h.router.handle_update(
            text_update("add those trello candidates to the board", OWNER)
        )
        parser_calls = [
            r for r in h.claude_requests
            if str(r.get("system", "")).startswith("You convert Paul's message")
        ]
        self.assertEqual(len(parser_calls), 1)
        system = parser_calls[0]["system"]
        self.assertIn("RECENT CONVERSATION", system)
        self.assertIn("Jack's birthday party reservation email", system)
        self.assertIn("meso packaging check", system)
        # The message being parsed rides as the prompt, not duplicated in
        # the recent block.
        self.assertNotIn("add those trello candidates", system)

    async def test_tune_jarvis_rides_the_next_reply(self):
        # Phase A1 (31 Jul): Paul pastes his preferred style and it becomes
        # the top authority on tone, injected into every brain turn.
        from app.core.store import SettingsStore
        from app.memory.brief import BRIEF_KEY, PERSONA_NOTES_KEY

        h = RouterHarness(self.db)
        await h.router.handle_update(text_update(
            "tune jarvis: talk like ChatGPT does with me — casual, curious, no butler stuff",
            OWNER,
        ))
        store = SettingsStore(self.db)
        self.assertIn("no butler stuff", await store.get(PERSONA_NOTES_KEY, ""))
        await store.set(BRIEF_KEY, "RIGHT NOW: in Dubai until Friday.")
        await h.router.handle_update(text_update("morning, how are we", OWNER))
        system = h.claude_requests[-1]["system"]
        self.assertIn("PAUL'S OWN WORDS", system)
        self.assertIn("no butler stuff", system)
        self.assertIn("THE PAUL BRIEF", system)
        self.assertIn("in Dubai until Friday", system)
        # And reset clears it.
        await h.router.handle_update(text_update("reset jarvis persona", OWNER))
        self.assertEqual(await store.get(PERSONA_NOTES_KEY, ""), "")

    async def test_update_your_brief_command(self):
        from app.core.store import SettingsStore
        from app.memory.brief import BRIEF_KEY

        h = RouterHarness(self.db, claude_reply="THE FRESH BRIEF")
        await h.router.handle_update(text_update("update your brief please", OWNER))
        store = SettingsStore(self.db)
        self.assertEqual(await store.get(BRIEF_KEY, ""), "THE FRESH BRIEF")

    async def test_text_in_short_reply_comes_back_as_voice(self):
        h = RouterHarness(self.db)
        await h.router.handle_update(text_update("what's first today?", OWNER))
        self.assertIn("sendVoice", h.methods())
        # transcript rides along as caption
        voice_call = dict(h.telegram_calls)["sendVoice"]
        self.assertIn(b"Short answer. Go run.", voice_call)
        # both directions logged
        log = await h.router.log.recent()
        self.assertEqual([r["direction"] for r in log], ["in", "out"])
        self.assertEqual(log[0]["transcript"], "what's first today?")

    async def test_voice_note_is_downloaded_transcribed_and_answered(self):
        h = RouterHarness(self.db)
        await h.router.handle_update(voice_update(chat_id=OWNER))
        self.assertIn("getFile", h.methods())
        self.assertIn("sendVoice", h.methods())
        log = await h.router.log.recent()
        self.assertEqual(log[0]["transcript"], "voice words here")
        self.assertEqual(log[0]["kind"], "voice")
        # conversation history was sent to the brain (triage may run first)
        brain = [r for r in h.claude_requests if r["model"] == "claude-opus-5"][0]
        self.assertEqual(brain["messages"][-1]["content"], "voice words here")
        self.assertIn("Jarvis", brain["system"])

    async def test_listy_reply_goes_as_text(self):
        h = RouterHarness(self.db, claude_reply="Today:\n- surveys\n- run\n- call Kiefer")
        await h.router.handle_update(text_update("list my day", OWNER))
        self.assertIn("sendMessage", h.methods())
        self.assertNotIn("sendVoice", h.methods())

    async def test_tts_failure_falls_back_to_text(self):
        h = RouterHarness(self.db, tts_fails=True)
        await h.router.handle_update(text_update("hey", OWNER))
        self.assertIn("sendMessage", h.methods())
        self.assertNotIn("sendVoice", h.methods()[:1])  # sendVoice never succeeded
        log = await h.router.log.recent()
        self.assertEqual(len(log), 2)  # reply still logged

    async def test_stranger_is_locked_out_after_owner_lock(self):
        h = RouterHarness(self.db)
        await h.router.handle_update(text_update("hi jarvis", OWNER))   # locks owner
        h.telegram_calls.clear()
        await h.router.handle_update(text_update("hello?", STRANGER))
        self.assertEqual(h.methods(), ["sendMessage"])
        from urllib.parse import parse_qs

        sent_text = parse_qs(h.telegram_calls[0][1].decode())["text"][0]
        self.assertEqual(sent_text, STRANGER_REPLY)
        # stranger's message never reached the log or the brain
        log = await h.router.log.recent()
        self.assertTrue(all("hello?" not in r["transcript"] for r in log))

    async def test_explicit_owner_id_setting_wins(self):
        h = RouterHarness(self.db)
        h.router.settings.telegram_owner_chat_id = OWNER
        await h.router.handle_update(text_update("intruder", STRANGER))
        self.assertEqual(h.methods(), ["sendMessage"])

    async def test_claude_failure_sends_apology(self):
        h = RouterHarness(self.db, claude_json={"content": []})

        async def broken(*a, **k):
            raise RuntimeError("brain offline")

        h.router.claude.converse = broken
        await h.router.handle_update(text_update("hey", OWNER))
        self.assertIn("sendMessage", h.methods())
        self.assertIn(b"snag", dict(h.telegram_calls)["sendMessage"])


if __name__ == "__main__":
    unittest.main()
