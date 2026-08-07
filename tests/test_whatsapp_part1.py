"""WhatsApp Part 1 (7 Aug build slice): Paul talks to Jarvis on WhatsApp —
same brain as Telegram, owner-only, voice notes transcribed, replies sent
back as text or voice. No real network anywhere; Meta's Graph API, Deepgram,
Claude and ElevenLabs are all mocked at the HTTP layer."""
import json
import os
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient

from app import main as app_main
from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.whatsapp_client import WhatsAppClient
from app.config import Settings
from app.core.router import JarvisRouter
from app.db.sqlite import SqliteDatabase
from tests.test_wakeup import OWNER, FakePhone, Harness

OWNER_WA = "447700900123"
STRANGER_WA = "447700999999"


def claude_says(text: str) -> ClaudeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})

    return ClaudeClient("K", transport=httpx.MockTransport(handler))


def deepgram_says(text: str) -> DeepgramClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "results": {"channels": [{"alternatives": [{"transcript": text}]}]}
        })

    return DeepgramClient("K", transport=httpx.MockTransport(handler))


def elevenlabs_ok() -> ElevenLabsClient:
    return ElevenLabsClient(
        "K", voice_id="V",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"MP3BYTES")),
    )


class TestDownloadMedia(unittest.IsolatedAsyncioTestCase):
    async def test_two_step_fetch_carries_the_bearer_header(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.path.endswith("/MEDIA1"):
                return httpx.Response(200, json={
                    "url": "https://lookaside.fbsbx.com/attach?mid=MEDIA1",
                    "mime_type": "audio/ogg; codecs=opus",
                })
            return httpx.Response(200, content=b"OGGBYTES")

        client = WhatsAppClient("TOK", "PN1", transport=httpx.MockTransport(handler))
        audio, mime = await client.download_media("MEDIA1")
        self.assertEqual(audio, b"OGGBYTES")
        self.assertIn("audio/ogg", mime)
        self.assertEqual(len(seen), 2)
        await client.close()

    async def test_lookup_failure_raises(self):
        from app.clients.whatsapp_client import WhatsAppError

        client = WhatsAppClient(
            "TOK", "PN1", transport=httpx.MockTransport(lambda r: httpx.Response(404, text="gone"))
        )
        with self.assertRaises(WhatsAppError):
            await client.download_media("MEDIA1")
        await client.close()


class TestSendVoice(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_then_sends_by_media_id(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/media"):
                captured["upload"] = True
                return httpx.Response(200, json={"id": "MEDIAOUT"})
            captured["message"] = json.loads(request.content)
            return httpx.Response(200, json={"messages": [{"id": "wamid.x"}]})

        client = WhatsAppClient("TOK", "PN1", sending_enabled=True, transport=httpx.MockTransport(handler))
        ok = await client.send_voice("447700900123", b"MP3BYTES")
        self.assertTrue(ok)
        self.assertTrue(captured["upload"])
        self.assertEqual(captured["message"]["audio"]["id"], "MEDIAOUT")
        self.assertEqual(captured["message"]["type"], "audio")
        await client.close()

    async def test_refuses_while_sending_disabled(self):
        calls = []
        client = WhatsAppClient(
            "TOK", "PN1", sending_enabled=False,
            transport=httpx.MockTransport(lambda r: (calls.append(r), httpx.Response(200))[1]),
        )
        self.assertFalse(await client.send_voice("447700900123", b"MP3BYTES"))
        self.assertEqual(calls, [])
        await client.close()


class WhatsAppBase(unittest.IsolatedAsyncioTestCase):
    claude_reply = "Sounds good."
    deepgram_reply = "what's on today"
    sending_enabled = True
    owner_number = OWNER_WA

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = Harness(self.db, phone=FakePhone())
        settings = Settings(
            telegram_bot_token="TOK", telegram_owner_chat_id=OWNER,
            whatsapp_verify_token="VERIFY", whatsapp_owner_number=self.owner_number,
            whatsapp_sending_enabled=self.sending_enabled, _env_file=None,
        )
        self.sent = {"text": [], "voice": []}

        def wa_handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/MEDIA1"):
                return httpx.Response(200, json={
                    "url": "https://lookaside.fbsbx.com/attach?mid=MEDIA1",
                    "mime_type": "audio/ogg",
                })
            if "lookaside.fbsbx.com" in str(request.url):
                return httpx.Response(200, content=b"OGGBYTES")
            if path.endswith("/media"):
                return httpx.Response(200, json={"id": "MEDIAOUT"})
            if path.endswith("/messages"):
                body = json.loads(request.content)
                if body.get("type") == "audio":
                    self.sent["voice"].append(body)
                else:
                    self.sent["text"].append(body)
                return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})
            return httpx.Response(404)

        self.wa_client = WhatsAppClient(
            "TOK", "PN1", sending_enabled=self.sending_enabled,
            transport=httpx.MockTransport(wa_handler),
        )
        self.router = JarvisRouter(
            settings=settings,
            db=self.db,
            telegram=self.h.jobs.telegram,
            claude=claude_says(self.claude_reply),
            deepgram=deepgram_says(self.deepgram_reply),
            elevenlabs=elevenlabs_ok(),
            heartbeat=self.h.jobs,
        )
        self.router.whatsapp = self.wa_client
        app_main.app.state.router = self.router
        self.client = TestClient(app_main.app)

    async def asyncTearDown(self):
        del app_main.app.state.router
        await self.wa_client.close()
        await self.db.close()
        self._dir.cleanup()

    def _post(self, from_wa: str, mtype: str = "text", text: str = "hi", media_id: str = "") -> httpx.Response:
        msg = {"from": from_wa, "id": "wamid.1", "timestamp": "1754300000", "type": mtype}
        if mtype == "text":
            msg["text"] = {"body": text}
        else:
            msg["audio"] = {"id": media_id}
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "WABA1", "changes": [{"value": {
                "messaging_product": "whatsapp",
                "contacts": [{"profile": {"name": "Paul"}, "wa_id": from_wa}],
                "messages": [msg],
            }, "field": "messages"}]}],
        }
        return self.client.post("/webhook/whatsapp", content=json.dumps(payload))


