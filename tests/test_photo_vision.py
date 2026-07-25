"""Regression: photo messages must reach the brain as real images (vision)."""
import json
import os
import tempfile
import unittest

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.telegram_client import TelegramClient, parse_update
from app.config import Settings
from app.core.router import JarvisRouter
from app.db.sqlite import SqliteDatabase

OWNER = 111
FAKE_JPEG = b"\xff\xd8\xff\xe0FAKEJPEGBYTES"


def photo_update(caption="This is my girlfriend"):
    return {
        "update_id": 20,
        "message": {
            "message_id": 21,
            "from": {"id": OWNER, "first_name": "Paul"},
            "chat": {"id": OWNER, "type": "private"},
            "photo": [
                {"file_id": "PSMALL", "width": 90, "height": 60},
                {"file_id": "PBIG", "width": 1280, "height": 960},
            ],
            "caption": caption,
        },
    }


class TestParsePhoto(unittest.TestCase):
    def test_largest_size_selected(self):
        msg = parse_update(photo_update())
        self.assertTrue(msg.is_photo)
        self.assertEqual(msg.photo_file_id, "PBIG")
        self.assertEqual(msg.text, "This is my girlfriend")


class PhotoHarness:
    def __init__(self, db):
        self.telegram_calls = []
        self.claude_requests = []

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.split("/")[-1]
            self.telegram_calls.append(method)
            if method == "getFile":
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/p.jpg"}})
            if "/file/" in request.url.path:
                return httpx.Response(200, content=FAKE_JPEG)
            return httpx.Response(200, json={"ok": True, "result": {}})

        def claude_handler(request: httpx.Request) -> httpx.Response:
            self.claude_requests.append(json.loads(request.content))
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "Nice spot — looks like a pub table."}]}
            )

        self.router = JarvisRouter(
            settings=Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None),
            db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler)),
            claude=ClaudeClient("K", transport=httpx.MockTransport(claude_handler)),
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"MP3"))),
        )


class TestPhotoFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = PhotoHarness(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_photo_reaches_brain_as_image_block(self):
        import base64

        await self.h.router.handle_update(photo_update("where do you think this is?"))
        self.assertIn("getFile", self.h.telegram_calls)
        [request] = self.h.claude_requests
        last_turn = request["messages"][-1]
        self.assertEqual(last_turn["role"], "user")
        blocks = last_turn["content"]
        self.assertEqual(blocks[0]["type"], "image")
        self.assertEqual(blocks[0]["source"]["media_type"], "image/jpeg")
        self.assertEqual(
            base64.b64decode(blocks[0]["source"]["data"]), FAKE_JPEG
        )
        self.assertIn("where do you think this is?", blocks[1]["text"])

    async def test_photo_logged_and_replied(self):
        await self.h.router.handle_update(photo_update())
        rows = await self.db.fetch_all("SELECT * FROM messages ORDER BY id")
        self.assertEqual(rows[0]["transcript"], "[photo] This is my girlfriend")
        self.assertEqual(rows[1]["direction"], "out")
        # short conversational reply to a photo goes out as voice
        self.assertIn("sendVoice", self.h.telegram_calls)

    async def test_photo_without_caption_still_works(self):
        await self.h.router.handle_update(photo_update(caption=""))
        [request] = self.h.claude_requests
        blocks = request["messages"][-1]["content"]
        self.assertEqual(blocks[0]["type"], "image")


if __name__ == "__main__":
    unittest.main()
