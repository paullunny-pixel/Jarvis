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


class TestPaulBrief(unittest.IsolatedAsyncioTestCase):
    """Phase A1: the living Paul Brief — composed from the day, private wall held."""

    async def asyncSetUp(self):
        import os
        import tempfile

        from app.db.sqlite import SqliteDatabase

        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        from app.core.store import SettingsStore

        self.store = SettingsStore(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_brief_composes_stores_and_holds_the_private_wall(self):
        import httpx

        from app.clients.anthropic_client import ClaudeClient
        from app.core.store import MessageLog
        from app.memory.brief import BRIEF_KEY, compose_brief
        from app.memory.store import LivingFacts

        log = MessageLog(self.db)
        await log.log("in", "Flying to Dubai tonight, back Friday")
        await log.log("out", "[private exchange]")           # must never reach the model
        living = LivingFacts(self.db)
        await living.set("villa.paid", "27%", room="finances")
        await living.set("sobriety.days", "44", room="private")
        await self.store.set(BRIEF_KEY, "OLD BRIEF v1")

        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "NEW BRIEF v2"}]}
            )

        claude = ClaudeClient("K", transport=httpx.MockTransport(handler))
        brief = await compose_brief(claude, self.db, self.store)
        self.assertEqual(brief, "NEW BRIEF v2")
        self.assertEqual(await self.store.get(BRIEF_KEY), "NEW BRIEF v2")
        material = requests[-1]["messages"][0]["content"]
        self.assertIn("OLD BRIEF v1", material)              # merges, never restarts
        self.assertIn("Dubai tonight", material)
        self.assertIn("villa.paid", material)
        self.assertNotIn("[private exchange]", material)     # the wall holds
        self.assertNotIn("sobriety.days", material)

    async def test_failed_rebuild_keeps_the_old_brief(self):
        import httpx

        from app.clients.anthropic_client import ClaudeClient
        from app.memory.brief import BRIEF_KEY, compose_brief

        await self.store.set(BRIEF_KEY, "OLD BRIEF v1")
        claude = ClaudeClient(
            "K", transport=httpx.MockTransport(lambda r: httpx.Response(500, text="down"))
        )
        claude.RETRY_WAIT = 0.01
        self.assertEqual(await compose_brief(claude, self.db, self.store), "")
        self.assertEqual(await self.store.get(BRIEF_KEY), "OLD BRIEF v1")


if __name__ == "__main__":
    unittest.main()
