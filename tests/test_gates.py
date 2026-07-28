"""The non-skippables: run + meds gate the working day until confirmed,
with photo proof of the run verified by vision."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.telegram_client import TelegramClient
from app.config import Settings
from app.core.router import JarvisRouter
from app.daily12.service import Daily12Service
from app.heartbeat.gates import GateKeeper, mentions_meds
from app.heartbeat.streaks import Streaks
from app.db.sqlite import SqliteDatabase
from tests.test_photo_vision import photo_update
from tests.test_telegram_client import text_update

OWNER = 111
TZ = ZoneInfo("Europe/London")
TODAY = date(2026, 7, 25)


def at(hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime(2026, 7, 25, hour, minute, tzinfo=TZ)


class TestGateKeeper(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.streaks = Streaks(self.db)
        self.gates = GateKeeper(self.db, self.streaks)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_nothing_outstanding_before_deadlines(self):
        self.assertEqual(await self.gates.outstanding(at("07:15")), [])

    async def test_both_outstanding_after_deadlines(self):
        outstanding = await self.gates.outstanding(at("10:00"))
        self.assertEqual({g["id"] for g in outstanding}, {"run", "meds"})

    async def test_run_gate_follows_the_streak(self):
        await self.streaks.record("run", TODAY)
        outstanding = await self.gates.outstanding(at("10:00"))
        self.assertEqual([g["id"] for g in outstanding], ["meds"])

    async def test_meds_confirm_clears_gate(self):
        await self.gates.confirm("meds", TODAY)
        await self.streaks.record("run", TODAY)
        self.assertEqual(await self.gates.outstanding(at("12:00")), [])

    def test_meds_detection(self):
        self.assertTrue(mentions_meds("meds done"))
        self.assertTrue(mentions_meds("taken my supplements and TRT"))
        self.assertTrue(mentions_meds("vitamins gone in"))
        self.assertFalse(mentions_meds("need to order more vitamins"))
        self.assertFalse(mentions_meds("what's my 12?"))

    def test_block_message_mentions_photo_proof_for_run(self):
        message = GateKeeper.block_message(
            [{"id": "run", "label": "the 5km run", "by": "09:30"}]
        )
        self.assertIn("photo", message.lower())

    async def test_declared_rest_day_opens_the_run_gate(self):
        # The production bug: Paul logged a rest day at 08:35 and the board
        # still demanded the 5km at 11:41. Recovery is valid training (§11).
        await self.streaks.record_recovery(TODAY)
        outstanding = await self.gates.outstanding(at("11:41"))
        self.assertEqual([g["id"] for g in outstanding], ["meds"])  # run gate open

    async def test_overridden_gate_stays_open_all_day(self):
        await self.gates.override(["run"], "physio said rest today", TODAY)
        outstanding = await self.gates.outstanding(at("10:00"))
        self.assertEqual([g["id"] for g in outstanding], ["meds"])
        rows = await self.db.fetch_all("SELECT item, reason FROM override_log")
        self.assertEqual(rows[0]["item"], "run")
        self.assertIn("physio", rows[0]["reason"])


ALWAYS_BLOCKED = json.dumps(
    [{"id": "run", "label": "the 5km run", "by": "00:00"},
     {"id": "meds", "label": "supplements & medication", "by": "00:00"}]
)
NEVER_BLOCKED = json.dumps(
    [{"id": "run", "label": "the 5km run", "by": "23:59"},
     {"id": "meds", "label": "supplements & medication", "by": "23:59"}]
)


class TestUniversalOverride(unittest.IsolatedAsyncioTestCase):
    """Master Update §1: 'override' releases any block — one confirm, logged."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        from app.core.store import SettingsStore

        self.store = SettingsStore(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_override_releases_after_one_confirm(self):
        h = GatedHarness(self.db)
        await self.store.set("gates_config", ALWAYS_BLOCKED)
        await h.router.handle_update(text_update("override", OWNER))
        self.assertIn("You sure?", " ".join(h.texts()))
        await h.router.handle_update(text_update("flight day, zero time this morning", OWNER))
        self.assertIn("released", " ".join(h.texts()).lower())
        # The override marks the REAL today (router clock), not the fixture date.
        noon_today = datetime.now(TZ).replace(hour=12, minute=0)
        self.assertEqual(await h.gates.outstanding(noon_today), [])
        rows = await self.db.fetch_all("SELECT item, reason FROM override_log")
        self.assertEqual({r["item"] for r in rows}, {"run", "meds"})
        self.assertIn("flight day", rows[0]["reason"])

    async def test_override_can_be_called_off(self):
        h = GatedHarness(self.db)
        await self.store.set("gates_config", ALWAYS_BLOCKED)
        await h.router.handle_update(text_update("override jarvis", OWNER))
        await h.router.handle_update(text_update("no, leave it — I'll do the run", OWNER))
        self.assertIn("stays in place", " ".join(h.texts()).lower())
        noon_today = datetime.now(TZ).replace(hour=12, minute=0)
        self.assertEqual(len(await h.gates.outstanding(noon_today)), 2)

    async def test_override_with_nothing_blocked(self):
        h = GatedHarness(self.db)
        await self.store.set("gates_config", NEVER_BLOCKED)
        await h.router.handle_update(text_update("override", OWNER))
        self.assertIn("already open", " ".join(h.texts()).lower())

    async def test_conversational_move_on_is_not_hijacked(self):
        h = GatedHarness(self.db)
        await self.store.set("gates_config", NEVER_BLOCKED)
        await h.router.handle_update(
            text_update("right, let's move on to the villa numbers", OWNER)
        )
        combined = " ".join(h.texts())
        self.assertNotIn("You sure?", combined)   # fell through to conversation
        self.assertIn("Very good, sir.", combined)


class GatedHarness:
    """Router + gates + a minimal Daily12 stub over mocked HTTP."""

    def __init__(self, db, vision_json=None):
        self.telegram_calls = []

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.split("/")[-1]
            self.telegram_calls.append((method, request.content))
            if method == "getFile":
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "p.jpg"}})
            if "/file/" in request.url.path:
                return httpx.Response(200, content=b"\xff\xd8JPEG")
            return httpx.Response(200, json={"ok": True, "result": {}})

        def claude_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            content = payload["messages"][0].get("content")
            has_image = isinstance(content, list) and content and content[0].get("type") == "image"
            if "haiku" in payload["model"]:
                if has_image:  # the run-stats vision check
                    return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(
                        vision_json or {"is_run_stats": False, "distance_km": 0, "duration_min": 0}
                    )}]})
                return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(
                    {"run": "na", "workout": "na", "meals": "na", "medication": "done"}
                )}]})
            return httpx.Response(200, json={"content": [{"type": "text", "text": "Very good, sir."}]})

        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        claude = ClaudeClient("K", transport=httpx.MockTransport(claude_handler))
        streaks = Streaks(db)
        self.gates = GateKeeper(db, streaks)

        def trello_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        from app.daily12.trello import TrelloClient

        self.router = JarvisRouter(
            settings=settings,
            db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler)),
            claude=claude,
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            daily12=Daily12Service(db, TrelloClient("K", "T", transport=httpx.MockTransport(trello_handler)), claude),
            gates=self.gates,
        )

    def texts(self):
        return [
            parse_qs(body.decode())["text"][0]
            for method, body in self.telegram_calls
            if method == "sendMessage"
        ]


