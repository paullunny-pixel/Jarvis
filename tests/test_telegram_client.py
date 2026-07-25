import json
import unittest

import httpx

from app.clients.telegram_client import (
    TelegramClient,
    TelegramError,
    chunk_text,
    parse_update,
)


def text_update(text: str, chat_id: int = 111) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "from": {"id": chat_id, "first_name": "Paul", "last_name": "Lunny"},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def voice_update(file_id: str = "VOICE1", duration: int = 7, chat_id: int = 111) -> dict:
    return {
        "update_id": 2,
        "message": {
            "message_id": 6,
            "from": {"id": chat_id, "first_name": "Paul"},
            "chat": {"id": chat_id, "type": "private"},
            "voice": {"file_id": file_id, "duration": duration, "mime_type": "audio/ogg"},
        },
    }


class TestParsing(unittest.TestCase):
    def test_parse_text(self):
        msg = parse_update(text_update("hello"))
        self.assertEqual(msg.text, "hello")
        self.assertEqual(msg.chat_id, 111)
        self.assertEqual(msg.from_name, "Paul Lunny")
        self.assertFalse(msg.is_voice)

    def test_parse_voice(self):
        msg = parse_update(voice_update())
        self.assertTrue(msg.is_voice)
        self.assertEqual(msg.voice_file_id, "VOICE1")
        self.assertEqual(msg.voice_duration, 7)

    def test_parse_irrelevant_update(self):
        self.assertIsNone(parse_update({"update_id": 3, "edited_message": {}}))
        self.assertIsNone(parse_update({"update_id": 4}))

    def test_chunking_respects_limit_and_boundaries(self):
        text = "para one\n\n" + ("a" * 4000) + "\n\nlast para"
        chunks = chunk_text(text)
        self.assertTrue(all(len(c) <= 4096 for c in chunks))
        self.assertEqual("".join(chunks).replace("\n", "").replace(" ", ""),
                         text.replace("\n", "").replace(" ", ""))

    def test_chunking_hard_split_without_separators(self):
        chunks = chunk_text("a" * 9000)
        self.assertEqual([len(c) for c in chunks], [4096, 4096, 808])


class TestClient(unittest.IsolatedAsyncioTestCase):
    def make_client(self, handler) -> TelegramClient:
        return TelegramClient("TESTTOKEN", transport=httpx.MockTransport(handler))

    async def test_send_text_chunks_long_messages(self):
        sent = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json={"ok": True, "result": {}})

        client = self.make_client(handler)
        await client.send_text(1, "a" * 5000)
        self.assertEqual(len(sent), 2)
        self.assertIn("/botTESTTOKEN/sendMessage", str(sent[0].url))

    async def test_send_voice_short_transcript_uses_caption(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured[request.url.path.split("/")[-1]] = request
            return httpx.Response(200, json={"ok": True, "result": {}})

        client = self.make_client(handler)
        await client.send_voice(1, b"MP3BYTES", transcript="short transcript")
        self.assertIn("sendVoice", captured)
        body = captured["sendVoice"].content
        self.assertIn(b"short transcript", body)
        self.assertIn(b"MP3BYTES", body)
        self.assertNotIn("sendMessage", captured)

    async def test_send_voice_long_transcript_follows_up_as_text(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path.split("/")[-1])
            return httpx.Response(200, json={"ok": True, "result": {}})

        client = self.make_client(handler)
        await client.send_voice(1, b"MP3", transcript="x" * 2000)
        self.assertEqual(calls, ["sendVoice", "sendMessage"])

    async def test_download_file(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/getFile"):
                return httpx.Response(
                    200, json={"ok": True, "result": {"file_id": "V1", "file_path": "voice/f.oga"}}
                )
            self.assertIn("/file/botTESTTOKEN/voice/f.oga", str(request.url))
            return httpx.Response(200, content=b"OGGDATA")

        client = self.make_client(handler)
        data = await client.download_file("V1")
        self.assertEqual(data, b"OGGDATA")

    async def test_api_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": False, "description": "Unauthorized"})

        client = self.make_client(handler)
        with self.assertRaises(TelegramError):
            await client.send_text(1, "hi")

    async def test_set_webhook_sends_secret(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"ok": True, "result": True})

        client = self.make_client(handler)
        await client.set_webhook("https://x.onrender.com/webhook/telegram/abc", "sekrit")
        self.assertIn("sekrit", captured["body"])
        self.assertIn("drop_pending_updates", captured["body"])


if __name__ == "__main__":
    unittest.main()
