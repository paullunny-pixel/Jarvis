"""Milestone 2 end-to-end through the router: memory recall in conversation,
document upload via Telegram, and 'show me the X' retrieval."""
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
from app.core.store import SettingsStore
from app.documents.service import DocumentLibrary
from app.memory.crypto import PrivateBox
from app.memory.embedder import HashEmbedder
from app.memory.seed import load_day_one_brain
from app.memory.store import LivingFacts, MemoryStore
from app.db.sqlite import SqliteDatabase
from tests.test_telegram_client import text_update

OWNER = 111


def document_update(file_id="DOC1", name="bmi-contract.pdf", mime="application/pdf"):
    return {
        "update_id": 9,
        "message": {
            "message_id": 10,
            "from": {"id": OWNER, "first_name": "Paul"},
            "chat": {"id": OWNER, "type": "private"},
            "document": {"file_id": file_id, "file_name": name, "mime_type": mime},
        },
    }


class MemoryRouterHarness:
    def __init__(self, db, memory, living, library, claude_reply="Understood."):
        self.telegram_calls = []
        self.claude_requests = []
        self.doc_bytes = b"%PDF-fake BMI contract about dermal filler manufacturing"

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.split("/")[-1]
            self.telegram_calls.append((method, request.content))
            if method == "getFile":
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "docs/f.pdf"}})
            if "/file/" in request.url.path:
                return httpx.Response(200, content=self.doc_bytes)
            return httpx.Response(200, json={"ok": True, "result": {}})

        def claude_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.claude_requests.append(payload)
            if "haiku" in payload["model"]:
                return httpx.Response(200, json={"content": [{"type": "text", "text": "[]"}]})
            return httpx.Response(200, json={"content": [{"type": "text", "text": claude_reply}]})

        self.router = JarvisRouter(
            settings=Settings(telegram_bot_token="TOK", _env_file=None),
            db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler)),
            claude=ClaudeClient("K", transport=httpx.MockTransport(claude_handler)),
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient(
                "K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"MP3"))
            ),
            memory=memory,
            living=living,
            library=library,
        )

    def methods(self):
        return [m for m, _ in self.telegram_calls]


class TestRouterMemory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.memory = MemoryStore(self.db, HashEmbedder(), PrivateBox("test"))
        self.living = LivingFacts(self.db)
        self.library = DocumentLibrary(self.db, self.memory)
        await load_day_one_brain(self.memory, self.living, SettingsStore(self.db))

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    def harness(self, **kw) -> MemoryRouterHarness:
        return MemoryRouterHarness(self.db, self.memory, self.living, self.library, **kw)

    async def test_brain_receives_recalled_knowledge(self):
        h = self.harness()
        await h.router.handle_update(text_update("what do we do about the BMI fillers situation?", OWNER))
        system = h.claude_requests[0]["system"]
        self.assertIn("Recalled memory", system)
        self.assertIn("BMI", system)
        self.assertIn("Current living facts", system)
        self.assertIn("villa.paid", system)

    async def test_private_knowledge_never_reaches_business_context(self):
        h = self.harness()
        await h.router.handle_update(text_update("feeling a bit off around sobriety today", OWNER))
        system = h.claude_requests[0]["system"]
        # Only the recalled-memory block matters (the persona itself may name
        # the sobriety track — that's instruction, not leaked data).
        recalled = system.split("Relevant knowledge")[-1]
        self.assertNotIn("trade shows", recalled)   # sobriety triggers (private room)
        self.assertNotIn("K-pop", recalled)         # Eva's profile (private room)
        self.assertNotIn("Brazilian", recalled)     # Steph's profile (private room)
        self.assertNotIn("sobriety.", recalled)     # no private living keys
        # (commitments.steph in Finances is deliberate — Day-One Brain Room 4)

    async def test_document_upload_is_ingested_and_confirmed(self):
        h = self.harness()
        h.doc_bytes = b"plain text contract with BMI terms"
        await h.router.handle_update(document_update(name="bmi-terms.txt", mime="text/plain"))
        self.assertIn("getFile", h.methods())
        self.assertIn("sendMessage", h.methods())
        row = await self.db.fetch_one("SELECT filename FROM documents")
        self.assertEqual(row["filename"], "bmi-terms.txt")
        hits = await self.memory.search("BMI terms contract")
        self.assertTrue(any("bmi-terms.txt" in h_["content"] for h_ in hits))

    async def test_show_me_the_contract_sends_the_file(self):
        h = self.harness()
        h.doc_bytes = b"the BMI manufacturing contract dermal fillers MOQ"
        await h.router.handle_update(document_update(name="bmi-contract.txt", mime="text/plain"))
        h.telegram_calls.clear()
        await h.router.handle_update(text_update("show me the BMI contract", OWNER))
        self.assertIn("sendDocument", h.methods())
        sent = dict(h.telegram_calls)["sendDocument"]
        self.assertIn(b"bmi-contract.txt", sent)
        self.assertIn(h.doc_bytes, sent)

    async def test_no_matching_document_falls_through_to_brain(self):
        h = self.harness(claude_reply="No such file yet — upload it and I'll keep it safe.")
        await h.router.handle_update(text_update("show me the alien invasion contract", OWNER))
        self.assertTrue(len(h.claude_requests) >= 1)


if __name__ == "__main__":
    unittest.main()
