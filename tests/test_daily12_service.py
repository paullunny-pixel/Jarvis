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
    {"id": "L1", "name": "To Do"},
    {"id": "L2", "name": "In Progress"},
    {"id": "L3", "name": "Done"},
    {"id": "L4", "name": "Blocked/Waiting"},
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
]

TAGS = [
    {"company": "prodermis", "project": "BMI relationship"},
    {"company": "derma_uk", "project": "Website relaunch"},
    {"company": "derma_eu", "project": "Dutch company formation"},
    {"company": "derma_eu", "project": "NL warehouse"},
    {"company": "derma_uk", "project": "Retention marketing"},
    {"company": "aesthetics_supply", "project": "Range activation"},
    {"company": "prodermis", "project": "Distributor registrations"},
    {"company": "aesthetics_supply", "project": "Promotions"},
    {"company": "derma_uk", "project": "Website relaunch"},
    {"company": "prodermis", "project": "BMI relationship"},
]


class ServiceHarness:
    def __init__(self, db):
        self.trello_writes = []

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
                return httpx.Response(200, json=CARDS)
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
        # 8 actionable+tagged cards only → fewer than 12, but every company shows up
        self.assertEqual(len(main), 8)
        companies = {r["company_slug"] for r in main}
        self.assertEqual(companies, {"derma_uk", "derma_eu", "aesthetics_supply", "prodermis"})
        again = await self.service.generate(date(2026, 7, 25))
        self.assertEqual(len(again), len(plan))  # no duplicates on regeneration

    async def test_urgent_bmi_survey_ranks_top_of_prodermis(self):
        await self.service.generate(date(2026, 7, 25))
        text = await self.service.format_plan(date(2026, 7, 25))
        self.assertIn("PRODERMIS", text)
        self.assertIn("Chase BMI doctor surveys", text)
        self.assertNotIn("BONUS", text)  # hidden until 12/12

    async def test_mark_done_updates_local_and_moves_card(self):
        await self.service.generate(date(2026, 7, 25))
        plan = await self.service.plan(date(2026, 7, 25))
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
        await self.service.generate(date(2026, 7, 25))
        result = await self.service.mark_done("the dutch notary one")
        self.assertIn("Dutch notary paperwork", result)

    async def test_defer_sets_due_and_counts_avoidance(self):
        await self.service.generate(date(2026, 7, 25))
        result = await self.service.defer("website relaunch", "2026-07-31", "Friday 31 July")
        self.assertIn("pushed to Friday", result)
        writes = [w for w in self.h.trello_writes if w[2].get("due")]
        self.assertTrue(writes)
        row = await self.db.fetch_one("SELECT defer_count FROM tasks WHERE trello_id = 'C2'")
        self.assertEqual(row["defer_count"], 1)

    async def test_repeat_deferral_gets_called_out(self):
        await self.service.generate(date(2026, 7, 25))
        for _ in range(3):
            result = await self.service.defer("website relaunch", "2026-07-31", "Friday")
        self.assertIn("deferral", result)

    async def test_create_task_on_kiefer(self):
        result = await self.service.create("VAT return prep", assignee="Kiefer")
        self.assertIn("Created 'VAT return prep' on Kiefer", result)
        creates = [w for w in self.h.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(creates[0][2]["idList"], "L1")  # To Do list
        assigns = [w for w in self.h.trello_writes if "idMembers" in w[1]]
        self.assertEqual(assigns[0][2]["value"], "M1")

    async def test_comment_writes_back(self):
        await self.service.generate(date(2026, 7, 25))
        result = await self.service.comment("boosters promo", "Karen confirmed the artwork")
        self.assertIn("Noted", result)
        comments = [w for w in self.h.trello_writes if "actions/comments" in w[1]]
        self.assertIn("Karen confirmed the artwork", comments[0][2]["text"])


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


if __name__ == "__main__":
    unittest.main()
