"""Phase 1 Trello layer (5 Aug spec): bootstrap by name, per-board option
guards, aliases, urgent label, timezone conversion, time-in-list."""
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx

from app.daily12.trello import TrelloClient
from app.daily12.trello_layer import (
    ResolutionError, TrelloLayer, classify_domain, _ordinal,
)

MASTER = "6a2a8b7f8eafad960b8f7a33"
HARRY = "6a731cbe38c30479c35caecc"


def board_fixture(board_id):
    personal = board_id == MASTER
    domain_opts = [
        {"id": f"opt-{n}", "value": {"text": n}}
        for n in (["Personal"] if personal else [])
        + ["Prodermis", "Derma", "Derma EU", "Business Ops"]
        + (["Aesthetics Supply"] if personal else [])
    ]
    return {
        "lists": [
            {"id": f"{board_id}-inbox", "name": "Inbox"},
            {"id": f"{board_id}-today", "name": "Paul Today"},
            {"id": f"{board_id}-partner", "name": "Kiefer Today" if personal else "Harry Today"},
            {"id": f"{board_id}-old", "name": "Old Stuff (Archive)"},
        ],
        "customFields": [
            {"id": f"{board_id}-domain", "name": "Domain", "options": domain_opts},
            {"id": f"{board_id}-priority", "name": "Priority", "options": [
                {"id": f"opt-P{n}", "value": {"text": f"P{n}"}} for n in range(1, 6)]},
            {"id": f"{board_id}-entered", "name": "Entered List On", "options": None},
        ],
        "labels": [{"id": f"{board_id}-urgent", "name": "Urgent"}, {"id": "x", "name": ""}],
        "members": [{"id": "58189b548fc170379eca1937", "fullName": "Paul Lunny"},
                    {"id": "59bfdd4a3b5e18a719885d42", "fullName": "Kiefer Brindle"}],
    }


class Harness:
    def __init__(self):
        self.requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            path = request.url.path
            for board_id in (MASTER, HARRY):
                for part in ("lists", "customFields", "labels", "members"):
                    if path == f"/1/boards/{board_id}/{part}":
                        return httpx.Response(200, json=board_fixture(board_id)[part])
            if path == "/1/cards" and request.method == "POST":
                return httpx.Response(200, json={"id": "68919f2f" + "a" * 16, "name": "x"})
            if "/actions" in path:
                return httpx.Response(200, json=[
                    {"date": "2026-08-05T10:00:00.000Z", "data": {"listAfter": {"name": "Paul Today"}}},
                ])
            if "/checklists" in path and request.method == "POST":
                return httpx.Response(200, json={"id": "CHK1"})
            return httpx.Response(200, json={"ok": True})

        self.layer = TrelloLayer(TrelloClient("K", "T", transport=httpx.MockTransport(handler)))

    def paths(self):
        return [(r.method, r.url.path, dict(parse_qs(r.url.query.decode()))) for r in self.requests]


