import json
import os
import tempfile
import unittest

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.memory.crypto import PrivateBox
from app.memory.embedder import HashEmbedder
from app.memory.store import LivingFacts, MemoryStore
from app.memory.writer import extract_and_file, format_memory_context
from app.db.sqlite import SqliteDatabase


def haiku_returning(items) -> ClaudeClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": json.dumps(items)}]}
        )

    return ClaudeClient("K", transport=httpx.MockTransport(handler))


class TestWriter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.memory = MemoryStore(self.db, HashEmbedder(), PrivateBox("k"))
        self.living = LivingFacts(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_files_facts_to_rooms(self):
        claude = haiku_returning(
            [
                {"content": "Paul weighs 87.2 kg", "room": "health", "type": "LIVING",
                 "tags": ["weight"], "living_key": "health.weight_kg"},
                {"content": "Kenny is moving to Valencia", "room": "people", "type": "STABLE",
                 "tags": ["kenny"], "living_key": ""},
            ]
        )
        n = await extract_and_file(claude, self.memory, self.living, "weighed in at 87.2 this morning")
        self.assertEqual(n, 2)
        self.assertEqual(await self.living.get("health.weight_kg"), "Paul weighs 87.2 kg")
        hits = await self.memory.search("where is Kenny moving")
        self.assertTrue(any("Valencia" in h["content"] for h in hits))

    async def test_private_facts_encrypted_and_walled(self):
        claude = haiku_returning(
            [{"content": "Paul had a hard sobriety day", "room": "private", "type": "PRIVATE",
              "tags": [], "living_key": ""}]
        )
        await extract_and_file(claude, self.memory, self.living, "rough one today")
        business = await self.memory.search("hard sobriety day")
        self.assertEqual(business, [])
        raw = await self.db.fetch_one("SELECT content FROM memory_chunks WHERE is_private = 1")
        self.assertTrue(raw["content"].startswith("enc:"))

    async def test_bad_model_output_never_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": [{"type": "text", "text": "not json at all"}]})

        claude = ClaudeClient("K", transport=httpx.MockTransport(handler))
        n = await extract_and_file(claude, self.memory, self.living, "hello")
        self.assertEqual(n, 0)

    async def test_invalid_room_defaults_safely(self):
        claude = haiku_returning(
            [{"content": "fact", "room": "made_up_room", "type": "WEIRD", "tags": [], "living_key": ""}]
        )
        await extract_and_file(claude, self.memory, self.living, "x")
        rows = await self.memory.audit()
        self.assertEqual(rows[0]["room"], "you")
        self.assertEqual(rows[0]["type"], "STABLE")

    def test_format_memory_context(self):
        out = format_memory_context(
            [{"room": "companies", "content": "BMI make the fillers"}],
            [{"key": "villa.paid", "value": "27%"}],
        )
        self.assertIn("villa.paid: 27%", out)
        self.assertIn("[companies] BMI make the fillers", out)
        self.assertEqual(format_memory_context([], []), "")


if __name__ == "__main__":
    unittest.main()