class TestGatedRouter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_board_blocked_while_gates_outstanding(self):
        # Assumes tests run at a time past the 09:30 deadline is NOT guaranteed,
        # so force deadlines that have always passed.
        h = GatedHarness(self.db)
        from app.core.store import SettingsStore

        await SettingsStore(self.db).set(
            "gates_config",
            json.dumps([{"id": "run", "label": "the 5km run", "by": "00:00"},
                        {"id": "meds", "label": "supplements & medication", "by": "00:00"}]),
        )
        await h.router.handle_update(text_update("what's my 12?", OWNER))
        combined = " ".join(h.texts())
        self.assertIn("aren't skippable", combined)
        self.assertNotIn("THE DAILY 12", combined)

    async def test_meds_confirmation_opens_meds_gate(self):
        h = GatedHarness(self.db)
        await h.router.handle_update(text_update("meds and vitamins taken", OWNER))
        today = datetime.now(TZ).date()
        self.assertTrue(await h.gates.is_confirmed("meds", today))
        self.assertTrue(any("Meds confirmed" in t for t in h.texts()))

    async def test_run_photo_proof_opens_run_gate_with_real_stats(self):
        h = GatedHarness(self.db, vision_json={"is_run_stats": True, "distance_km": 5.24, "duration_min": 28})
        await h.router.handle_update(photo_update("there's the run"))
        today = datetime.now(TZ).date()
        self.assertTrue(await h.gates.is_confirmed("run", today))
        run = await self.db.fetch_one("SELECT * FROM runs")
        self.assertEqual(run["source"], "photo_proof")
        self.assertAlmostEqual(run["distance_km"], 5.24)
        self.assertTrue(any("5.24" in t for t in h.texts()))

    async def test_short_run_photo_does_not_open_gate(self):
        h = GatedHarness(self.db, vision_json={"is_run_stats": True, "distance_km": 2.1, "duration_min": 12})
        await h.router.handle_update(photo_update("does this count?"))
        today = datetime.now(TZ).date()
        self.assertFalse(await h.gates.is_confirmed("run", today))
        self.assertIsNone(await self.db.fetch_one("SELECT * FROM runs"))

    async def test_non_stats_photo_falls_through_to_conversation(self):
        h = GatedHarness(self.db)  # vision says not run stats
        await h.router.handle_update(photo_update("look at this"))
        self.assertTrue(any("Very good, sir." in t for t in h.texts()))