class TestTrelloLayer(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()
        await self.h.layer.bootstrap()

    async def test_bootstrap_resolves_by_name_and_separates_archives(self):
        master = self.h.layer.board("master")
        self.assertIn("Inbox", master.lists)
        # Paul's cleanup override (5 Aug eve): archive lists readable, but
        # never a routing destination.
        self.assertNotIn("Old Stuff (Archive)", master.lists)
        self.assertIn("Old Stuff (Archive)", master.archive_lists)
        self.assertIn("Domain", master.fields)
        self.assertIn("Personal", master.options["Domain"])
        self.assertIn("Urgent", master.labels)
        self.assertIn("Paul Lunny", master.members)

    async def test_option_sets_differ_per_board_and_fail_loudly(self):
        harry = self.h.layer.board("harry")
        self.assertNotIn("Personal", harry.options["Domain"])
        with self.assertRaises(ResolutionError) as ctx:
            await self.h.layer.set_domain("harry", "CARD1", "Personal")
        self.assertIn("available", str(ctx.exception))   # loud, with the real options

    async def test_domain_aliases_and_unresolved_ask_first(self):
        self.assertEqual(classify_domain("Chase Prime Derm restock"), ("Aesthetics Supply", None))
        self.assertEqual(classify_domain("Sculptide invoice"), ("Aesthetics Supply", None))
        self.assertEqual(classify_domain("Exobelle launch"), ("Prodermis", None))
        self.assertEqual(classify_domain("Derma Direct order"), ("Derma", None))
        self.assertEqual(classify_domain("sort the wages run"), ("Business Ops", None))
        domain, unresolved = classify_domain("BMI paperwork for the clinic")
        self.assertIsNone(domain)
        self.assertEqual(unresolved, "bmi")   # ask Paul, never guess

    async def test_priority_p2_applies_urgent_and_p4_removes_it(self):
        await self.h.layer.set_priority("master", "CARD1", "P2")
        adds = [p for p in self.h.paths() if p[0] == "POST" and "idLabels" in p[1]]
        self.assertTrue(adds)
        await self.h.layer.set_priority("master", "CARD1", "P4")
        removes = [p for p in self.h.paths() if p[0] == "DELETE" and "idLabels" in p[1]]
        self.assertTrue(removes)

    async def test_due_converts_dubai_to_utc(self):
        # 09:00 Dubai = 05:00 UTC — step 5 of the bug-test sequence.
        card = await self.h.layer.create_card_full(
            "master", "Inbox", "Test", due_local=datetime(2026, 8, 10, 9, 0)
        )
        self.assertTrue(card["id"])
        create = next(p for p in self.h.paths() if p[0] == "POST" and p[1] == "/1/cards")
        self.assertEqual(create[2]["due"], ["2026-08-10T05:00:00.000Z"])

    async def test_create_full_stamps_entered_and_builds_checklist(self):
        await self.h.layer.create_card_full(
            "master", "Paul Today", "Big one", owner_name="Kiefer Brindle",
            domain="Prodermis", priority="P2",
            checklist=[{"name": "Step one", "member": "Paul Lunny",
                        "due_local": datetime(2026, 8, 10, 9, 0)}],
        )
        paths = self.h.paths()
        self.assertTrue(any("customField" in p[1] and "entered" in p[1] for p in paths))
        item = next(p for p in paths if "checkItems" in p[1])
        self.assertEqual(item[2]["idMember"], ["58189b548fc170379eca1937"])

    async def test_move_restamps_entered_list_on(self):
        await self.h.layer.move_card("master", "CARD9", "Paul Today")
        paths = self.h.paths()
        self.assertTrue(any(p[0] == "PUT" and p[1] == "/1/cards/CARD9" for p in paths))
        self.assertTrue(any("customField" in p[1] and "entered" in p[1] for p in paths))

    async def test_time_in_list_reads_action_history(self):
        entered = await self.h.layer.entered_current_list_at("CARD1")
        self.assertEqual(entered.isoformat(), "2026-08-05T10:00:00+00:00")
        days = await self.h.layer.days_in_list(
            "CARD1", now=datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)
        )
        self.assertAlmostEqual(days, 2.0, places=3)

    async def test_time_in_list_falls_back_to_card_id_epoch(self):
        self.h.layer.client.card_actions = self._none_actions
        entered = await self.h.layer.entered_current_list_at("68919f2f" + "a" * 16)
        self.assertEqual(entered.year, 2025)   # 0x68919f2f → Aug 2025

    async def _none_actions(self, card_id, before=""):
        return []

    async def test_multiple_owners_assign_and_unknowns_surface(self):
        # 01:01, 6 Aug: 'Paul, Kiefer' treated as one name killed the card.
        from app.daily12.trello_layer import match_members

        master = self.h.layer.board("master")
        ids, unknown = match_members(master, "Paul, Kiefer")
        self.assertEqual(len(ids), 2)
        self.assertEqual(unknown, [])
        ids, unknown = match_members(master, "Kiefer and Steph")
        self.assertEqual(len(ids), 1)
        self.assertEqual(unknown, ["Steph"])
        card = await self.h.layer.create_card_full(
            "master", "Paul Today", "Joint one", owner_name="Paul, Kiefer"
        )
        assigns = [p for p in self.h.paths() if p[0] == "POST" and "idMembers" in p[1]]
        self.assertEqual(len(assigns), 2)          # both really assigned
        self.assertNotIn("_unassigned_owners", card)

    async def test_kiefer_never_nudged_is_in_the_registry(self):
        master = next(b for b in self.h.layer.registry["boards"] if b["key"] == "master")
        self.assertFalse(master["partner"]["nudge"])

    async def test_learned_rulings_beat_the_unresolved_set(self):
        # 'BMI is Prodermis' said once → BMI resolves forever, no more asking.
        self.assertEqual(classify_domain("BMI paperwork"), (None, "bmi"))
        self.assertEqual(
            classify_domain("BMI paperwork", learned={"bmi": "Prodermis"}),
            ("Prodermis", None),
        )
        self.assertEqual(
            classify_domain("Revolax restock", learned={"revolax": "Aesthetics Supply"}),
            ("Aesthetics Supply", None),
        )

    async def test_brain_trello_card_tool_full_schema(self):
        import json as _json
        import os
        import tempfile

        from app.clients.anthropic_client import ClaudeClient
        from app.clients.deepgram_client import DeepgramClient
        from app.clients.elevenlabs_client import ElevenLabsClient
        from app.clients.telegram_client import IncomingMessage, TelegramClient
        from app.config import Settings
        from app.core.router import JarvisRouter
        from app.daily12.intake import VoiceIntake
        from app.db.sqlite import SqliteDatabase

        with tempfile.TemporaryDirectory() as tmp:
            db = SqliteDatabase(os.path.join(tmp, "t.db"))
            await db.init()
            mock = httpx.MockTransport(lambda r: httpx.Response(500))
            settings = Settings(telegram_bot_token="TOK", trello_key="K",
                                trello_token="T", _env_file=None)
            router = JarvisRouter(
                settings=settings, db=db,
                telegram=TelegramClient("TOK", transport=mock),
                claude=ClaudeClient("K", transport=mock),
                deepgram=DeepgramClient("K", transport=mock),
                elevenlabs=ElevenLabsClient("K", voice_id="V", transport=mock),
            )

            async def factory():
                return self.h.layer   # the bootstrapped mock layer

            router._intake_obj = VoiceIntake(router.claude, router.store, settings, factory)
            msg = IncomingMessage(chat_id=1, message_id=1, from_name="Paul")

            # Unruled name → honest ASK, nothing written.
            result = await router._dispatch_tool(
                "trello_card", {"action": "create", "board": "harry",
                                "title": "BMI paperwork"}, msg,
            )
            self.assertIn("DOMAIN UNRULED", result)

            # Ruled via memory → full-schema create, relative due resolved.
            await router.store.set("trello_domain_rulings", _json.dumps({"bmi": "Prodermis"}))
            result = await router._dispatch_tool(
                "trello_card", {"action": "create", "board": "harry",
                                "title": "BMI paperwork", "priority": "P2",
                                "due": "tomorrow", "list": "Inbox"}, msg,
            )
            self.assertIn("Card created on harry", result)
            self.assertIn("custom fields set", result)

            # Per-board guard surfaces loudly.
            result = await router._dispatch_tool(
                "trello_card", {"action": "create", "board": "harry",
                                "title": "Life admin", "domain": "Personal"}, msg,
            )
            self.assertIn("TRELLO REFUSED", result)
            await db.close()

    async def test_ordinals_match_pauls_convention(self):
        self.assertEqual(_ordinal(3), "3rd")
        self.assertEqual(_ordinal(11), "11th")
        self.assertEqual(_ordinal(21), "21st")
