"""Phase 3 voice intake (5 Aug spec): extraction context, dedup verification,
the confirm-before-write loop, honest partial writes, accuracy tracking."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.config import Settings
from app.core.store import SettingsStore
from app.daily12.intake import (
    IntakeParked, IntakeReminder, VoiceIntake, title_similarity,
)
from app.db.sqlite import SqliteDatabase

EXTRACTION = {
    "cards": [
        {"action": "create", "matchedCardId": None, "matchConfidence": 0.0,
         "title": "Chase printers for Dubai brochures", "description": None,
         "board": "harry", "list": "Harry Today", "domain": "Derma",
         "priority": "P2", "owner": "Harry", "due": "2026-08-07T13:00:00Z",
         "checklist": [{"text": "Call printer", "owner": None, "due": None},
                       {"text": "Send artwork", "owner": None, "due": None}],
         "confidence": 0.9, "uncertainties": []},
        {"action": "create", "matchedCardId": None, "matchConfidence": 0.0,
         "title": "Book flights for Korea", "description": None,
         "board": "master", "list": "This Week", "domain": "Personal",
         "priority": "P4", "owner": "Paul", "due": None, "checklist": [],
         "confidence": 0.8, "uncertainties": ["No deadline stated — leave open?"]},
    ],
    "unassignedRemarks": ["something about the villa"],
}


class FakeLayerClient:
    async def board_cards(self, board_id):
        return [{"id": "EX1", "name": "Review Derma Ads", "idList": "L1"}]

    async def update_card(self, card_id, **fields):
        pass


class FakeBoardMap:
    id = "B1"
    lists = {"Paul Today": "L1"}


class FakeLayer:
    client = FakeLayerClient()
    created: list = []

    def board(self, key):
        return FakeBoardMap()

    async def create_card_full(self, board, list_name, title, **kwargs):
        FakeLayer.created.append((board, list_name, title, kwargs))
        return {"id": "NEW1"}

    async def set_domain(self, *a):
        pass

    async def set_priority(self, *a):
        pass

    async def move_card(self, *a):
        pass

    def _due_utc(self, d):
        return "X"


class IntakeBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        FakeLayer.created = []
        self.responses = [json.dumps(EXTRACTION)]

        def handler(request: httpx.Request) -> httpx.Response:
            text = self.responses.pop(0) if self.responses else "{}"
            return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})

        self.claude = ClaudeClient("K", transport=httpx.MockTransport(handler))

        async def factory():
            return FakeLayer()

        self.intake = VoiceIntake(
            self.claude, SettingsStore(self.db),
            Settings(telegram_bot_token="T", _env_file=None), factory,
        )

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()


class TestExtractionAndSummary(IntakeBase):
    async def test_batch_summary_is_numbered_with_routing_and_flags(self):
        summary = await self.intake.start_batch("long ramble about work " * 10)
        self.assertIn("Got 2 things", summary)
        self.assertIn("1. NEW · Chase printers", summary)
        self.assertIn("→ Paul x Harry / Harry Today", summary)
        self.assertIn("☐ Call printer", summary)
        self.assertIn("⚠ No deadline stated", summary)
        self.assertIn("couldn't place", summary)          # unassignedRemarks surface
        self.assertIn("Reply OK to write", summary)
        self.assertIsNotNone(await self.intake.pending())

    async def test_no_actionable_content_creates_nothing(self):
        self.responses = ['{"cards": [], "unassignedRemarks": ["chat"]}']
        self.assertIsNone(await self.intake.start_batch("just chatting"))
        self.assertIsNone(await self.intake.pending())

    async def test_uncertain_dedup_downgrades_to_ask(self):
        card = dict(EXTRACTION["cards"][0])
        card.update({"action": "update", "matchedCardId": "EX1",
                     "matchConfidence": 0.5, "title": "Something unrelated"})
        self.responses = [json.dumps({"cards": [card], "unassignedRemarks": []})]
        summary = await self.intake.start_batch("ramble " * 30)
        self.assertIn("NEW ·", summary)                   # downgraded, not merged
        self.assertIn("Same as existing 'Review Derma Ads'", summary)

    def test_title_similarity_separates_near_misses(self):
        self.assertGreater(title_similarity("Review Derma Ads", "review the derma ads"), 0.6)
        self.assertLess(title_similarity("Review Derma Ads", "Book Korea flights"), 0.4)


class TestConfirmLoop(IntakeBase):
    async def test_ok_writes_all_and_reports_per_card(self):
        await self.intake.start_batch("ramble " * 30)
        result = await self.intake.handle_reply("OK")
        self.assertIn("✅ created 'Chase printers for Dubai brochures'", result)
        self.assertIn("✅ created 'Book flights for Korea'", result)
        self.assertEqual(len(FakeLayer.created), 2)
        self.assertIsNone(await self.intake.pending())    # batch cleared

    async def test_cancel_scraps_without_writing(self):
        await self.intake.start_batch("ramble " * 30)
        result = await self.intake.handle_reply("cancel")
        self.assertIn("nothing written", result.lower())
        self.assertEqual(FakeLayer.created, [])

    async def test_correction_rerenders_and_loops(self):
        await self.intake.start_batch("ramble " * 30)
        corrected = json.loads(json.dumps(EXTRACTION))
        corrected["cards"][0]["priority"] = "P1"
        del corrected["cards"][1]
        self.responses = [json.dumps(corrected)]
        summary = await self.intake.handle_reply("1 should be P1 and drop 2")
        self.assertIn("Got 1 thing", summary)
        self.assertIn("P1", summary)
        self.assertIsNotNone(await self.intake.pending())  # still awaiting OK

    async def test_no_pending_means_none(self):
        self.assertIsNone(await self.intake.handle_reply("OK"))

    async def test_partial_write_failure_is_precise(self):
        await self.intake.start_batch("ramble " * 30)

        async def broken_create(layer_self, board, list_name, title, **kwargs):
            if "Korea" in title:
                raise RuntimeError("custom field write refused")
            FakeLayer.created.append((board, list_name, title, kwargs))
            return {"id": "NEW1"}

        FakeLayer.create_card_full = broken_create
        try:
            result = await self.intake.handle_reply("yes")
        finally:
            del FakeLayer.create_card_full   # restore class default
        self.assertIn("✅ created 'Chase printers", result)
        self.assertIn("⚠️ 'Book flights for Korea' FAILED PARTWAY", result)
        self.assertIn("custom field write refused", result)


class TestTimeoutAndAccuracy(IntakeBase):
    async def test_reminds_then_parks_never_drops(self):
        await self.intake.start_batch("ramble " * 30)
        pend = await self.intake.pending()
        created = datetime.fromisoformat(pend["created"])
        with self.assertRaises(IntakeReminder):
            await self.intake.remind_or_park(created + timedelta(hours=4))
        with self.assertRaises(IntakeParked):
            await self.intake.remind_or_park(created + timedelta(hours=7))
        self.assertIsNone(await self.intake.pending())
        summary = await self.intake.unpark()               # retrievable, not lost
        self.assertIn("Chase printers", summary)
        self.assertIsNotNone(await self.intake.pending())

    async def test_accuracy_report_counts_corrections(self):
        await self.intake.start_batch("ramble " * 30)
        corrected = json.loads(json.dumps(EXTRACTION))
        corrected["cards"][0]["priority"] = "P1"
        self.responses = [json.dumps(corrected)]
        await self.intake.handle_reply("1 is P1")
        await self.intake.handle_reply("OK")
        report = await self.intake.accuracy_report()
        self.assertIn("priority", report)
        self.assertIn("1 corrections", report)
        self.assertIn("owner", report)
