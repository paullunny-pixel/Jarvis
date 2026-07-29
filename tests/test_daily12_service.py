"""Milestone 3 service tests: Trello sync (mocked HTTP), plan generation,
voice-feedback actions writing back to the board."""
import json
import os
import tempfile
import unittest
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.daily12.commands import execute_actions, mentions_tasks, parse_actions, wants_plan
from app.daily12.service import Daily12Service, relative_due
from app.db.sqlite import SqliteDatabase

BOARD = {"id": "B1", "name": "Master Board"}
LISTS = [
    {"id": "L1", "name": "Paul Today"},
    {"id": "L2", "name": "In Progress"},
    {"id": "L3", "name": "Done"},
    {"id": "L4", "name": "Blocked/Waiting"},
    {"id": "L5", "name": "Paul Personal"},
    {"id": "L7", "name": "This Week"},
    {"id": "L8", "name": "Brain Dump"},
]


def trello_card(i, name, list_id="L1", due=None, labels=None, activity="2026-07-10T09:00:00.000Z"):
    return {
        "id": f"C{i}",
        "name": name,
        "desc": "",
        "idList": list_id,
        "due": due,
        "labels": [{"name": l} for l in (labels or [])],
        "dateLastActivity": activity,
        "idMembers": [],
        "shortUrl": f"https://trello.com/c/{i}",
    }


CARDS = [
    trello_card(1, "Chase BMI doctor surveys", due="2026-07-26T12:00:00.000Z", labels=["£££", "waiting on paul"]),
    trello_card(2, "Website relaunch QA", labels=["££"]),
    trello_card(3, "Dutch notary paperwork", due="2026-07-30T12:00:00.000Z"),
    trello_card(4, "EU warehouse insurance quote"),
    trello_card(5, "Activate dormant accounts campaign", labels=["£"]),
    trello_card(6, "Grey range price list refresh"),
    trello_card(7, "Distributor registration pack for Karen", labels=["waiting-on-paul"]),
    trello_card(8, "5-for-5 boosters promo"),
    trello_card(9, "Old done thing", list_id="L3"),
    trello_card(10, "Stuck on supplier", list_id="L4"),
    trello_card(11, "Book the dentist", list_id="L5"),
    trello_card(12, "Renew passport", list_id="L5", due="2026-08-05T12:00:00.000Z"),
    trello_card(13, "Kiefer cashflow review", list_id="L7"),
    trello_card(14, "Someday: villa office idea", list_id="L8"),
]

# Order matches the untagged ACTIONABLE rows the tagger sees: C1-C8, C11-C14.
TAGS = [
    {"company": "prodermis", "project": "BMI relationship"},
    {"company": "derma_uk", "project": "Website relaunch"},
    {"company": "derma_eu", "project": "Dutch company formation"},
    {"company": "derma_eu", "project": "NL warehouse"},
    {"company": "derma_uk", "project": "Retention marketing"},
    {"company": "aesthetics_supply", "project": "Range activation"},
    {"company": "prodermis", "project": "Distributor registrations"},
    {"company": "aesthetics_supply", "project": "Promotions"},
    {"company": "", "project": ""},   # dentist — personal
    {"company": "", "project": ""},   # passport — personal
    {"company": "derma_uk", "project": "Finance rhythm"},   # This Week card
    {"company": "", "project": ""},   # Brain Dump card
]


class ServiceHarness:
    def __init__(self, db):
        self.trello_writes = []
        self.cards = [dict(c) for c in CARDS]   # per-test copies — mutable mid-flight

        def trello_handler(request: httpx.Request) -> httpx.Response:
            path = urlparse(str(request.url)).path
            if request.method in ("PUT", "POST"):
                self.trello_writes.append((request.method, path, dict(request.url.params)))
                if path == "/1/cards":
                    return httpx.Response(200, json={"id": "CNEW", "name": "created"})
                return httpx.Response(200, json={})
            if path.endswith("/members/me/boards"):
                return httpx.Response(200, json=[BOARD])
            if path.endswith("/lists"):
                return httpx.Response(200, json=LISTS)
            if path.endswith("/cards"):
                return httpx.Response(200, json=self.cards)
            if path.endswith("/members"):
                return httpx.Response(
                    200,
                    json=[
                        {"id": "M1", "fullName": "Kiefer Brindle", "username": "kiefer"},
                        {"id": "M2", "fullName": "Adrianna", "username": "adrianna"},
                    ],
                )
            return httpx.Response(404, text=path)

        def claude_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(TAGS)}]})

        from app.daily12.trello import TrelloClient

        self.service = Daily12Service(
            db,
            TrelloClient("K", "T", transport=httpx.MockTransport(trello_handler)),
            ClaudeClient("K", transport=httpx.MockTransport(claude_handler)),
        )