class TestOwnerRouting(WhatsAppBase):
    def test_owner_text_message_gets_a_reply(self):
        response = self._post(OWNER_WA, "text", "what's on today")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ingested"], 1)
        self.assertTrue(self.sent["text"] or self.sent["voice"])

    def test_stranger_is_silently_ignored(self):
        response = self._post(STRANGER_WA, "text", "hello")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ingested"], 0)
        self.assertEqual(self.sent["text"], [])
        self.assertEqual(self.sent["voice"], [])

    async def test_voice_note_is_transcribed_and_routed(self):
        response = self._post(OWNER_WA, "audio", media_id="MEDIA1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ingested"], 1)
        row = await self.db.fetch_one(
            "SELECT transcript FROM messages WHERE direction = 'in' ORDER BY id DESC"
        )
        self.assertEqual(row["transcript"], "what's on today")


class TestSendChannel(WhatsAppBase):
    claude_reply = "[TEXT] Here's the list:\n- one\n- two"

    def test_text_tagged_reply_sends_as_text(self):
        self._post(OWNER_WA, "text", "give me the list")
        self.assertEqual(len(self.sent["text"]), 1)
        self.assertEqual(len(self.sent["voice"]), 0)
        self.assertNotIn("[TEXT]", self.sent["text"][0]["text"]["body"])


class TestSendingDisabled(WhatsAppBase):
    sending_enabled = False

    def test_reply_computed_but_send_refused_without_crashing(self):
        response = self._post(OWNER_WA, "text", "hello")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sent["text"], [])
        self.assertEqual(self.sent["voice"], [])


class TestOwnerNotConfigured(WhatsAppBase):
    owner_number = ""

    async def test_falls_back_to_read_only_ingest(self):
        response = self._post(OWNER_WA, "text", "invoices signed")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ingested"], 0)
        row = await self.db.fetch_one("SELECT message FROM wa_direct_ingest")
        self.assertIn("invoices signed", row["message"])
        self.assertEqual(self.sent["text"], [])


if __name__ == "__main__":
    unittest.main()