MEDS_ONLY_BLOCKED = json.dumps(
    [{"id": "meds", "label": "supplements & medication", "by": "00:00"}]
)
RUN_ONLY_BLOCKED = json.dumps(
    [{"id": "run", "label": "the 5km run", "by": "00:00"}]
)


class TestGatedRequestReplay(unittest.IsolatedAsyncioTestCase):
    """The production bug (28 Jul): a voice note's instructions died at the
    run gate, and opening the gate later never actioned them. The gate must
    queue instructions and replay them the moment it opens."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        from app.core.store import SettingsStore

        self.store = SettingsStore(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_blocked_instructions_are_queued_not_lost(self):
        h = GatedHarness(self.db)
        await self.store.set("gates_config", ALWAYS_BLOCKED)
        await h.router.handle_update(
            text_update("stick a card on the board for the VAT return", OWNER)
        )
        self.assertIn("queued", " ".join(h.texts()))
        stash = json.loads(await self.store.get("gated_request"))
        self.assertIn("VAT return", stash["text"])

    async def test_second_blocked_ask_joins_the_queue(self):
        h = GatedHarness(self.db)
        await self.store.set("gates_config", ALWAYS_BLOCKED)
        await h.router.handle_update(
            text_update("stick a card on the board for the VAT return", OWNER)
        )
        await h.router.handle_update(
            text_update("and a card for the BMI stock order", OWNER)
        )
        stash = json.loads(await self.store.get("gated_request"))
        self.assertIn("VAT return", stash["text"])
        self.assertIn("BMI stock", stash["text"])

    async def test_meds_confirm_replays_the_queued_request(self):
        h = GatedHarness(self.db)
        await self.store.set("gates_config", MEDS_ONLY_BLOCKED)
        await h.router.handle_update(
            text_update("put a card on the board for the VAT return", OWNER)
        )
        self.assertIn("queued", " ".join(h.texts()))
        await h.router.handle_update(text_update("meds and vitamins taken", OWNER))
        self.assertIn("back to what you asked", " ".join(h.texts()))
        self.assertEqual(await self.store.get("gated_request", ""), "")

    async def test_rest_day_replays_like_production(self):
        from tests.test_heartbeat import JobsHarness

        h = GatedHarness(self.db)
        h.router.heartbeat = JobsHarness(self.db).jobs
        await self.store.set("gates_config", RUN_ONLY_BLOCKED)
        await h.router.handle_update(
            text_update("add the BMI order to my trello", OWNER)
        )
        self.assertIn("queued", " ".join(h.texts()))
        await h.router.handle_update(text_update("I'm having a rest day", OWNER))
        combined = " ".join(h.texts())
        self.assertIn("Recovery day logged", combined)
        self.assertIn("back to what you asked", combined)
        self.assertEqual(await self.store.get("gated_request", ""), "")

    async def test_replay_holds_until_every_gate_is_open(self):
        h = GatedHarness(self.db)
        await self.store.set("gates_config", ALWAYS_BLOCKED)
        await h.router.handle_update(
            text_update("put a card on the board for the VAT return", OWNER)
        )
        await h.router.handle_update(text_update("meds and vitamins taken", OWNER))
        # The run gate is still shut — the queue holds rather than half-fires.
        self.assertNotIn("back to what you asked", " ".join(h.texts()))
        self.assertTrue(await self.store.get("gated_request", ""))

    async def test_yesterdays_stash_never_replays(self):
        h = GatedHarness(self.db)
        await self.store.set("gates_config", NEVER_BLOCKED)
        await self.store.set(
            "gated_request", json.dumps({"date": "2026-07-27", "text": "old ask"})
        )
        await h.router.handle_update(text_update("meds and vitamins taken", OWNER))
        self.assertNotIn("back to what you asked", " ".join(h.texts()))
        self.assertEqual(await self.store.get("gated_request", ""), "")


if __name__ == "__main__":
    unittest.main()