class TestService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = ServiceHarness(self.db)
        self.service = self.h.service

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_sync_caches_and_tags_cards(self):
        n = await self.service.sync()
        self.assertEqual(n, len(CARDS))
        row = await self.db.fetch_one("SELECT * FROM tasks WHERE trello_id = 'C1'")
        self.assertEqual(row["company_slug"], "prodermis")
        self.assertEqual(row["money"], 3)
        self.assertEqual(row["waiting_on_paul"], 1)
        self.assertEqual(row["actionable"], 1)
        done = await self.db.fetch_one("SELECT actionable FROM tasks WHERE trello_id = 'C9'")
        self.assertEqual(done["actionable"], 0)
        blocked = await self.db.fetch_one("SELECT actionable FROM tasks WHERE trello_id = 'C10'")
        self.assertEqual(blocked["actionable"], 0)

    async def test_generate_produces_plan_grouped_and_idempotent(self):
        plan = await self.service.generate(date(2026, 7, 25))
        main = [r for r in plan if r["position"] != 0]
        # 8 business cards from Paul Today (≤3/company) + 2 from Paul Personal
        self.assertEqual(len(main), 10)
        companies = {r["company_slug"] for r in main}
        self.assertEqual(
            companies,
            {"derma_uk", "derma_eu", "aesthetics_supply", "prodermis", "personal"},
        )
        again = await self.service.generate(date(2026, 7, 25))
        self.assertEqual(len(again), len(plan))  # no duplicates on regeneration

    async def test_focus_only_sources_from_the_two_lists(self):
        # An actionable card parked in "In Progress" must NOT enter the pool.
        await self.service.sync()
        await self.db.execute(
            "UPDATE tasks SET list_name = 'In Progress' WHERE trello_id = 'C2'"
        )
        plan = await self.service.generate(date(2026, 7, 25), resync=False)
        titles = [r["title"] for r in plan]
        self.assertNotIn("Website relaunch QA", titles)

    async def test_personal_capped_at_three(self):
        plan = await self.service.generate(date(2026, 7, 25))
        personal = [r for r in plan if r["company_slug"] == "personal"]
        self.assertLessEqual(len(personal), 3)
        self.assertEqual({r["title"] for r in personal}, {"Book the dentist", "Renew passport"})

    async def test_format_uses_focus_name_and_variable_count(self):
        await self.service.generate(date(2026, 7, 25))
        text = await self.service.format_plan(date(2026, 7, 25))
        self.assertIn("TODAY'S FOCUS", text)
        self.assertIn("/10 done", text)
        self.assertIn("PERSONAL", text)
        self.assertNotIn("BONUS", text)

    async def test_empty_day_is_a_clear_day(self):
        text = await self.service.format_plan(date(2026, 7, 20))
        self.assertIn("clear", text.lower())

    async def test_deleted_card_stops_haunting_the_plan(self):
        # The BMI ghost: a card deleted from Trello stayed actionable in the
        # cache forever and Jarvis swore it was 'still sitting there'.
        await self.service.sync()
        row = await self.db.fetch_one("SELECT actionable FROM tasks WHERE trello_id = 'C1'")
        self.assertEqual(row["actionable"], 1)
        self.h.cards = [c for c in self.h.cards if c["id"] != "C1"]  # deleted on Trello
        import asyncio

        await asyncio.sleep(1.1)  # a later sync run (second-resolution timestamps)
        await self.service.sync()
        row = await self.db.fetch_one("SELECT actionable FROM tasks WHERE trello_id = 'C1'")
        self.assertEqual(row["actionable"], 0)  # retired, not haunting

    async def test_trello_side_tick_lands_on_todays_plan(self):
        # Paul completes a card IN TRELLO (drags it to Done) — the next
        # hourly sync must tick it on Today's Focus too.
        today = await self.service.paul_today()
        await self.service.generate(today)
        for card in self.h.cards:
            if card["id"] == "C1":
                card["idList"] = "L3"  # moved to Done on the board
        import asyncio

        await asyncio.sleep(1.1)
        await self.service.sync()
        row = await self.db.fetch_one(
            "SELECT d.done FROM daily_12 d JOIN tasks t ON t.id = d.task_id"
            " WHERE t.trello_id = 'C1' AND d.plan_date = ?",
            (today.isoformat(),),
        )
        self.assertEqual(row["done"], 1)

    async def test_archive_really_removes_a_card(self):
        await self.service.generate(date(2026, 7, 25))
        result = await self.service.archive("website relaunch qa")
        self.assertIn("Archived 'Website relaunch QA'", result)
        closes = [w for w in self.h.trello_writes if "/cards/C2" in w[1]]
        self.assertTrue(any(w[2].get("closed") == "true" for w in closes))
        row = await self.db.fetch_one("SELECT actionable FROM tasks WHERE trello_id = 'C2'")
        self.assertEqual(row["actionable"], 0)

    async def test_overdue_due_dates_say_so(self):
        # Monday must never hear 'due Sunday' as if it's still coming.
        await self.service.generate(date(2026, 7, 27))
        text = await self.service.format_plan(date(2026, 7, 27))
        self.assertIn("2026-07-26 — OVERDUE", text)   # C1's Sunday deadline

    # ------- §3 planning rituals + §7 park-it (Trello writes) -------

    async def test_week_preview_lists_and_remembers_numbering(self):
        await self.service.sync()
        text = await self.service.week_preview()
        self.assertIn("THIS WEEK", text)
        self.assertIn("1. Kiefer cashflow review", text)
        self.assertNotIn("BRAIN DUMP", text)          # not Sunday mode
        sunday = await self.service.week_preview(sunday=True)
        self.assertIn("BRAIN DUMP", sunday)
        self.assertIn("villa office idea", sunday)

    async def test_queue_by_number_moves_card_into_paul_today(self):
        await self.service.sync()
        await self.service.week_preview()
        result = await self.service.queue_for_today("1")
        self.assertIn("queued for tomorrow", result)
        moves = [w for w in self.h.trello_writes if w[0] == "PUT" and "/cards/C13" in w[1]]
        self.assertTrue(any(w[2].get("idList") == "L1" for w in moves))  # → Paul Today
        row = await self.db.fetch_one("SELECT list_name FROM tasks WHERE trello_id = 'C13'")
        self.assertEqual(row["list_name"].lower(), "paul today")

    async def test_sunday_grooming_promotes_brain_dump(self):
        await self.service.sync()
        result = await self.service.promote_to_week("villa office")
        self.assertIn("promoted to This Week", result)
        moves = [w for w in self.h.trello_writes if w[0] == "PUT" and "/cards/C14" in w[1]]
        self.assertTrue(any(w[2].get("idList") == "L7" for w in moves))

    async def test_park_drops_thought_into_brain_dump(self):
        await self.service.sync()
        result = await self.service.park("order new shipping labels")
        self.assertIn("Parked", result)
        creates = [w for w in self.h.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(creates[-1][2]["idList"], "L8")
        self.assertIn("order new shipping labels", creates[-1][2]["name"])

    async def test_urgent_bmi_survey_ranks_top_of_prodermis(self):
        await self.service.generate(date(2026, 7, 25))
        text = await self.service.format_plan(date(2026, 7, 25))
        self.assertIn("PRODERMIS", text)
        self.assertIn("Chase BMI doctor surveys", text)
        self.assertNotIn("BONUS", text)  # hidden until 12/12

    async def test_mark_done_updates_local_and_moves_card(self):
        # mark_done/defer/comment act on TODAY's plan — generate for the real
        # today, not a fixed date, or these tests only pass on that date.
        today = await self.service.paul_today()
        await self.service.generate(today)
        plan = await self.service.plan(today)
        target = next(r for r in plan if "BMI doctor" in r["title"])
        result = await self.service.mark_done(str(target["position"]))
        self.assertIn("done", result.lower())
        moves = [w for w in self.h.trello_writes if w[0] == "PUT" and "/cards/" in w[1]]
        self.assertTrue(any(w[2].get("idList") == "L3" for w in moves))  # moved to Done
        row = await self.db.fetch_one(
            "SELECT done FROM daily_12 WHERE task_id = ?", (target["task_id"],)
        )
        self.assertEqual(row["done"], 1)

    async def test_done_by_title_words(self):
        await self.service.generate(await self.service.paul_today())
        result = await self.service.mark_done("the dutch notary one")
        self.assertIn("Dutch notary paperwork", result)

    async def test_defer_sets_due_and_counts_avoidance(self):
        await self.service.generate(await self.service.paul_today())
        result = await self.service.defer("website relaunch", "2026-07-31", "Friday 31 July")
        self.assertIn("pushed to Friday", result)
        writes = [w for w in self.h.trello_writes if w[2].get("due")]
        self.assertTrue(writes)
        row = await self.db.fetch_one("SELECT defer_count FROM tasks WHERE trello_id = 'C2'")
        self.assertEqual(row["defer_count"], 1)

    async def test_repeat_deferral_gets_called_out(self):
        await self.service.generate(await self.service.paul_today())
        for _ in range(3):
            result = await self.service.defer("website relaunch", "2026-07-31", "Friday")
        self.assertIn("deferral", result)

    async def test_create_task_on_kiefer(self):
        result = await self.service.create("VAT return prep", assignee="Kiefer")
        self.assertIn("Created 'VAT return prep' on Kiefer", result)
        creates = [w for w in self.h.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(creates[0][2]["idList"], "L8")  # Brain Dump — the intake
        assigns = [w for w in self.h.trello_writes if "idMembers" in w[1]]
        self.assertEqual(assigns[0][2]["value"], "M1")

    async def test_create_lands_in_the_column_paul_named(self):
        # The bug: a named column was ignored and everything fell to one list.
        result = await self.service.create("Chase BMI invoices", list_name="paul today")
        self.assertIn("in Paul Today", result)
        creates = [w for w in self.h.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(creates[-1][2]["idList"], "L1")   # Paul Today
        await self.service.create("Renew insurance", list_name="the this week column")
        creates = [w for w in self.h.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(creates[-1][2]["idList"], "L7")   # fuzzy → This Week

    async def test_comment_writes_back(self):
        await self.service.generate(await self.service.paul_today())
        result = await self.service.comment("boosters promo", "Karen confirmed the artwork")
        self.assertIn("Noted", result)
        comments = [w for w in self.h.trello_writes if "actions/comments" in w[1]]
        self.assertIn("Karen confirmed the artwork", comments[0][2]["text"])


MULTI_BOARDS = [
    {"id": "B0", "name": "Welcome Board"},   # empty and first — the production trap
    {"id": "B1", "name": "Master Board"},
    {"id": "B2", "name": "Prodermis Board"},
]
B2_LISTS = [
    {"id": "L21", "name": "Paul Today"},
    {"id": "L23", "name": "Done"},
]
B2_CARDS = [trello_card(21, "Pyway booster launch", list_id="L21", labels=["£££"])]
MULTI_TAGS = TAGS + [{"company": "prodermis", "project": "Pyway launch"}]


class MultiBoardHarness:
    """Three boards: an empty Welcome Board that sorts first, the Master Board,
    and a second working board with its own Done list."""

    def __init__(self, db, board_filter=""):
        self.trello_writes = []
        per_board = {"B0": ([], []), "B1": (LISTS, CARDS), "B2": (B2_LISTS, B2_CARDS)}

        def trello_handler(request: httpx.Request) -> httpx.Response:
            path = urlparse(str(request.url)).path
            if request.method in ("PUT", "POST"):
                self.trello_writes.append((request.method, path, dict(request.url.params)))
                if path == "/1/cards":
                    return httpx.Response(200, json={"id": "CNEW", "name": "created"})
                return httpx.Response(200, json={})
            if path.endswith("/members/me/boards"):
                return httpx.Response(200, json=MULTI_BOARDS)
            for board_id, (lists, cards) in per_board.items():
                if path == f"/1/boards/{board_id}/lists":
                    return httpx.Response(200, json=lists)
                if path == f"/1/boards/{board_id}/cards":
                    return httpx.Response(200, json=cards)
            if path.endswith("/members"):
                return httpx.Response(200, json=[])
            return httpx.Response(404, text=path)

        def claude_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(MULTI_TAGS)}]})

        from app.daily12.trello import TrelloClient

        self.service = Daily12Service(
            db,
            TrelloClient("K", "T", transport=httpx.MockTransport(trello_handler)),
            ClaudeClient("K", transport=httpx.MockTransport(claude_handler)),
            board_filter=board_filter,
        )


