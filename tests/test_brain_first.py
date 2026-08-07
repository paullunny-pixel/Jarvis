"""Brain-first (Phase A2): the brain drives the machinery through tools.
Covers the client tool loop, the router's tool belt end-to-end (Trello create,
rhythm switches, remember), and the no-phantom guarantees around them."""
import json
import os
import tempfile
import unittest
from datetime import datetime

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

    async def test_fuzzy_board_talk_drafts_not_writes_the_card(self):
        # No 'Jarvis add to Trello' prefix, typos and all — the brain routes
        # it, but a NEW card from conversation is drafted, not written (7
        # Aug: brainstorming out loud once produced 3 near-duplicate cards
        # with no chance to say no).
        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("trello", instruction="Create a card: Pay the BMI invoice — £480, urgent, for Paul")],
                [text_block("I'd put 'Pay the BMI invoice — £480' on the board for you, flagged urgent — say the word and I'll add it.")],
            ],
            parser_actions=[{"action": "create", "title": "Pay the BMI invoice — £480",
                            "assignee": "paul", "domain": "prodermis", "flags": ["urgent", "money"]}],
        )
        await b.h.router.handle_update(
            text_update("cna you put the bmi invocie on the borad, urgent one", OWNER)
        )
        creates = [w for w in b.board.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(len(creates), 0)  # nothing written yet
        self.assertIn("say the word", b.sent())
        pending = await b.board.service.pending_creates_preview()
        self.assertEqual(len(pending), 1)
        self.assertIn("BMI invoice", pending[0]["title"])

    async def test_confirming_a_draft_writes_exactly_one_card(self):
        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("trello", instruction="Create a card: Pay the BMI invoice — £480")],
                [text_block("Drafted — say the word.")],
                [tool_use("trello", instruction="confirm the pending card")],
                [text_block("Done — it's on the board now.")],
            ],
            parser_actions=[{"action": "create", "title": "Pay the BMI invoice — £480"}],
        )
        await b.h.router.handle_update(text_update("put the bmi invoice on the board", OWNER))
        await b.h.router.handle_update(text_update("yes go ahead", OWNER))
        creates = [w for w in b.board.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(len(creates), 1)
        self.assertIn("BMI invoice", creates[0][2]["name"])
        self.assertEqual(await b.board.service.pending_creates_preview(), [])

    async def test_cancelling_a_draft_writes_nothing(self):
        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("trello", instruction="Create a card: Pay the BMI invoice — £480")],
                [text_block("Drafted — say the word.")],
                [tool_use("trello", instruction="cancel the pending card")],
                [text_block("Dropped, never touched the board.")],
            ],
            parser_actions=[{"action": "create", "title": "Pay the BMI invoice — £480"}],
        )
        await b.h.router.handle_update(text_update("put the bmi invoice on the board", OWNER))
        await b.h.router.handle_update(text_update("no, never mind", OWNER))
        creates = [w for w in b.board.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(len(creates), 0)
        self.assertEqual(await b.board.service.pending_creates_preview(), [])

    async def test_repeated_conversational_creates_stage_without_duplicating(self):
        # The original bug: an evolving conversation re-called the trello
        # tool three times for slightly-reworded versions of the same plan.
        # Now none of them touch Trello until Paul actually says yes.
        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("trello", instruction="Create a card: Book travel option A")],
                [text_block("Drafted option A.")],
                [tool_use("trello", instruction="Create a card: Book travel option A, revised")],
                [text_block("Drafted the revised version too.")],
                [tool_use("trello", instruction="Create a card: Book travel option A, final")],
                [text_block("Drafted the final version.")],
            ],
            parser_actions=[{"action": "create", "title": "Book travel option A"}],
        )
        for msg in ("book it one way", "actually here's a better plan", "final version now"):
            await b.h.router.handle_update(text_update(msg, OWNER))
        creates = [w for w in b.board.trello_writes if w[1] == "/1/cards"]
        self.assertEqual(len(creates), 0)

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

    async def test_rhythm_tool_stands_down_the_watch_chase(self):
        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("rhythm", watch_standdown=True)],
                [text_block("No bother, sir — I'll leave the watch alone for a bit.")],
            ],
        )
        await b.h.router.handle_update(
            text_update("just so you know I'm at dinner, watch is upstairs", OWNER)
        )
        self.assertTrue(await b.jobs._watch_standdown_active(datetime.now(await b.jobs._tz())))
        self.assertIn("No bother", b.sent())

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

    async def test_rhythm_tool_can_no_longer_move_the_clocks(self):
        # Paul's rule (6 Aug): only his exact 'set my location as X' command
        # changes timezone. Even a brain determined to pass timezone_place
        # finds the lever gone — the input is ignored, the clocks stand.
        from app.core.store import SettingsStore

        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("rhythm", timezone_place="dubai")],
                [text_block("To move the clocks, tell me: set my location as Dubai.")],
            ],
        )
        await b.h.router.handle_update(
            text_update("mate those bedtime pings came at half one in the morning here", OWNER)
        )
        self.assertFalse(await SettingsStore(self.db).get("current_timezone"))
        self.assertIn("set my location", b.sent())

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
        # The brain is told the clocks are NOT its to move — only Paul's
        # explicit command (6 Aug rule).
        self.assertIn("set my location as X", opus["system"])
        self.assertNotIn("timezone_place", opus["system"])

    async def test_paul_saying_no_meds_this_week_stands_the_chase_down(self):
        # 4 Aug, 11:16: 'Run and meds were skipping today and we will for this
        # week while I recover' — the run had a lever, meds had none, and the
        # chase kept coming. Paul's no is final now: skip_gates + skip_days.
        import json as _json
        from datetime import datetime as dt, timedelta as td
        from zoneinfo import ZoneInfo as _Z

        from app.heartbeat.gates import GateKeeper
        from app.heartbeat.streaks import Streaks

        b = BrainHarness(
            self.db,
            opus_responses=[
                [tool_use("rhythm", skip_gates=["run", "meds"], skip_days=7,
                          skip_reason="recovery week — walking instead")],
                [text_block("Done — no run or meds chasing this week. Walking counts, "
                            "and I'm with you.")],
            ],
        )
        gates = GateKeeper(self.db, Streaks(self.db))
        b.h.router.gates = gates
        b.jobs.gates = gates
        from app.core.store import SettingsStore

        await SettingsStore(self.db).set("gates_config", _json.dumps(
            [{"id": "run", "label": "the 5km run", "by": "00:00"},
             {"id": "meds", "label": "supplements & medication", "by": "00:00"}]
        ))
        await b.h.router.handle_update(text_update(
            "Run and meds were skipping today and we will for this week I think "
            "while I recover just walking", OWNER,
        ))
        now = dt.now(_Z("Europe/London"))
        self.assertEqual(await gates.outstanding(now), [])                    # today settled
        self.assertTrue(await gates.is_overridden("meds", now.date() + td(days=6)))
        self.assertFalse(await gates.is_overridden("meds", now.date() + td(days=7)))
        # outstanding == [] means the chaser and the owed-NOTE both stand down.
        self.assertIn("no run or meds chasing", b.sent())

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
