"""Brain-first (Phase A2): the brain drives the machinery through tools.
Covers the client tool loop, the router's tool belt end-to-end (Trello create,
rhythm switches, remember), and the no-phantom guarantees around them."""
import json
import os
import tempfile
import unittest

import httpx

from app.clients.anthropic_client import ClaudeClient
from tests.test_heartbeat import JobsHarness, at_local
from tests.test_router import OWNER, RouterHarness
from tests.test_telegram_client import text_update


def tool_use(name, **tool_input):
    return {"type": "tool_use", "id": f"t_{name}", "name": name, "input": tool_input}


def text_block(text):
    return {"type": "text", "text": text}


class ScriptedClaude:
    """MockTransport handler: opus turns pop from a script; haiku answers by
    system-prompt role (task parser / intent triage / memory writer)."""

    def __init__(self, opus_responses, parser_actions=None, writer_items=None):
        self.opus_responses = list(opus_responses)
        self.parser_actions = parser_actions or []
        self.writer_items = writer_items or []
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        if "haiku" in payload["model"]:
            system = str(payload.get("system", ""))
            if system.startswith("You convert Paul's message"):
                body = json.dumps(self.parser_actions)
            elif system.startswith("You read ONE short message"):
                body = json.dumps({"intent": "none", "confident": True})
            else:  # memory writer & friends
                body = json.dumps(self.writer_items)
            return httpx.Response(200, json={"content": [{"type": "text", "text": body}]})
        content = self.opus_responses.pop(0) if self.opus_responses else [text_block("As you were, sir.")]
        return httpx.Response(200, json={"content": content})


class TestToolLoop(unittest.IsolatedAsyncioTestCase):
    async def test_two_round_loop_feeds_results_back(self):
        script = ScriptedClaude([
            [text_block("Let me check."), tool_use("probe", q="x")],
            [text_block("The answer is 42.")],
        ])
        claude = ClaudeClient("K", transport=httpx.MockTransport(script))
        calls = []

        async def handler(name, tool_input):
            calls.append((name, tool_input))
            return "probe says 42"

        out = await claude.converse_with_tools(
            "sys", [{"role": "user", "content": "q"}], [{"name": "probe"}], handler
        )
        self.assertEqual(out, "The answer is 42.")
        self.assertEqual(calls, [("probe", {"q": "x"})])
        # Round 2 carried the assistant turn + the tool result back.
        second = script.requests[-1]["messages"]
        self.assertEqual(second[1]["role"], "assistant")
        self.assertEqual(second[2]["content"][0]["type"], "tool_result")
        self.assertIn("probe says 42", second[2]["content"][0]["content"])

    async def test_tool_crash_reports_honestly_instead_of_dying(self):
        script = ScriptedClaude([
            [tool_use("probe")],
            [text_block("That tool fell over, sir — nothing was done.")],
        ])
        claude = ClaudeClient("K", transport=httpx.MockTransport(script))

        async def handler(name, tool_input):
            raise RuntimeError("boom")

        out = await claude.converse_with_tools(
            "sys", [{"role": "user", "content": "q"}], [{"name": "probe"}], handler
        )
        self.assertIn("nothing was done", out)
        result = script.requests[-1]["messages"][2]["content"][0]["content"]
        self.assertIn("TOOL FAILED", result)
        self.assertIn("do not pretend", result)

    async def test_plain_text_needs_one_round_only(self):
        script = ScriptedClaude([[text_block("Just chat.")]])
        claude = ClaudeClient("K", transport=httpx.MockTransport(script))
        out = await claude.converse_with_tools(
            "sys", [{"role": "user", "content": "q"}], [{"name": "probe"}], None
        )
        self.assertEqual(out, "Just chat.")
        self.assertEqual(len(script.requests), 1)


