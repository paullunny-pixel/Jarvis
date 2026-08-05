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
    archived: list = []

    async def board_cards(self, board_id):
        return [{"id": "EX1", "name": "Review Derma Ads", "idList": "L1"}]

    async def update_card(self, card_id, **fields):
        pass

    async def archive_card(self, card_id):
        FakeLayerClient.archived.append(card_id)


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

    async def test_delete_a_card_by_voice_archives_it(self):
        # 17:52, 5 Aug: 'delete this card, I don't need it any more' fell
        # through to conversation — archive is a first-class action now.
        FakeLayerClient.archived = []
        self.responses = [json.dumps({"cards": [
            {"action": "archive", "matchedCardId": "EX1", "matchConfidence": 0.9,
             "title": "Plan and code morning routine with Jarvis",
             "board": "master", "list": None, "domain": None, "priority": None,
             "owner": None, "due": None, "checklist": [], "confidence": 0.9,
             "uncertainties": []}], "unassignedRemarks": []})]
        summary = await self.intake.start_batch("delete the morning routine card please " * 4)
        self.assertIn("ARCHIVE · Plan and code morning routine", summary)
        result = await self.intake.handle_reply("OK")
        self.assertIn("✅ archived", result)
        self.assertEqual(FakeLayerClient.archived, ["EX1"])

    async def test_archive_without_a_match_asks_for_the_title(self):
        self.responses = [json.dumps({"cards": [
            {"action": "archive", "matchedCardId": None, "matchConfidence": 0.0,
             "title": "that old one", "board": "master", "list": None,
             "domain": None, "priority": None, "owner": None, "due": None,
             "checklist": [], "confidence": 0.4,
             "uncertainties": ["Which card should I archive?"]}],
            "unassignedRemarks": []})]
        summary = await self.intake.start_batch("bin that old card " * 8)
        self.assertIn("⚠ Which card", summary)
        result = await self.intake.handle_reply("yes")
        self.assertIn("couldn't archive", result)
        self.assertIn("exact title", result)

    async def test_partial_write_failure_is_precise(self):
        await self.intake.start_batch("ramble " * 30)

        async def broken_create(layer_self, board, list_name, title, **kwargs):
            if "Korea" in title:
                raise RuntimeError("custom field write refused")
            FakeLayer.created.append((board, list_name, title, kwargs))
            return {"id": "NEW1"}

        original = FakeLayer.create_card_full
        FakeLayer.create_card_full = broken_create
        try:
            result = await self.intake.handle_reply("yes")
        finally:
            FakeLayer.create_card_full = original
        self.assertIn("✅ created 'Chase printers", result)
        self.assertIn("⚠️ 'Book flights for Korea' FAILED PARTWAY", result)
        self.assertIn("custom field write refused", result)


class TestConversationIsNotCapture(unittest.IsolatedAsyncioTestCase):
    async def test_replying_to_jarvis_question_never_becomes_a_card(self):
        # 21:54, 5 Aug: 'how are you in yourself?' → Paul's answer became a
        # Trello card proposal. A quote-reply to the bot is conversation.
        from app.clients.deepgram_client import DeepgramClient
        from app.clients.elevenlabs_client import ElevenLabsClient
        from app.clients.telegram_client import TelegramClient
        from app.core.router import JarvisRouter
        from app.heartbeat.jobs import HeartbeatJobs
        from tests.test_telegram_client import text_update

        with tempfile.TemporaryDirectory() as tmp:
            db = SqliteDatabase(os.path.join(tmp, "t.db"))
            await db.init()
            sent = []

            def telegram_handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("sendMessage"):
                    sent.append(request.content.decode())
                return httpx.Response(200, json={"ok": True, "result": {}})

            def claude_handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                if "extract Trello card intents" in body.get("system", ""):
                    return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps({
                        "cards": [{"action": "create", "matchedCardId": None,
                                   "matchConfidence": 0, "title": "Ghost card",
                                   "board": "master", "list": "Inbox", "domain": None,
                                   "priority": None, "owner": None, "due": None,
                                   "checklist": [], "confidence": 0.9, "uncertainties": []}],
                        "unassignedRemarks": []})}]})
                return httpx.Response(200, json={"content": [{"type": "text", "text": "Noted, sir."}]})

            settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=1, _env_file=None)
            claude = ClaudeClient("K", transport=httpx.MockTransport(claude_handler))
            jobs = HeartbeatJobs(
                settings=settings, db=db,
                telegram=TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler)),
                claude=claude,
            )
            router = JarvisRouter(
                settings=settings, db=db, telegram=jobs.telegram, claude=claude,
                deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
                elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
                heartbeat=jobs,
            )
            text = (
                "Positive a little tired but feeling like I'm really close to hitting "
                "some major milestones on my task list and a lot of big decisions"
            )
            update = text_update(text, 1)
            update["message"]["reply_to_message"] = {"from": {"is_bot": True, "id": 9}}
            await router.handle_update(update)
            combined = " ".join(sent)
            self.assertNotIn("Ghost+card", combined)     # no card proposal
            self.assertIn("Noted", combined)             # conversational reply
            # Same text NOT as a reply → capture is allowed to propose.
            sent.clear()
            await router.handle_update(text_update(text, 1))
            self.assertIn("Ghost+card", " ".join(sent))
            await db.close()


