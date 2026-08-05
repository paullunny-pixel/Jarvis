"""Phase 2 chase engine (5 Aug spec): sweep, working-day maths, one numbered
nudge, Kiefer's wall, reply-to-close, escalation, digest, dry-run."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.daily12.chase import (
    LAST_NUDGE_KEY, SWEEP_KEY, NudgeForbidden, working_days_between,
)
from app.db.sqlite import SqliteDatabase
from tests.test_wakeup import FakePhone, Harness

LONDON = ZoneInfo("Europe/London")


class FakeTrelloClient:
    def __init__(self):
        self.calls = []

    async def set_custom_field(self, card_id, field_id, body):
        self.calls.append(("field", card_id, body))

    async def comment(self, card_id, text):
        self.calls.append(("comment", card_id, text))

    async def update_card(self, card_id, **fields):
        self.calls.append(("update", card_id, fields))


class FakeBoardMap:
    fields = {"Last Nudged On": "F-NUDGE"}


class FakeLayer:
    def __init__(self, cards=None):
        self.client = FakeTrelloClient()
        self.cards = cards or []
        self.moves = []

    async def list_snapshot(self, board, list_name):
        return self.cards

    async def entered_current_list_at(self, card_id):
        return next(c["_entered"] for c in self.cards if c["id"] == card_id)

    def board(self, key):
        return FakeBoardMap()

    async def done_week_list(self, board, today):
        return "DONE-LIST"

    async def move_card(self, board, card_id, list_name):
        self.moves.append((card_id, list_name))


class ChaseBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = Harness(self.db, phone=FakePhone())
        self.chase = self.h.jobs.chase
        now = datetime.now(LONDON)
        self.layer = FakeLayer(cards=[
            {"id": "C1", "name": "Packaging check with Sarah", "due": None,
             "_entered": now - timedelta(days=1)},
            {"id": "C2", "name": "Shipping quote for Dubai", "due": None,
             "_entered": now - timedelta(days=2)},
        ])

        async def factory():
            return self.layer

        self.chase._layer_factory = factory

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def _sweep_now(self):
        return await self.chase.sweep("harry", datetime.now(LONDON))


class TestWorkingDays(unittest.TestCase):
    def test_friday_card_is_day_two_on_monday(self):
        fri = datetime(2026, 7, 31, 15, 0, tzinfo=LONDON)   # Friday
        mon = datetime(2026, 8, 3, 8, 30, tzinfo=LONDON)    # Monday
        self.assertEqual(working_days_between(fri, mon, [0, 1, 2, 3, 4]), 2)

    def test_same_day_is_day_one(self):
        d = datetime(2026, 8, 3, 9, 0, tzinfo=LONDON)
        self.assertEqual(working_days_between(d, d, [0, 1, 2, 3, 4]), 1)

    def test_thursday_to_friday_is_day_two(self):
        thu = datetime(2026, 8, 6, 10, 0, tzinfo=LONDON)
        fri = datetime(2026, 8, 7, 8, 30, tzinfo=LONDON)
        self.assertEqual(working_days_between(thu, fri, [0, 1, 2, 3, 4]), 2)


class TestSweepAndNudge(ChaseBase):
    async def test_sweep_observes_and_sends_nothing(self):
        result = await self._sweep_now()
        self.assertEqual(len(result["cards"]), 2)
        self.assertEqual(self.h.texts(), [])   # §6: the sweep is silent

    async def test_nudge_is_one_numbered_message_dry_run_to_paul(self):
        await self._sweep_now()
        weekday = datetime.now(LONDON)
        while weekday.weekday() > 4:
            weekday -= timedelta(days=1)
        sent = await self.chase.nudge("harry", weekday)
        self.assertTrue(sent)
        [msg] = [t for t in self.h.texts() if "DRY-RUN" in t]
        self.assertIn("1. Packaging check with Sarah", msg)
        self.assertIn("2. Shipping quote for Dubai", msg)
        self.assertIn('"1 done"', msg)
        # Last Nudged On stamped + audit comment per card
        kinds = [c[0] for c in self.layer.client.calls]
        self.assertIn("field", kinds)
        self.assertIn("comment", kinds)

    async def test_never_twice_in_one_day(self):
        await self._sweep_now()
        weekday = datetime.now(LONDON)
        while weekday.weekday() > 4:
            weekday -= timedelta(days=1)
        self.assertTrue(await self.chase.nudge("harry", weekday))
        self.assertFalse(await self.chase.nudge("harry", weekday))

    async def test_no_nudge_on_weekends_or_holiday(self):
        await self._sweep_now()
        saturday = datetime(2026, 8, 8, 8, 30, tzinfo=LONDON)
        self.assertFalse(await self.chase.nudge("harry", saturday))
        await self.chase.set_holiday("harry", "2099-01-01")
        monday = datetime(2026, 8, 10, 8, 30, tzinfo=LONDON)
        self.assertFalse(await self.chase.nudge("harry", monday))

    async def test_stale_sweep_never_fires_a_backlog(self):
        await self._sweep_now()
        sweeps = json.loads(await self.h.jobs.store.get(SWEEP_KEY))
        sweeps["harry"]["swept_at"] = (
            datetime.now(LONDON) - timedelta(days=3)
        ).isoformat(timespec="seconds")
        await self.h.jobs.store.set(SWEEP_KEY, json.dumps(sweeps))
        weekday = datetime.now(LONDON)
        while weekday.weekday() > 4:
            weekday -= timedelta(days=1)
        self.assertFalse(await self.chase.nudge("harry", weekday))   # §14

    async def test_clear_list_means_silence(self):
        self.layer.cards = []
        await self._sweep_now()
        self.assertFalse(await self.chase.nudge("harry", datetime.now(LONDON)))


class TestKieferWall(ChaseBase):
    async def test_send_boundary_raises_for_nudge_false(self):
        with self.assertRaises(NudgeForbidden):
            await self.chase._send_to(self.chase.participants["kiefer"], "hi")

    async def test_nudge_job_never_composes_for_kiefer(self):
        await self.chase.sweep("kiefer", datetime.now(ZoneInfo("Asia/Dubai")))
        self.assertFalse(await self.chase.nudge("kiefer"))
        self.assertEqual([t for t in self.h.texts() if "DRY-RUN" in t], [])

    async def test_kiefer_still_appears_in_the_digest(self):
        await self.chase.sweep("kiefer", datetime.now(ZoneInfo("Asia/Dubai")))
        digest = await self.chase.digest()
        self.assertIn("KIEFER", digest)
        self.assertIn("Packaging check", digest)


class TestReplies(ChaseBase):
    async def _nudged(self):
        await self._sweep_now()
        nudges = {"harry": {"date": datetime.now(LONDON).date().isoformat(),
                            "items": ["C1", "C2"]}}
        await self.h.jobs.store.set(LAST_NUDGE_KEY, json.dumps(nudges))

    async def test_one_done_closes_comments_and_confirms(self):
        await self._nudged()
        reply = await self.chase.handle_reply("harry", "1 done")
        self.assertIn("Packaging check", reply)
        update = next(c for c in self.layer.client.calls if c[0] == "update")
        self.assertEqual(update[1], "C1")
        self.assertEqual(update[2]["idList"], "DONE-LIST")
        comment = next(c for c in self.layer.client.calls if c[0] == "comment")
        self.assertIn("Closed via Telegram by Harry", comment[2])

    async def test_blocked_moves_and_records_why(self):
        await self._nudged()
        reply = await self.chase.handle_reply("harry", "2 blocked, waiting on Sarah")
        self.assertIn("Blocked", reply)
        self.assertEqual(self.layer.moves, [("C2", "Blocked / Waiting On")])
        comment = next(c for c in self.layer.client.calls if c[0] == "comment")
        self.assertIn("waiting on Sarah", comment[2])

    async def test_bare_done_with_two_open_asks_not_guesses(self):
        await self._nudged()
        reply = await self.chase.handle_reply("harry", "done")
        self.assertIn("Which one", reply)
        self.assertEqual([c for c in self.layer.client.calls if c[0] == "update"], [])

    async def test_help_escalates_to_paul_immediately(self):
        await self._nudged()
        reply = await self.chase.handle_reply("harry", "1 I need a hand with this")
        self.assertIn("Flagged to Paul", reply)
        self.assertTrue(any("needs a hand" in t for t in self.h.texts()))

    async def test_name_match_works_without_numbers(self):
        await self._nudged()
        reply = await self.chase.handle_reply("harry", "the shipping one is finished")
        self.assertIn("Shipping quote", reply)


class TestDigestAndEscalation(ChaseBase):
    async def test_escalation_flags_stalled_and_blocker(self):
        now = datetime.now(LONDON)
        self.layer.cards = [
            {"id": "C1", "name": "Old one", "due": None,
             "_entered": now - timedelta(days=9)},
            {"id": "C2", "name": "Fresh one", "due": None, "_entered": now},
        ]
        await self._sweep_now()
        digest = await self.chase.digest()
        self.assertIn("BLOCKER", digest)
        self.assertIn("Fresh one — day 1", digest)
        msg = self.chase._compose_nudge(
            self.chase.participants["harry"],
            [{"id": "C1", "name": "Old one", "days": 4}],
        )
        self.assertIn("need a hand", msg)   # §7 day-3+ phrasing shift