class BrainHarness:
    """Full router with a scripted brain and the multi-board fake Trello."""

    def __init__(self, db, opus_responses, parser_actions=None, writer_items=None):
        from tests.test_daily12_service import MultiBoardHarness

        self.script = ScriptedClaude(opus_responses, parser_actions, writer_items)
        self.h = RouterHarness(db)
        self.h.router.claude = ClaudeClient("K", transport=httpx.MockTransport(self.script))
        self.board = MultiBoardHarness(db)
        self.board.service._claude = self.h.router.claude  # not used, but consistent
        self.h.router.daily12 = self.board.service
        self.jobs = JobsHarness(db).jobs
        self.h.router.heartbeat = self.jobs

    def sent(self):
        from urllib.parse import parse_qs

        out = []
        for method, body in self.h.telegram_calls:
            if method == "sendMessage":
                out.append(parse_qs(body.decode())["text"][0])
            elif method == "sendVoice":
                out.append(body.decode(errors="replace"))
        return " ".join(out)


class TestBrainHands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db.sqlite import SqliteDatabase

        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_fuzzy_board_talk_creates_the_card_through_the_tool(self):
        # No 'Jarvis add to Trello' prefix, typos and all — the brain routes it.
        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("trello", instruction="Create a card: Pay the BMI invoice — £480, urgent, for Paul")],
                [text_block("Card's on the board, sir — BMI invoice, flagged urgent.")],
            ],
            parser_actions=[{"action": "create", "title": "Pay the BMI invoice — £480",
                            "assignee": "paul", "domain": "prodermis", "flags": ["urgent", "money"]}],
        )
        await b.h.router.handle_update(
            text_update("cna you put the bmi invocie on the borad, urgent one", OWNER)
        )
        creates = [w for w in b.board.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(len(creates), 1)
        self.assertIn("BMI invoice", creates[0][2]["name"])
        self.assertIn("Card's on the board", b.sent())

    async def test_rhythm_tool_flips_the_real_quiet_switch(self):
        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("rhythm", quiet_today=True)],
                [text_block("Silence engaged for today, sir — meds still stand.")],
            ],
        )
        await b.h.router.handle_update(
            text_update("mate im fried, shush the pings for the rest of the day yeah", OWNER)
        )
        self.assertTrue(await b.jobs.quiet_today())
        # The tool result confirmed before the brain claimed it.
        results = [
            blk for r in b.script.requests for m in r["messages"]
            if isinstance(m.get("content"), list)
            for blk in m["content"] if isinstance(blk, dict) and blk.get("type") == "tool_result"
        ]
        self.assertTrue(any("quiet day ON" in str(blk["content"]) for blk in results))
        self.assertIn("Silence engaged", b.sent())

    async def test_remember_tool_files_into_the_second_brain(self):
        from app.memory.crypto import PrivateBox
        from app.memory.embedder import HashEmbedder
        from app.memory.store import LivingFacts, MemoryStore

        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("remember", facts="Steph's birthday is 14 September.")],
                [text_block("Locked in — Steph's birthday, 14 September.")],
            ],
            writer_items=[{"content": "Steph's birthday is 14 September", "room": "people",
                           "type": "STABLE", "tags": ["steph"], "living_key": ""}],
        )
        b.h.router.memory = MemoryStore(self.db, HashEmbedder(), PrivateBox("k"))
        b.h.router.living = LivingFacts(self.db)
        await b.h.router.handle_update(
            text_update("remember steph birthday is 14th of september", OWNER)
        )
        hits = await b.h.router.memory.search("when is Steph's birthday", k=3)
        self.assertTrue(any("14 September" in h["content"] for h in hits))
        self.assertIn("Locked in", b.sent())

    async def test_deliberate_remember_never_comes_back_empty(self):
        # The Marijana bug (3 Aug): Paul said who Kiefer is seeing, the
        # extraction classifier shrugged ('[]'), and Jarvis told him his
        # memory refused to file her. A deliberate remember now files
        # verbatim when the classifier finds nothing.
        from app.memory.crypto import PrivateBox
        from app.memory.embedder import HashEmbedder
        from app.memory.store import LivingFacts, MemoryStore

        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("remember", facts="Marijana is the girl Kiefer is seeing.", room="people")],
                [text_block("Marijana — filed. She'll be no stranger tomorrow.")],
            ],
            writer_items=[],   # the classifier finds nothing durable
        )
        b.h.router.memory = MemoryStore(self.db, HashEmbedder(), PrivateBox("k"))
        b.h.router.living = LivingFacts(self.db)
        await b.h.router.handle_update(
            text_update("dinner with Kiefer, Stef and Marijana the girl he is seeing", OWNER)
        )
        hits = await b.h.router.memory.search("who is Kiefer seeing", k=3)
        self.assertTrue(any("Marijana" in h["content"] for h in hits))
        rows = await self.db.fetch_all("SELECT room FROM memory_chunks WHERE source = 'brain-tool'")
        self.assertEqual(rows[0]["room"], "people")
        self.assertIn("filed", b.sent().lower())

    async def test_rhythm_tool_moves_the_clocks_when_paul_is_elsewhere(self):
        # 4 Aug, 01:30 Dubai: lights-out fired on London clocks. The brain
        # knew he was in Dubai but had no lever — now it does.
        from app.core.store import SettingsStore

        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("rhythm", timezone_place="dubai")],
                [text_block("Clocks moved to Dubai, sir — everything follows you now.")],
            ],
        )
        await b.h.router.handle_update(
            text_update("mate those bedtime pings came at half one in the morning here", OWNER)
        )
        self.assertEqual(
            await SettingsStore(self.db).get("current_timezone"), "Asia/Dubai"
        )
        self.assertIn("Clocks moved to Dubai", b.sent())

    async def test_rhythm_tool_goodnight_closes_the_previous_nights_day(self):
        from unittest.mock import patch

        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("rhythm", goodnight=True)],
                [text_block("Goodnight, Paul — day closed. Sleep well.")],
            ],
        )
        from datetime import datetime as real_dt
        from zoneinfo import ZoneInfo as _Z

        class PastMidnight(real_dt):
            @classmethod
            def now(cls, tz=None):
                return real_dt(2026, 8, 4, 2, 5, tzinfo=tz or _Z("Europe/London"))

        with patch("app.core.router.datetime", PastMidnight):
            await b.h.router.handle_update(
                text_update("right im actually off to sleep now speak tomorrow", OWNER)
            )
        row = await self.db.fetch_one("SELECT day FROM sleep_log")
        self.assertEqual(row["day"], "2026-08-03")   # closes YESTERDAY's night
        self.assertIn("Goodnight", b.sent())

    async def test_rhythm_state_carries_the_clock_truth(self):
        b = BrainHarness(self.db, opus_responses=[[text_block("Evening, sir.")]])
        await b.h.router.handle_update(text_update("evening", OWNER))
        opus = [r for r in b.script.requests if "haiku" not in r["model"]][0]
        self.assertIn("Clocks: Europe/London", opus["system"])
        self.assertIn("timezone_place", opus["system"])

    async def test_brain_system_carries_the_hands_rules(self):
        b = BrainHarness(self.db, opus_responses=[[text_block("Evening, sir.")]])
        await b.h.router.handle_update(text_update("evening mate, how are we", OWNER))
        opus = [r for r in b.script.requests if "haiku" not in r["model"]][0]
        self.assertIn("YOUR HANDS", opus["system"])
        self.assertIn("tool results are ground truth", opus["system"])
        names = [t["name"] for t in opus["tools"]]
        self.assertEqual(
            set(names) & {"trello", "rhythm", "build_list", "update_brief"},
            {"trello", "rhythm", "build_list", "update_brief"},
        )

    async def test_no_action_instruction_changes_nothing_on_the_board(self):
        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("trello", instruction="what's the weather")],
                [text_block("Nothing to do on the board there, sir.")],
            ],
            parser_actions=[],
        )
        await b.h.router.handle_update(text_update("odd one but check the board thing", OWNER))
        self.assertEqual([w for w in b.board.trello_writes if w[1] == "/1/cards"], [])
        results = [
            blk for r in b.script.requests for m in r["messages"]
            if isinstance(m.get("content"), list)
            for blk in m["content"] if isinstance(blk, dict) and blk.get("type") == "tool_result"
        ]
        self.assertTrue(any("NO ACTION RECOGNISED" in str(blk["content"]) for blk in results))


if __name__ == "__main__":
    unittest.main()
