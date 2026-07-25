"""Milestone 6 through the router: private exchanges never touch the general
log or business history; SOS gets an instant voice reply."""
import json
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
from app.core.router import JarvisRouter
from app.memory.crypto import PrivateBox
from app.private.service import PrivateTrack
from app.db.sqlite import SqliteDatabase
from tests.test_telegram_client import text_update

OWNER = 111


class PrivateHarness:
    def __init__(self, db):
        self.telegram_calls = []
        self.claude_requests = []

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            self.telegram_calls.append((request.url.path.split("/")[-1], request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})

        def claude_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.claude_requests.append(payload)
            if "haiku" in payload["model"]:
                return httpx.Response(200, json={"content": [{"type": "text", "text": '{"mood":3,"lapse":false,"day_claim":null,"lonely":false}'}]})
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Steady on, I've got you."}]})

        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        claude = ClaudeClient("K", transport=httpx.MockTransport(claude_handler))
        box = PrivateBox("k")
        self.router = JarvisRouter(
            settings=settings,
            db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler)),
            claude=claude,
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"MP3"))),
            private_track=PrivateTrack(db, claude, box),
        )


class TestRouterPrivate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = PrivateHarness(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_private_message_redacted_in_general_log(self):
        await self.h.router.handle_update(text_update("really struggling to stay sober tonight", OWNER))
        rows = await self.db.fetch_all("SELECT transcript FROM messages")
        self.assertTrue(all(r["transcript"] == "[private exchange]" for r in rows))
        row = await self.db.fetch_one("SELECT content FROM sobriety")
        self.assertTrue(row["content"].startswith("enc:"))

    async def test_private_reply_is_voice(self):
        await self.h.router.handle_update(text_update("craving badly", OWNER))
        methods = [m for m, _ in self.h.telegram_calls]
        self.assertIn("sendVoice", methods)

    async def test_sos_replies_instantly(self):
        await self.h.router.handle_update(text_update("SOS", OWNER))
        methods = [m for m, _ in self.h.telegram_calls]
        self.assertIn("sendVoice", methods)
        row = await self.db.fetch_one("SELECT kind FROM sobriety")
        self.assertEqual(row["kind"], "sos")

    async def test_business_history_never_sees_private_content(self):
        await self.h.router.handle_update(text_update("nearly relapsed at the airport", OWNER))
        await self.h.router.handle_update(text_update("morning, plan the day", OWNER))
        # last claude call = business conversation; its history must be clean
        business = self.h.claude_requests[-1]
        history = json.dumps(business["messages"])
        self.assertNotIn("relapsed", history)
        self.assertNotIn("airport", history)


if __name__ == "__main__":
    unittest.main()
