import os
import tempfile
import unittest
from datetime import date
from zoneinfo import ZoneInfo

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.telegram_client import TelegramClient
from app.config import Settings
from app.core.store import SettingsStore
from app.heartbeat.calendar_ics import parse_ics_events, travel_or_event_flags
from app.heartbeat.emailer import Emailer
from app.heartbeat.jobs import (
    HeartbeatJobs,
    assert_no_private_content,
    compose_kiefer_data,
    compose_morning_skeleton,
)
from app.heartbeat.streaks import Streaks, detect_activities
from app.db.sqlite import SqliteDatabase

TODAY = date(2026, 7, 25)


class TestStreaks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.streaks = Streaks(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_consecutive_days_extend(self):
        await self.streaks.record("run", date(2026, 7, 23))
        await self.streaks.record("run", date(2026, 7, 24))
        result = await self.streaks.record("run", TODAY)
        self.assertEqual(result["current"], 3)
        self.assertEqual(result["best"], 3)

    async def test_same_day_idempotent(self):
        await self.streaks.record("run", TODAY)
        result = await self.streaks.record("run", TODAY)
        self.assertEqual(result["current"], 1)
        self.assertFalse(result["changed"])

    async def test_gap_resets_but_best_remains(self):
        for d in (21, 22, 23):
            await self.streaks.record("workout", date(2026, 7, d))
        result = await self.streaks.record("workout", TODAY)  # 24th missed
        self.assertEqual(result["current"], 1)
        self.assertEqual(result["best"], 3)

    async def test_snapshot_shows_broken_streak_as_zero(self):
        await self.streaks.record("meals", date(2026, 7, 20))
        snap = await self.streaks.snapshot(TODAY)
        self.assertEqual(snap["meals"]["current"], 0)
        self.assertEqual(snap["meals"]["best"], 1)
        self.assertFalse(snap["meals"]["done_today"])

    def test_activity_detection(self):
        self.assertEqual(detect_activities("run done, felt strong"), ["run"])
        self.assertEqual(detect_activities("just ran the 5k"), ["run"])
        self.assertEqual(detect_activities("smashed the workout"), ["workout"])
        self.assertEqual(detect_activities("push day done"), ["workout"])
        self.assertEqual(detect_activities("meals all on plan today"), ["meals"])
        self.assertEqual(
            set(detect_activities("did my run and finished the gym session, meals on plan")),
            {"run", "workout", "meals"},
        )
        self.assertEqual(detect_activities("what's the plan for tomorrow?"), [])
        self.assertEqual(detect_activities("I should run more"), [])


ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260725T090000Z
SUMMARY:Flight EK18 MAN-DXB
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Europe/London:20260725T150000
SUMMARY:Call with Kiefer
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260725
SUMMARY:Kids with Paul
END:VEVENT
BEGIN:VEVENT
DTSTART:20260726T100000Z
SUMMARY:Tomorrow thing
END:VEVENT
END:VCALENDAR
"""


class TestCalendar(unittest.TestCase):
    def test_parses_today_only_sorted(self):
        events = parse_ics_events(ICS, TODAY, ZoneInfo("Europe/London"))
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["title"], "Kids with Paul")
        self.assertEqual(events[0]["time"], "all day")
        self.assertEqual(events[1], {"time": "10:00", "title": "Flight EK18 MAN-DXB"})  # 09Z = 10:00 BST
        self.assertEqual(events[2], {"time": "15:00", "title": "Call with Kiefer"})

    def test_travel_flags(self):
        events = parse_ics_events(ICS, TODAY, ZoneInfo("Europe/London"))
        flags = travel_or_event_flags(events)
        self.assertEqual(flags, ["Flight EK18 MAN-DXB"])


class JobsHarness:
    def __init__(self, db, flourish="Right then, Paul."):
        self.telegram_calls = []

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            self.telegram_calls.append(
                (request.url.path.split("/")[-1], request.content.decode(errors="replace"))
            )
            return httpx.Response(200, json={"ok": True, "result": {}})

        def claude_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": [{"type": "text", "text": flourish}]})

        self.jobs = HeartbeatJobs(
            settings=Settings(telegram_owner_chat_id=111, _env_file=None),
            db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler)),
            claude=ClaudeClient("K", transport=httpx.MockTransport(claude_handler)),
            elevenlabs=None,
            daily12=None,
            calendar=None,
            emailer=None,
            kiefer_email="",
        )

    def sent_texts(self):
        return [body for method, body in self.telegram_calls if method == "sendMessage"]


class TestJobs(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = JobsHarness(self.db)
        self.jobs = self.h.jobs
        self.today = (await self.jobs._today()).isoformat()

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def _plant_twelve(self, done: int, total: int = 12):
        for i in range(total):
            await self.db.execute(
                "INSERT INTO tasks (trello_id, title) VALUES (?, ?)", (f"T{i}", f"task {i}")
            )
            await self.db.execute(
                "INSERT INTO daily_12 (plan_date, position, task_id, company_slug, done)"
                " VALUES (?, ?, ?, 'derma_uk', ?)",
                (self.today, i + 1, i + 1, 1 if i < done else 0),
            )

    async def test_morning_brief_sends_skeleton_and_streaks(self):
        await self.jobs.morning_brief()
        [text] = self.h.sent_texts()
        self.assertIn("YOUR+DAY", text.replace("%E2%80%94", "").replace("+", "+"))
        self.assertIn("5km+run", text)
        self.assertIn("STREAKS", text)

    async def test_midday_on_track_no_hound(self):
        await self._plant_twelve(done=6)
        await self.jobs.streaks.record("run", date.fromisoformat(self.today))
        await self.jobs.midday_nudge()
        self.assertFalse(await self.jobs.hound_active())

    async def test_midday_behind_triggers_hound(self):
        await self._plant_twelve(done=1)
        await self.jobs.midday_nudge()
        self.assertTrue(await self.jobs.hound_active())

    async def test_midday_missed_run_triggers_hound_even_if_tasks_fine(self):
        await self._plant_twelve(done=8)
        await self.jobs.midday_nudge()
        self.assertTrue(await self.jobs.hound_active())

    async def test_hound_ping_silent_when_inactive(self):
        await self.jobs.hound_ping()
        self.assertEqual(self.h.telegram_calls, [])

    async def test_hound_stands_down_when_board_clears(self):
        await self._plant_twelve(done=12)
        await self.jobs.set_hound(True)
        await self.jobs.hound_ping()
        self.assertFalse(await self.jobs.hound_active())

    async def test_completing_the_twelve_increments_streak(self):
        await self._plant_twelve(done=12)
        await self.jobs._twelve_progress(date.fromisoformat(self.today))
        snap = await self.jobs.streaks.snapshot(date.fromisoformat(self.today))
        self.assertEqual(snap["twelve"]["current"], 1)

    async def test_evening_review_sends_summary_and_private_checkin(self):
        await self._plant_twelve(done=9)
        await self.jobs.evening_review()
        texts = self.h.sent_texts()
        self.assertTrue(any("9%2F12" in t or "9/12" in t for t in texts))
        # the separate gentle check-in went out too (as text; no TTS in harness)
        self.assertTrue(any("in+yourself" in t for t in texts))

    async def test_run_protect_skips_if_run_done(self):
        await self.jobs.streaks.record("run", date.fromisoformat(self.today))
        await self.jobs.run_protect()
        self.assertEqual(self.h.telegram_calls, [])

    async def test_kiefer_note_skipped_without_email_config(self):
        self.assertFalse(await self.jobs.kiefer_note())


class TestKieferGuard(unittest.TestCase):
    def test_clean_note_passes(self):
        assert_no_private_content("Paul cleared 11 of 12 and his run streak hit 6. — Jarvis")

    def test_private_markers_blocked(self):
        for bad in (
            "Paul is 40 days sober today!",
            "He did his sobriety check-in",
            "Took his ADHD medication and TRT on time",
        ):
            with self.assertRaises(RuntimeError):
                assert_no_private_content(bad)

    def test_kiefer_data_contains_only_business(self):
        snapshot = {
            t: {"current": 3, "best": 5, "done_today": True}
            for t in ("run", "workout", "twelve", "meals")
        }
        data = compose_kiefer_data(TODAY, 12, 12, snapshot)
        assert_no_private_content(data)
        self.assertIn("12/12", data)

    def test_morning_skeleton_composition(self):
        snapshot = {
            t: {"current": 2, "best": 4, "done_today": False}
            for t in ("run", "workout", "twelve", "meals")
        }
        text = compose_morning_skeleton(TODAY, [{"time": "15:00", "title": "Call"}], snapshot)
        self.assertIn("5km run", text)
        self.assertIn("15:00", text)
        self.assertIn("Run 2", text)


class TestEmailer(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_returns_false(self):
        emailer = Emailer("", "")
        self.assertFalse(emailer.configured)
        self.assertFalse(await emailer.send("k@x.com", "s", "b"))


if __name__ == "__main__":
    unittest.main()