class TestRelativeDues(IntakeBase):
    async def test_tomorrow_resolves_instead_of_crashing(self):
        # 00:16, 6 Aug: due 'tomorrow' → Invalid isoformat → card lost. Never again.
        card = dict(EXTRACTION["cards"][0])
        card["due"] = "tomorrow"
        self.responses = [json.dumps({"cards": [card], "unassignedRemarks": []})]
        await self.intake.start_batch("ramble " * 30)
        result = await self.intake.handle_reply("OK")
        self.assertIn("✅ created", result)
        self.assertNotIn("FAILED", result)
        (_, _, _, kwargs) = FakeLayer.created[0]
        self.assertIsNotNone(kwargs["due_local"])   # a real resolved date

    async def test_unparseable_due_creates_without_deadline_honestly(self):
        card = dict(EXTRACTION["cards"][0])
        card["due"] = "whenever suits"
        self.responses = [json.dumps({"cards": [card], "unassignedRemarks": []})]
        await self.intake.start_batch("ramble " * 30)
        result = await self.intake.handle_reply("OK")
        self.assertIn("✅ created", result)
        self.assertIn("WITHOUT a deadline", result)
        (_, _, _, kwargs) = FakeLayer.created[0]
        self.assertIsNone(kwargs["due_local"])

    def test_past_year_iso_bumps_forward(self):
        # 01:01, 6 Aug: the model wrote 2025-08-14 for 'next week' in 2026.
        from zoneinfo import ZoneInfo

        from app.daily12.intake import resolve_due

        now = datetime(2026, 8, 6, 1, 1, tzinfo=ZoneInfo("Asia/Dubai"))
        bumped = resolve_due("2025-08-14", now)
        self.assertEqual((bumped.year, bumped.month, bumped.day), (2026, 8, 14))
        self.assertIsNone(resolve_due("2020-01-01", now))   # nonsense stays out

    async def test_unmatched_owner_reported_not_fatal(self):
        card = dict(EXTRACTION["cards"][0])
        card["owner"] = "Paul, Kiefer"
        self.responses = [json.dumps({"cards": [card], "unassignedRemarks": []})]

        async def create_with_unknown(layer_self, board, list_name, title, **kwargs):
            FakeLayer.created.append((board, list_name, title, kwargs))
            return {"id": "NEW1", "_unassigned_owners": ["Steph"]}

        original = FakeLayer.create_card_full
        FakeLayer.create_card_full = create_with_unknown
        try:
            await self.intake.start_batch("ramble " * 30)
            result = await self.intake.handle_reply("OK")
        finally:
            FakeLayer.create_card_full = original
        self.assertIn("✅ created", result)
        self.assertIn("couldn't assign Steph", result)

    def test_resolve_due_words(self):
        from zoneinfo import ZoneInfo

        from app.daily12.intake import resolve_due

        now = datetime(2026, 8, 6, 0, 16, tzinfo=ZoneInfo("Asia/Dubai"))   # Thursday
        self.assertEqual(resolve_due("tomorrow", now).day, 7)
        self.assertEqual(resolve_due("today", now).hour, 18)
        self.assertEqual(resolve_due("friday", now).weekday(), 4)
        self.assertEqual(resolve_due("2026-08-09T13:00:00Z", now).day, 9)
        self.assertIsNone(resolve_due("whenever suits", now))


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
