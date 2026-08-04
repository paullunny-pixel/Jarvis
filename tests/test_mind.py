"""Phase A3 — the Continuous Mind: hourly thinking passes that usually stay
silent, guards that keep it polite (sleep, wake floor, mid-conversation,
once per hour, off switch), and the nightly reflection whose notes ride
tomorrow's turns."""
import json
import os
import tempfile
import unittest

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.heartbeat.mind import MIND_ENABLED_KEY, MIND_NOTES_KEY, reflect, think
from tests.test_heartbeat import JobsHarness, at_local


def mind_json(**kw):
    base = {"speak": False, "reason": "", "channel": "text", "message": ""}
    base.update(kw)
    return json.dumps(base)


def claude_returning(text):
    return ClaudeClient("K", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"content": [{"type": "text", "text": text}]})
    ))


class TestThink(unittest.IsolatedAsyncioTestCase):
    async def test_speaking_decision_comes_back_clean(self):
        claude = claude_returning(mind_json(
            speak=True, channel="voice", reason="calendar heads-up",
            message="Distributor call in forty minutes, sir — worth surfacing early.",
        ))
        decision = await think(claude, "SITUATION")
        self.assertEqual(decision["channel"], "voice")
        self.assertIn("Distributor call", decision["message"])

    async def test_silence_and_garbage_both_mean_silence(self):
        for text in (mind_json(speak=False),
                     mind_json(speak=True, message=""),   # nothing to say = silence
                     "not json at all"):
            self.assertIsNone(await think(claude_returning(text), "S"), text)

    async def test_api_failure_is_silence_not_a_crash(self):
        claude = ClaudeClient("K", transport=httpx.MockTransport(
            lambda r: httpx.Response(500, text="down")
        ))
        claude.RETRY_WAIT = 0.01
        self.assertIsNone(await think(claude, "S"))

    async def test_reflection_returns_trimmed_notes(self):
        notes = await reflect(claude_returning("  Watch the villa payment. Open gently.  "), "S")
        self.assertEqual(notes, "Watch the villa payment. Open gently.")


class TestMindTick(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db.sqlite import SqliteDatabase

        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    def harness(self, speak=False, **kw):
        return JobsHarness(self.db, flourish=mind_json(speak=speak, **kw))

    def sent(self, h):
        return [b for m, b in h.telegram_calls if m in ("sendMessage", "sendVoice")]

    async def test_silent_pass_sends_nothing(self):
        h = self.harness(speak=False)
        await h.jobs.mind_tick(at_local("14:50"))
        self.assertEqual(self.sent(h), [])

    async def test_speaking_pass_delivers_and_respects_once_per_hour(self):
        h = self.harness(speak=True, channel="text",
                         message="Quiet stretch since lunch — the Sobha payment email is the one to move.")
        await h.jobs.mind_tick(at_local("14:50"))
        self.assertEqual(len(self.sent(h)), 1)
        self.assertIn("Sobha", self.sent(h)[0])
        await h.jobs.mind_tick(at_local("14:55"))
        self.assertEqual(len(self.sent(h)), 1)   # same hour — once
        # Next hour it THINKS again (fresh once-stamp)…
        await h.jobs.mind_tick(at_local("15:50"))
        stamped = await self.db.fetch_one(
            "SELECT nudge_key FROM nudge_state WHERE nudge_key LIKE 'mind:%:15'"
        )
        self.assertIsNotNone(stamped)
        # …but an identical thought is deduped at the funnel: the mind can
        # never nag the same sentence twice. (A fresh thought would send.)
        self.assertEqual(len(self.sent(h)), 1)

    async def test_goodnight_and_night_hours_silence_the_mind(self):
        h = self.harness(speak=True, message="should never send")
        await h.jobs.mind_tick(at_local("23:50"))          # outside waking window
        await self.db.execute(
            "INSERT INTO sleep_log (day, goodnight_time, tz) VALUES (?, ?, ?)",
            (at_local("21:00").date().isoformat(), "2026-07-25T20:00:00+00:00", "Europe/London"),
        )
        await h.jobs.mind_tick(at_local("21:50"))          # goodnight said
        self.assertEqual(self.sent(h), [])

    async def test_wake_floor_and_off_switch_hold_the_mind(self):
        h = self.harness(speak=True, message="should never send")
        await h.jobs.delay_wake(at_local("07:50").date(), 9)
        await h.jobs.mind_tick(at_local("07:50"))          # before his declared nine
        self.assertEqual(self.sent(h), [])
        await h.jobs.store.set(MIND_ENABLED_KEY, "off")
        await h.jobs.mind_tick(at_local("14:50"))
        self.assertEqual(self.sent(h), [])

    async def test_mid_conversation_keeps_the_mind_out(self):
        h = self.harness(speak=True, message="should never send")
        await h.jobs.log.log("in", "just voice-noted you about the BMI order")  # ts = now
        await h.jobs.mind_tick(at_local("14:50"))
        self.assertEqual(self.sent(h), [])

    async def test_situation_carries_the_ground_truth(self):
        h = self.harness()
        await h.jobs.set_quiet_today(True)
        await h.jobs.store.set("paul_brief", "RIGHT NOW: recovery week in Dubai.")
        await h.jobs.log.log("out", "Morning brief text here")
        situation = await h.jobs._mind_situation(at_local("14:50"))
        self.assertIn("QUIET DAY is active", situation)
        self.assertIn("recovery week in Dubai", situation)
        self.assertIn("Morning brief text here", situation)
        self.assertIn("never repeat any of it", situation)

    async def test_nightly_reflection_stores_notes_for_tomorrow(self):
        h = JobsHarness(self.db, flourish="Watch the villa payment. Open the morning gently.")
        await h.jobs.refresh_paul_brief()
        stored = json.loads(await h.jobs.store.get(MIND_NOTES_KEY))
        from datetime import date, timedelta

        self.assertEqual(stored["for"], (date.today() + timedelta(days=1)).isoformat())
        self.assertIn("villa payment", stored["notes"])


class TestMindNotesRideTheBrain(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db.sqlite import SqliteDatabase

        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_todays_notes_are_injected_and_stale_ones_are_not(self):
        from datetime import date, timedelta

        from app.core.store import SettingsStore
        from tests.test_router import OWNER, RouterHarness
        from tests.test_telegram_client import text_update

        h = RouterHarness(self.db)
        store = SettingsStore(self.db)
        await store.set(MIND_NOTES_KEY, json.dumps(
            {"for": date.today().isoformat(), "notes": "Open gently; chase the Sobha email."}
        ))
        await h.router.handle_update(text_update("morning", OWNER))
        system = h.claude_requests[-1]["system"]
        self.assertIn("LAST NIGHT'S REFLECTION", system)
        self.assertIn("chase the Sobha email", system)

        await store.set(MIND_NOTES_KEY, json.dumps(
            {"for": (date.today() - timedelta(days=1)).isoformat(), "notes": "stale"}
        ))
        await h.router.handle_update(text_update("afternoon", OWNER))
        self.assertNotIn("LAST NIGHT'S REFLECTION", h.claude_requests[-1]["system"])


if __name__ == "__main__":
    unittest.main()