class TestMultiBoard(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = MultiBoardHarness(self.db)
        self.service = self.h.service

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_sync_reads_every_board_even_when_first_is_empty(self):
        n = await self.service.sync()
        self.assertEqual(n, len(CARDS) + len(B2_CARDS))
        master = await self.db.fetch_one("SELECT board_id, board_name FROM tasks WHERE trello_id = 'C1'")
        self.assertEqual(master["board_id"], "B1")
        self.assertEqual(master["board_name"], "Master Board")
        other = await self.db.fetch_one("SELECT board_id, board_name FROM tasks WHERE trello_id = 'C21'")
        self.assertEqual(other["board_id"], "B2")
        self.assertEqual(other["board_name"], "Prodermis Board")

    async def test_health_reports_all_boards(self):
        await self.service.sync()
        health = await self.service.health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["boards"], 3)
        self.assertIn("Master Board", health["board_name"])
        self.assertIn("Prodermis Board", health["board_name"])
        # counts cards in play: Done (C9) and Blocked (C10) don't count
        self.assertEqual(health["cached_cards"], len(CARDS) + len(B2_CARDS) - 2)

    async def test_mark_done_moves_card_on_its_own_board(self):
        await self.service.generate(await self.service.paul_today())
        result = await self.service.mark_done("pyway booster launch")
        self.assertIn("Pyway booster launch", result)
        moves = [w for w in self.h.trello_writes if w[0] == "PUT" and "/cards/C21" in w[1]]
        self.assertTrue(any(w[2].get("idList") == "L23" for w in moves))  # B2's Done, not B1's

    async def test_new_cards_go_to_the_busiest_board(self):
        await self.service.sync()
        await self.service.create("Test VAT card")
        creates = [w for w in self.h.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(creates[0][2]["idList"], "L8")  # Master Board's Brain Dump


class TestBoardScope(unittest.IsolatedAsyncioTestCase):
    """TRELLO_BOARDS scope: Jarvis works only from the named board(s)."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_sync_reads_only_the_scoped_board(self):
        h = MultiBoardHarness(self.db, board_filter="Master Board")
        n = await h.service.sync()
        self.assertEqual(n, len(CARDS))  # B2's card was never read
        self.assertIsNone(await self.db.fetch_one("SELECT id FROM tasks WHERE trello_id = 'C21'"))

    async def test_scope_is_case_insensitive(self):
        h = MultiBoardHarness(self.db, board_filter="  master board ")
        self.assertEqual(await h.service.sync(), len(CARDS))

    async def test_out_of_scope_cache_retires_but_keeps_history(self):
        # Yesterday: all boards synced. Today: scope narrows to Master Board.
        await MultiBoardHarness(self.db).service.sync()
        row = await self.db.fetch_one("SELECT actionable FROM tasks WHERE trello_id = 'C21'")
        self.assertEqual(row["actionable"], 1)

        await MultiBoardHarness(self.db, board_filter="Master Board").service.sync()
        row = await self.db.fetch_one("SELECT actionable FROM tasks WHERE trello_id = 'C21'")
        self.assertEqual(row["actionable"], 0)   # out of the working pool
        on_board = await self.db.fetch_one("SELECT actionable FROM tasks WHERE trello_id = 'C1'")
        self.assertEqual(on_board["actionable"], 1)

        # Scope widens again → the card returns to play on the next sync.
        await MultiBoardHarness(self.db, board_filter="all").service.sync()
        row = await self.db.fetch_one("SELECT actionable FROM tasks WHERE trello_id = 'C21'")
        self.assertEqual(row["actionable"], 1)

    async def test_unmatched_scope_falls_back_to_all_boards(self):
        # A renamed board must never put Jarvis back in the zero-cards trap.
        h = MultiBoardHarness(self.db, board_filter="Bored Master")
        self.assertEqual(await h.service.sync(), len(CARDS) + len(B2_CARDS))

    async def test_creates_and_done_moves_stay_on_the_scoped_board(self):
        h = MultiBoardHarness(self.db, board_filter="Master Board")
        await h.service.sync()
        await h.service.create("New scoped card")
        creates = [w for w in h.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(creates[0][2]["idList"], "L8")  # Master Board's Brain Dump

    async def test_health_reports_the_scope(self):
        h = MultiBoardHarness(self.db, board_filter="Master Board")
        await h.service.sync()
        health = await h.service.health()
        self.assertTrue(health["scoped"])
        self.assertEqual(health["boards"], 3)             # visible
        self.assertEqual(health["board_name"], "Master Board")  # in play
        # actionable cards only: Done (C9) and Blocked (C10) don't count
        self.assertEqual(health["cached_cards"], len(CARDS) - 2)


MASTER_LISTS = [
    {"id": "ML1", "name": "Paul Personal"},
    {"id": "ML2", "name": "Brain Dump"},
    {"id": "ML3", "name": "This Week"},
    {"id": "ML4", "name": "Paul Today"},
    {"id": "ML5", "name": "Kiefer Today"},
    {"id": "ML6", "name": "Blocked / Waiting on"},
    {"id": "ML7", "name": "Done Week 27th July"},
    {"id": "ML8", "name": "Done Week 20th July"},
    {"id": "ML9", "name": "Done!"},
]
# The audited label state: only Urgent named; five blank colour slots.
MASTER_LABELS = [
    {"id": "LB-red", "name": "Urgent", "color": "red"},
    {"id": "LB-yellow", "name": "", "color": "yellow"},
    {"id": "LB-green", "name": "", "color": "green"},
    {"id": "LB-orange", "name": "", "color": "orange"},
    {"id": "LB-purple", "name": "", "color": "purple"},
    {"id": "LB-blue", "name": "", "color": "blue"},
]


class MasterBoardHarness:
    """The real Master Board shape (Trello System Brief): brief lists, blank
    label slots, Paul + Kiefer as members. Label writes mutate state so
    ensure_labels can be proven idempotent."""

    def __init__(self, db):
        self.trello_writes = []
        self.labels = [dict(l) for l in MASTER_LABELS]
        self._label_seq = 0

        def trello_handler(request: httpx.Request) -> httpx.Response:
            path = urlparse(str(request.url)).path
            params = dict(request.url.params)
            if request.method in ("PUT", "POST"):
                self.trello_writes.append((request.method, path, params))
                if path == "/1/cards":
                    return httpx.Response(200, json={"id": "CNEW", "name": "created"})
                if path.startswith("/1/labels/"):
                    label_id = path.rsplit("/", 1)[1]
                    for label in self.labels:
                        if label["id"] == label_id:
                            label["name"] = params.get("name", label["name"])
                    return httpx.Response(200, json={})
                if path.endswith("/labels"):
                    self._label_seq += 1
                    new = {
                        "id": f"LB-new{self._label_seq}",
                        "name": params.get("name", ""),
                        "color": params.get("color", ""),
                    }
                    self.labels.append(new)
                    return httpx.Response(200, json=new)
                return httpx.Response(200, json={})
            if path.endswith("/members/me/boards"):
                return httpx.Response(200, json=[BOARD])
            if path.endswith("/labels"):
                return httpx.Response(200, json=self.labels)
            if path.endswith("/lists"):
                return httpx.Response(200, json=MASTER_LISTS)
            if path.endswith("/cards"):
                return httpx.Response(200, json=[])
            if path.endswith("/members"):
                return httpx.Response(200, json=[
                    {"id": "M-P", "fullName": "Paul Lunny", "username": "paullunny"},
                    {"id": "M-K", "fullName": "Kiefer Brindle", "username": "kiefer"},
                ])
            return httpx.Response(404, text=path)

        from app.daily12.trello import TrelloClient

        self.service = Daily12Service(
            db,
            TrelloClient("K", "T", transport=httpx.MockTransport(trello_handler)),
            ClaudeClient("K", transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"content": [{"type": "text", "text": "[]"}]})
            )),
        )


class TestMasterBoardSystem(unittest.IsolatedAsyncioTestCase):
    """Trello System Brief: the two-axis label system, member assignment,
    Done Week routing, and the never-duplicate rule."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = MasterBoardHarness(self.db)
        self.service = self.h.service

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_ensure_labels_builds_the_brief_system(self):
        ids = await self.service.ensure_labels()
        self.assertEqual(ids["urgent"], "LB-red")            # existing label kept
        self.assertEqual(ids["personal"], "LB-green")        # blank slots renamed
        self.assertEqual(ids["prodermis"], "LB-purple")
        self.assertEqual(ids["business_ops"], "LB-blue")
        self.assertEqual(ids["money"], "LB-orange")
        self.assertEqual(ids["waiting"], "LB-yellow")
        self.assertTrue(ids["derma"].startswith("LB-new"))   # sky + pink created
        self.assertTrue(ids["delegate"].startswith("LB-new"))
        renames = [w for w in self.h.trello_writes if w[0] == "PUT" and "/labels/" in w[1]]
        creates = [w for w in self.h.trello_writes if w[0] == "POST" and w[1].endswith("/labels")]
        self.assertEqual(len(renames), 5)
        self.assertEqual({w[2]["color"] for w in creates}, {"sky", "pink"})
        # Second run: everything already named — zero label writes.
        before = len(self.h.trello_writes)
        again = await self.service.ensure_labels()
        self.assertEqual(again, ids)
        self.assertEqual(len(self.h.trello_writes), before)

    async def test_create_carries_domain_flags_member_and_description(self):
        await self.service.ensure_labels()
        reply = await self.service.create(
            "Pay BMI invoice — £480", assignee="paul", list_name="Paul Today",
            domain="business_ops", flags=["urgent", "money"],
            description="Invoice arrived by email; BMI chasing.",
        )
        card_posts = [w for w in self.h.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(card_posts[0][2]["idList"], "ML4")            # Paul Today
        self.assertIn("desc", card_posts[0][2])
        label_adds = [w for w in self.h.trello_writes if w[1].endswith("/idLabels")]
        self.assertEqual(
            {w[2]["value"] for w in label_adds}, {"LB-blue", "LB-red", "LB-orange"}
        )
        member_adds = [w for w in self.h.trello_writes if w[1].endswith("/idMembers")]
        self.assertEqual(member_adds[0][2]["value"], "M-P")            # Paul as member
        self.assertIn("Business Ops", reply)
        self.assertIn("£ Money", reply)
        self.assertNotIn("on Paul", reply)                             # silently his

    async def test_exact_duplicate_is_never_created(self):
        await self.db.execute(
            "INSERT INTO tasks (trello_id, title, list_name, board_id, board_name,"
            " actionable, score, synced_at) VALUES ('TDUP', 'Pay BMI invoice — £480',"
            " 'This Week', 'B1', 'Master Board', 1, 0, '2026-07-30T10:00:00')",
        )
        reply = await self.service.create("pay bmi invoice £480", domain="business_ops")
        self.assertIn("already on the board", reply)
        self.assertIn("This Week", reply)
        self.assertEqual([w for w in self.h.trello_writes if w[1] == "/1/cards"], [])

    async def test_done_goes_to_the_current_week_not_the_legacy_pile(self):
        self.assertEqual(await self.service._done_list_id("B1"), "ML7")

    def test_board_shapes_are_out_of_play(self):
        from app.daily12.scoring import is_actionable_list

        for name in ("Done Week 27th July", "Done!", "Blocked / Waiting on", "Waiting on Sarah"):
            self.assertFalse(is_actionable_list(name), name)
        for name in ("Paul Today", "Kiefer Today", "In Progress", "Brain Dump", "This Week"):
            self.assertTrue(is_actionable_list(name), name)

    def test_parser_teaches_the_card_rules(self):
        from app.daily12.commands import PARSER_SYSTEM

        self.assertIn("CARD RULES", PARSER_SYSTEM)
        self.assertIn("business_ops", PARSER_SYSTEM)
        self.assertIn("£480", PARSER_SYSTEM)              # the money convention
        self.assertIn("Blocked / Waiting on", PARSER_SYSTEM)
        self.assertIn("Genuinely unsure → empty, never guess", PARSER_SYSTEM)


class TestCommands(unittest.IsolatedAsyncioTestCase):
    def test_task_hint_gate(self):
        self.assertTrue(mentions_tasks("number three is done"))
        self.assertTrue(mentions_tasks("what's my 12 looking like"))
        self.assertTrue(mentions_tasks("put a card on Kiefer for the VAT return"))
        self.assertTrue(mentions_tasks("push it to friday"))
        self.assertFalse(mentions_tasks("morning, how are we"))
        self.assertFalse(mentions_tasks("what should I eat before the gym"))

    def test_wants_plan(self):
        self.assertTrue(wants_plan("what's my 12?"))
        self.assertTrue(wants_plan("show me my plan"))
        self.assertTrue(wants_plan("plan my day"))
        self.assertFalse(wants_plan("number 4 done"))

    async def test_parse_actions_via_haiku(self):
        def handler(request: httpx.Request) -> httpx.Response:
            actions = [
                {"action": "done", "target": "3"},
                {"action": "create", "title": "VAT return", "assignee": "Kiefer"},
            ]
            return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(actions)}]})

        claude = ClaudeClient("K", transport=httpx.MockTransport(handler))
        actions = await parse_actions(claude, "three's done, VAT card on kiefer", "1. x")
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]["action"], "done")

    def test_relative_due(self):
        base = date(2026, 7, 25)  # Saturday
        iso, human = relative_due(base, "friday")
        self.assertEqual(iso, "2026-07-31")
        self.assertIn("Friday", human)
        self.assertEqual(relative_due(base, "tomorrow")[0], "2026-07-26")
        self.assertEqual(relative_due(base, "2026-08-03")[0], "2026-08-03")


