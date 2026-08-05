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

    async def test_bootstrap_resolves_by_name_and_skips_archives(self):
        master = self.h.layer.board("master")
        self.assertIn("Inbox", master.lists)
        self.assertNotIn("Old Stuff (Archive)", master.lists)
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

    async def test_ordinals_match_pauls_convention(self):
        self.assertEqual(_ordinal(3), "3rd")
        self.assertEqual(_ordinal(11), "11th")
        self.assertEqual(_ordinal(21), "21st")
