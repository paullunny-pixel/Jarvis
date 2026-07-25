import os
import tempfile
import unittest

from app.core.store import SettingsStore
from app.memory.crypto import PrivateBox
from app.memory.embedder import HashEmbedder, cosine
from app.memory.seed import LIVING_SEED, PRIVATE_CHUNKS, STABLE_CHUNKS, load_day_one_brain
from app.memory.store import LivingFacts, MemoryStore
from app.db.sqlite import SqliteDatabase


class MemoryBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.memory = MemoryStore(self.db, HashEmbedder(), PrivateBox("test-key"))
        self.living = LivingFacts(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()


class TestEmbedder(unittest.IsolatedAsyncioTestCase):
    async def test_similar_texts_are_closer(self):
        embedder = HashEmbedder()
        [a, b, c] = await embedder.embed(
            [
                "BMI manufacture the dermal fillers under contract",
                "the BMI contract covers dermal fillers manufacturing",
                "book a table for dinner on Tuesday",
            ]
        )
        self.assertGreater(cosine(a, b), cosine(a, c))

    async def test_deterministic(self):
        embedder = HashEmbedder()
        [v1] = await embedder.embed(["villa payment"])
        [v2] = await embedder.embed(["villa payment"])
        self.assertEqual(v1, v2)


class TestMemoryStore(MemoryBase):
    async def test_add_and_search_by_meaning(self):
        await self.memory.add_chunk(
            "BMI makes the dermal fillers for Prodermis under a high-pressure contract",
            room="companies", tags=["bmi"],
        )
        await self.memory.add_chunk("Paul runs 5km every morning", room="health")
        hits = await self.memory.search("who manufactures the fillers?", k=1)
        self.assertIn("BMI", hits[0]["content"])

    async def test_room_scoping(self):
        await self.memory.add_chunk("villa payment fact", room="finances")
        await self.memory.add_chunk("villa on trello board", room="companies")
        hits = await self.memory.search("villa payment", rooms=["finances"])
        self.assertTrue(all(h["room"] == "finances" for h in hits))

    async def test_private_wall_default_exclusion(self):
        await self.memory.add_chunk("sobriety day count is climbing", room="private", type_="PRIVATE")
        await self.memory.add_chunk("sobriety documentary on netflix", room="you")
        hits = await self.memory.search("sobriety")
        self.assertTrue(all(h["room"] != "private" for h in hits))
        self.assertTrue(all("day count" not in h["content"] for h in hits))

    async def test_private_room_readable_only_with_explicit_flag(self):
        await self.memory.add_chunk("trigger: trade shows are high-risk", room="private", type_="PRIVATE")
        hits = await self.memory.search("trade show triggers", include_private=True, rooms=["private"])
        self.assertEqual(len(hits), 1)
        self.assertIn("high-risk", hits[0]["content"])

    async def test_private_content_encrypted_at_rest(self):
        await self.memory.add_chunk("very sensitive fact", room="private", type_="PRIVATE")
        raw = await self.db.fetch_one("SELECT content FROM memory_chunks WHERE is_private = 1")
        self.assertTrue(raw["content"].startswith("enc:"))
        self.assertNotIn("sensitive", raw["content"])

    async def test_private_type_in_business_room_still_walled(self):
        await self.memory.add_chunk("family matter", room="people", type_="PRIVATE")
        hits = await self.memory.search("family matter")
        self.assertEqual(hits, [])

    async def test_supersede_keeps_history_hides_old(self):
        old_id = await self.memory.add_chunk("Paul weighs 89kg", room="health", type_="LIVING")
        await self.memory.supersede(old_id, "Paul weighs 87kg")
        hits = await self.memory.search("Paul weight kg")
        contents = [h["content"] for h in hits]
        self.assertIn("Paul weighs 87kg", contents)
        self.assertNotIn("Paul weighs 89kg", contents)
        old_row = await self.db.fetch_one("SELECT superseded_by FROM memory_chunks WHERE id = ?", (old_id,))
        self.assertNotEqual(old_row["superseded_by"], 0)

    async def test_audit_lists_current_knowledge(self):
        await self.memory.add_chunk("fact one", room="you")
        await self.memory.add_chunk("private fact", room="private", type_="PRIVATE")
        listing = await self.memory.audit()
        self.assertEqual(len(listing), 1)
        listing_private = await self.memory.audit(include_private=True)
        self.assertEqual(len(listing_private), 2)
        self.assertIn("private fact", [r["content"] for r in listing_private])


class TestLivingFacts(MemoryBase):
    async def test_update_in_place_with_history(self):
        await self.living.set("health.weight_kg", "88.7", room="health")
        await self.living.set("health.weight_kg", "87.2", room="health")
        self.assertEqual(await self.living.get("health.weight_kg"), "87.2")
        current = await self.living.all_current()
        self.assertEqual(len([f for f in current if f["key"] == "health.weight_kg"]), 1)
        history = await self.living.history("health.weight_kg")
        self.assertEqual([h["value"] for h in history], ["88.7"])

    async def test_private_room_facts_excluded_by_default(self):
        await self.living.set("sobriety.day_count", "41", room="private")
        await self.living.set("villa.paid", "27%", room="finances")
        keys = [f["key"] for f in await self.living.all_current()]
        self.assertIn("villa.paid", keys)
        self.assertNotIn("sobriety.day_count", keys)


class TestSeed(MemoryBase):
    async def test_seed_loads_once(self):
        store = SettingsStore(self.db)
        n1 = await load_day_one_brain(self.memory, self.living, store)
        self.assertEqual(n1, len(STABLE_CHUNKS) + len(PRIVATE_CHUNKS))
        n2 = await load_day_one_brain(self.memory, self.living, store)
        self.assertEqual(n2, 0)
        row = await self.db.fetch_one("SELECT COUNT(*) AS n FROM memory_chunks")
        self.assertEqual(row["n"], n1)

    async def test_seeded_brain_answers_business_questions(self):
        await load_day_one_brain(self.memory, self.living, SettingsStore(self.db))
        hits = await self.memory.search("who manufactures the Prodermis dermal fillers?", k=3)
        self.assertTrue(any("BMI" in h["content"] for h in hits))

    async def test_seeded_private_stays_private(self):
        await load_day_one_brain(self.memory, self.living, SettingsStore(self.db))
        hits = await self.memory.search("sobriety triggers trade shows loneliness", k=10)
        self.assertTrue(all(h["room"] != "private" for h in hits))
        living_keys = [f["key"] for f in await self.living.all_current()]
        self.assertTrue(all(not k.startswith("sobriety") for k in living_keys))
        # and the family/sobriety chunks ARE there when the private scope asks
        private_hits = await self.memory.search(
            "sobriety triggers", include_private=True, rooms=["private"], k=5
        )
        self.assertTrue(any("trade shows" in h["content"] for h in private_hits))

    async def test_seed_living_facts_present(self):
        await load_day_one_brain(self.memory, self.living, SettingsStore(self.db))
        self.assertIn("27%", await self.living.get("villa.paid"))
        self.assertEqual(len(await self.living.all_current(exclude_private=False)), len(LIVING_SEED))


class TestPrivateBox(unittest.TestCase):
    def test_roundtrip(self):
        box = PrivateBox("secret")
        sealed = box.seal("day 41")
        self.assertTrue(sealed.startswith("enc:"))
        self.assertEqual(box.open(sealed), "day 41")

    def test_wrong_key_fails(self):
        from cryptography.fernet import InvalidToken

        sealed = PrivateBox("secret").seal("day 41")
        with self.assertRaises(InvalidToken):
            PrivateBox("other").open(sealed)

    def test_no_key_degrades_to_plaintext(self):
        box = PrivateBox("")
        self.assertFalse(box.active)
        self.assertEqual(box.seal("x"), "x")
        self.assertEqual(box.open("x"), "x")


if __name__ == "__main__":
    unittest.main()