class TestCardMatching(unittest.IsolatedAsyncioTestCase):
    """The Eva-lunch bug (28 Jul): 'archive the lunch card' binned 'Confirm
    with Eva lunch time' instead of the card called 'Lunch'. Exact titles win;
    a destructive dead heat asks instead of guessing."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = MultiBoardHarness(self.db)
        self.service = self.h.service
        await self.service.sync()
        # The production board: three lunch-flavoured cards, oldest first.
        for i, title in enumerate(
            ["Confirm with Eva lunch time", "Make reservation for Prodermis management lunch", "Lunch"]
        ):
            await self.db.execute(
                "INSERT INTO tasks (trello_id, title, list_name, board_id, board_name,"
                " actionable, score, synced_at) VALUES (?, ?, 'Paul Today', 'B1',"
                " 'Master Board', 1, 0, '2026-07-28T10:00:00')",
                (f"LUNCH{i}", title),
            )

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    def test_exact_title_beats_a_longer_match(self):
        from app.daily12.service import best_title_match

        rows = [
            {"title": "Confirm with Eva lunch time"},
            {"title": "Make reservation for Prodermis management lunch"},
            {"title": "Lunch"},
        ]
        match, ties = best_title_match("the lunch card", rows)
        self.assertIsNotNone(match)
        self.assertEqual(match["title"], "Lunch")
        self.assertEqual(ties, [])

    def test_dead_heat_returns_candidates_not_a_guess(self):
        from app.daily12.service import best_title_match

        rows = [{"title": "Email supplier list"}, {"title": "Supplier email chase"}]
        match, ties = best_title_match("supplier email", rows)
        self.assertIsNone(match)
        self.assertEqual(len(ties), 2)

    async def test_archive_the_lunch_card_hits_lunch_exactly(self):
        result = await self.service.archive("the lunch card")
        self.assertIn("Archived 'Lunch'", result)
        self.assertNotIn("Eva", result)
        archived = [w for w in self.h.trello_writes if "/cards/LUNCH2" in w[1]]
        self.assertTrue(any(w[2].get("closed") == "true" for w in archived))
        row = await self.db.fetch_one(
            "SELECT actionable FROM tasks WHERE title = 'Confirm with Eva lunch time'"
        )
        self.assertEqual(row["actionable"], 1)  # the bystander survives

    async def test_ambiguous_archive_asks_instead_of_binning(self):
        await self.db.execute("UPDATE tasks SET actionable = 0 WHERE title = 'Lunch'")
        for i, title in enumerate(["Lunch with Kiefer", "Lunch with Harry"]):
            await self.db.execute(
                "INSERT INTO tasks (trello_id, title, list_name, board_id, board_name,"
                " actionable, score, synced_at) VALUES (?, ?, 'Paul Today', 'B1',"
                " 'Master Board', 1, 0, '2026-07-28T10:00:00')",
                (f"AMBIG{i}", title),
            )
        result = await self.service.archive("lunch")
        self.assertIn("more than one card", result)
        self.assertIn("Kiefer", result)
        self.assertIn("Harry", result)
        archived = [w for w in self.h.trello_writes if "closed" in w[2]]
        self.assertEqual(archived, [])  # nothing was binned on a guess


class TestRecentlyCleared(unittest.IsolatedAsyncioTestCase):
    """The BMI-surveys bug (28 Jul): the brain kept chasing cards the board
    had already closed. The live-truth injection now names what's cleared."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.service = MultiBoardHarness(self.db).service

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def _task(self, title, list_name, actionable, synced_at):
        await self.db.execute(
            "INSERT INTO tasks (trello_id, title, list_name, board_id, board_name,"
            " actionable, score, synced_at) VALUES (?, ?, ?, 'B1', 'Master Board',"
            " ?, 0, ?)",
            (f"T-{title[:12]}", title, list_name, actionable, synced_at),
        )

    async def test_cleared_cards_reported_with_their_real_state(self):
        from datetime import datetime as dt, timedelta as td, timezone as tz

        now = dt.now(tz.utc).isoformat(timespec="seconds")
        old = (dt.now(tz.utc) - td(days=30)).isoformat(timespec="seconds")
        await self._task("BMI doctor surveys", "Done", 0, now)
        await self._task("Chase supplier", "Blocked", 0, now)
        await self._task("Vanished card", "Paul Today", 0, now)
        await self._task("Ancient history", "Done", 0, old)
        await self._task("Still live", "Paul Today", 1, now)
        text = await self.service.recently_cleared()
        self.assertIn("BMI doctor surveys — DONE", text)
        self.assertIn("Chase supplier — parked in Blocked", text)
        self.assertIn("Vanished card — gone from the board", text)
        self.assertNotIn("Ancient history", text)   # outside the window
        self.assertNotIn("Still live", text)        # still in play
        self.assertIn("NOT open tasks", text)

    async def test_quiet_board_injects_nothing(self):
        self.assertEqual(await self.service.recently_cleared(), "")


