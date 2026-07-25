"""Milestone 6: the private sobriety track — supportive, encrypted, walled off."""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.memory.crypto import PrivateBox
from app.memory.embedder import HashEmbedder
from app.memory.store import LivingFacts, MemoryStore
from app.private.service import SOS_OPENER, PrivateTrack
from app.db.sqlite import SqliteDatabase

TODAY = date(2026, 7, 25)


def make_claude(reply="I've got you, Paul.", analysis=None):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "haiku" in payload["model"]:
            return httpx.Response(
                200,
                json={"content": [{"type": "text", "text": json.dumps(analysis or {"mood": 3, "lapse": False, "day_claim": None, "lonely": False})}]},
            )
        return httpx.Response(200, json={"content": [{"type": "text", "text": reply}]})

    return ClaudeClient("K", transport=httpx.MockTransport(handler))


class TestPrivateTrack(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.box = PrivateBox("private-key")
        self.living = LivingFacts(self.db)
        self.memory = MemoryStore(self.db, HashEmbedder(), self.box)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    def track(self, **kw) -> PrivateTrack:
        return PrivateTrack(
            self.db, kw.pop("claude", make_claude(**kw)), self.box,
            memory=self.memory, living=self.living,
        )

    def test_detection(self):
        t = PrivateTrack
        self.assertTrue(t.is_sos("SOS"))
        self.assertTrue(t.is_sos("jarvis, sos"))
        self.assertFalse(t.is_sos("the sos of the villa"))
        self.assertTrue(t.is_private_topic("feeling a craving tonight"))
        self.assertTrue(t.is_private_topic("day 40 sober today"))
        self.assertTrue(t.is_private_topic("nearly slipped up at the airport"))
        self.assertFalse(t.is_private_topic("push the BMI task to friday"))
        self.assertFalse(t.is_private_topic("what's my 12?"))

    async def test_sos_immediate_supportive_flow(self):
        track = self.track()
        reply = await track.respond("SOS", TODAY)
        self.assertEqual(reply, SOS_OPENER)
        row = await self.db.fetch_one("SELECT * FROM sobriety WHERE kind = 'sos'")
        self.assertIsNotNone(row)
        self.assertTrue(row["content"].startswith("enc:"))  # encrypted at rest

    async def test_checkin_recorded_encrypted_with_day_count(self):
        await self.living.set("sobriety.start_date", (TODAY - timedelta(days=41)).isoformat(), room="private")
        track = self.track()
        reply = await track.respond("feeling steady about staying sober tonight", TODAY)
        self.assertTrue(reply)
        row = await self.db.fetch_one("SELECT * FROM sobriety WHERE kind = 'checkin'")
        self.assertEqual(row["day_count"], 41)
        self.assertEqual(row["mood"], 3)
        self.assertTrue(row["content"].startswith("enc:"))
        self.assertNotIn("sober", row["content"])
        # living fact refreshed for the cockpit panel
        self.assertEqual(await self.living.get("sobriety.days"), "41")

    async def test_day_claim_sets_start_date(self):
        track = self.track(analysis={"mood": 4, "lapse": False, "day_claim": 30, "lonely": False})
        await track.respond("day 30 sober today, feeling good", TODAY)
        self.assertEqual(await track.day_count(TODAY), 30)
        row = await self.db.fetch_one("SELECT kind FROM sobriety ORDER BY id DESC LIMIT 1")
        self.assertEqual(row["kind"], "milestone")  # 30 is a milestone

    async def test_lapse_resets_supportively(self):
        await self.living.set("sobriety.start_date", (TODAY - timedelta(days=10)).isoformat(), room="private")
        track = self.track(analysis={"mood": 2, "lapse": True, "day_claim": None, "lonely": False})
        reply = await track.respond("I slipped up last night", TODAY)
        self.assertTrue(reply)
        self.assertEqual(await track.day_count(TODAY), 0)
        row = await self.db.fetch_one("SELECT * FROM sobriety WHERE kind = 'reset'")
        self.assertIsNotNone(row)

    async def test_private_context_included_but_business_excluded(self):
        await self.memory.add_chunk(
            "feeling lonely in the evening is a major trigger for Paul",
            room="private", type_="PRIVATE",
        )
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if "haiku" in payload["model"]:
                return httpx.Response(200, json={"content": [{"type": "text", "text": '{"mood":2,"lapse":false,"day_claim":null,"lonely":true}'}]})
            captured["system"] = payload["system"]
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Here for you."}]})

        track = PrivateTrack(
            self.db, ClaudeClient("K", transport=httpx.MockTransport(handler)),
            self.box, memory=self.memory, living=self.living,
        )
        await track.respond("feeling really lonely this evening and tempted", TODAY)
        self.assertIn("major trigger for Paul", captured["system"])
        self.assertIn("never lecture", captured["system"])
        self.assertNotIn("Daily 12", captured["system"].split("Private memory")[-1])

    async def test_claude_failure_still_supportive(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="down")

        track = PrivateTrack(
            self.db, ClaudeClient("K", transport=httpx.MockTransport(handler)),
            self.box, living=self.living,
        )
        reply = await track.respond("craving tonight", TODAY)
        self.assertIn("I'm here", reply)

    async def test_trigger_message_on_travel_day(self):
        await self.living.set("sobriety.start_date", (TODAY - timedelta(days=20)).isoformat(), room="private")
        track = self.track()
        msg = await track.trigger_message(["Flight EK18 MAN-DXB"], TODAY)
        self.assertIn("between us", msg)
        self.assertIn("Day 20", msg)
        self.assertEqual(await track.trigger_message([], TODAY), "")


if __name__ == "__main__":
    unittest.main()
