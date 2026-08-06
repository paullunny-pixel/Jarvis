"""The Mac desktop app's backend seam (6 Aug): /desktop endpoints, the
desktop_turn router surface, secret auth, and the pairing command."""
import os
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient

from app import main as app_main
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.config import Settings
from app.core.power import POWER_KEY
from app.core.router import JarvisRouter
from app.db.sqlite import SqliteDatabase
from tests.test_telegram_client import text_update
from tests.test_wakeup import OWNER, FakePhone, Harness


def deepgram_ok(text: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "results": {"channels": [{"alternatives": [{"transcript": text}]}]}
        })

    return handler


class DesktopBase(unittest.IsolatedAsyncioTestCase):
    deepgram_handler = staticmethod(lambda r: httpx.Response(500))

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = Harness(self.db, phone=FakePhone())
        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.router = JarvisRouter(
            settings=settings,
            db=self.db,
            telegram=self.h.jobs.telegram,
            claude=self.h.jobs.claude,
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(self.deepgram_handler)),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            heartbeat=self.h.jobs,
        )
        self.secret = settings.effective_desktop_secret
        app_main.app.state.router = self.router
        self.client = TestClient(app_main.app)

    async def asyncTearDown(self):
        del app_main.app.state.router
        await self.db.close()
        self._dir.cleanup()


class TestDesktopEndpoints(DesktopBase):
    def test_wrong_secret_403s_everywhere(self):
        for method, url in (
            ("get", "/desktop/WRONG/ping"),
            ("post", "/desktop/WRONG/message"),
            ("post", "/desktop/WRONG/voice"),
        ):
            self.assertEqual(getattr(self.client, method)(url).status_code, 403, url)

    def test_ping_answers(self):
        response = self.client.get(f"/desktop/{self.secret}/ping")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_message_turn_survives_a_dead_brain(self):
        # Claude is mocked to 500 — the reply must still be honest words.
        response = self.client.post(
            f"/desktop/{self.secret}/message", json={"text": "what's on today"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["reply"])
        self.assertIsNone(body["audio_b64"])

    def test_empty_message_asks_again(self):
        response = self.client.post(f"/desktop/{self.secret}/message", json={})
        self.assertEqual(response.json()["reply"], "Say again?")

    def test_voice_with_failed_stt_answers_honestly(self):
        response = self.client.post(
            f"/desktop/{self.secret}/voice", content=b"AUDIOBYTES",
            headers={"Content-Type": "audio/webm"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("give me it again", response.json()["reply"])

    def test_master_switch_holds_on_the_desktop(self):
        import asyncio

        asyncio.run(self.h.jobs.store.set(POWER_KEY, "off"))
        response = self.client.post(
            f"/desktop/{self.secret}/message", json={"text": "what's on today"}
        )
        self.assertIn("switched off", response.json()["reply"])
        # …and the desktop can wake him, same as Telegram.
        response = self.client.post(
            f"/desktop/{self.secret}/message", json={"text": "jarvis on"}
        )
        self.assertIn("Back online", response.json()["reply"])
        self.assertEqual(asyncio.run(self.h.jobs.store.get(POWER_KEY)), "")


class TestDesktopVoiceHappyPath(DesktopBase):
    deepgram_handler = staticmethod(deepgram_ok("add milk to the shopping list"))

    def test_transcript_rides_the_response(self):
        response = self.client.post(
            f"/desktop/{self.secret}/voice", content=b"AUDIOBYTES",
            headers={"Content-Type": "audio/webm"},
        )
        body = response.json()
        self.assertEqual(body["transcript"], "add milk to the shopping list")
        self.assertTrue(body["reply"])   # dead-brain honest fallback still words


class TestDesktopPairingCommand(DesktopBase):
    async def test_desktop_setup_hands_over_url_and_secret(self):
        await self.router.handle_update(text_update("desktop setup", OWNER))
        sent = self.h.texts()
        self.assertTrue(any(self.secret in t and "Desktop" in t for t in sent), sent)


class TestSecretDerivation(unittest.TestCase):
    def test_env_wins_and_fallback_is_stable(self):
        derived = Settings(telegram_bot_token="TOK", _env_file=None)
        self.assertEqual(len(derived.effective_desktop_secret), 32)
        self.assertEqual(
            derived.effective_desktop_secret,
            Settings(telegram_bot_token="TOK", _env_file=None).effective_desktop_secret,
        )
        pinned = Settings(
            telegram_bot_token="TOK", desktop_app_secret="MYSECRET", _env_file=None
        )
        self.assertEqual(pinned.effective_desktop_secret, "MYSECRET")


if __name__ == "__main__":
    unittest.main()