class TestCalendarHonesty(unittest.IsolatedAsyncioTestCase):
    """Calendar write-back isn't built yet — a calendar ask must be parked
    honestly, never silently dropped (28 Jul: only the Trello half of a
    two-instruction voice note happened)."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = MultiBoardHarness(self.db)
        await self.h.service.sync()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    def test_hint_catches_calendar_asks(self):
        self.assertTrue(mentions_tasks("add the dentist to my calendar for Thursday"))
        self.assertTrue(mentions_tasks("stick that in the diary"))

    async def test_calendar_ask_is_parked_not_dropped(self):
        results, _ = await execute_actions(
            self.h.service,
            [{"action": "calendar_event", "title": "Dentist", "when": "friday"}],
        )
        line = results[0]
        self.assertIn("can't add events yet", line)
        self.assertIn("CALENDAR: Dentist", line)
        creates = [w for w in self.h.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(len(creates), 1)

    async def test_two_instructions_both_execute(self):
        results, _ = await execute_actions(
            self.h.service,
            [{"action": "calendar_event", "title": "Dentist"},
             {"action": "create", "title": "Order BMI stock"}],
        )
        self.assertEqual(len(results), 2)
        creates = [w for w in self.h.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(len(creates), 2)


if __name__ == "__main__":
    unittest.main()
