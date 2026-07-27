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


ICS_WORK = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;TZID=Europe/London:20260725T113000
SUMMARY:Prodermis distributor call
END:VEVENT
END:VCALENDAR
"""


class TestMultiCalendar(unittest.IsolatedAsyncioTestCase):
    async def test_merges_events_from_all_calendar_urls(self):
        from app.heartbeat.calendar_ics import IcsCalendar

        def handler(request: httpx.Request) -> httpx.Response:
            body = ICS if "personal" in str(request.url) else ICS_WORK
            return httpx.Response(200, text=body)

        calendar = IcsCalendar(
            "https://cal.example/personal.ics, https://cal.example/work.ics",
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(calendar.calendar_count, 2)
        events = await calendar.events_for(TODAY, ZoneInfo("Europe/London"))
        titles = [e["title"] for e in events]
        self.assertIn("Call with Kiefer", titles)          # personal calendar
        self.assertIn("Prodermis distributor call", titles)  # work calendar
        times = [e["time"] for e in events]
        self.assertEqual(times, sorted(times, key=lambda t: "00:00" if t == "all day" else t))
        await calendar.close()

    async def test_one_dead_calendar_does_not_sink_the_rest(self):
        from app.heartbeat.calendar_ics import IcsCalendar

        def handler(request: httpx.Request) -> httpx.Response:
            if "dead" in str(request.url):
                return httpx.Response(500, text="gone")
            return httpx.Response(200, text=ICS_WORK)

        calendar = IcsCalendar(
            "https://cal.example/dead.ics https://cal.example/work.ics",
            transport=httpx.MockTransport(handler),
        )
        events = await calendar.events_for(TODAY, ZoneInfo("Europe/London"))
        self.assertEqual([e["title"] for e in events], ["Prodermis distributor call"])
        await calendar.close()


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


class TestNudgeIdempotency(unittest.IsolatedAsyncioTestCase):
    """Master Update §14: a proactive message asks once — never a repeat loop."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = JobsHarness(self.db)
        self.jobs = self.h.jobs

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_identical_message_never_fires_twice(self):
        await self.jobs._send_text("Sir, the metrics remain at zero. Shall we begin?")
        await self.jobs._send_text("Sir, the metrics remain at zero. Shall we begin?")
        self.assertEqual(len(self.h.sent_texts()), 1)

    async def test_different_messages_still_flow(self):
        await self.jobs._send_text("First message.")
        await self.jobs._send_text("Second, different message.")
        self.assertEqual(len(self.h.sent_texts()), 2)

    async def test_midday_nudge_fires_once_per_day(self):
        await self.jobs.midday_nudge()
        first = len(self.h.sent_texts())
        self.assertEqual(first, 1)
        await self.jobs.midday_nudge()
        await self.jobs.midday_nudge()
        self.assertEqual(len(self.h.sent_texts()), first)

    async def test_evening_review_fires_once_per_day(self):
        await self.jobs.evening_review()
        first = len(self.h.sent_texts())
        await self.jobs.evening_review()
        self.assertEqual(len(self.h.sent_texts()), first)


LONDON = ZoneInfo("Europe/London")


def at_local(hhmm: str, day: int = 25) -> "datetime":
    from datetime import datetime as _dt

    hour, minute = map(int, hhmm.split(":"))
    return _dt(2026, 7, day, hour, minute, tzinfo=LONDON)


class TestDayRhythm(unittest.IsolatedAsyncioTestCase):
    """Master Update §4 (wake, built but OFF), §5 (move+water), §6 (meds)."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = JobsHarness(self.db)
        self.jobs = self.h.jobs

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    def sent(self):
        return [b for m, b in self.h.telegram_calls if m in ("sendMessage", "sendVoice")]

    async def test_wake_is_off_by_default(self):
        await self.jobs.wake_tick(at_local("05:30"))
        self.assertEqual(self.sent(), [])

    async def test_armed_wake_escalates_until_selfie(self):
        await self.jobs.set_wake_enabled(True)
        await self.jobs.wake_tick(at_local("05:06"))
        await self.jobs.wake_tick(at_local("05:09"))
        self.assertEqual(len(self.sent()), 2)   # the loop repeats — no dedupe here
        await self.jobs.record_wake(at_local("05:10").date(), "selfie", photo_ref="F1")
        await self.jobs.wake_tick(at_local("05:12"))
        self.assertEqual(len(self.sent()), 2)   # confirmed → silence
        row = await self.db.fetch_one("SELECT method, photo_ref FROM wake_log")
        self.assertEqual((row["method"], row["photo_ref"]), ("selfie", "F1"))

    async def test_wake_respects_window(self):
        await self.jobs.set_wake_enabled(True)
        await self.jobs.wake_tick(at_local("04:57"))
        await self.jobs.wake_tick(at_local("09:03"))
        self.assertEqual(self.sent(), [])

    async def test_travel_skip_sits_out_exactly_one_day(self):
        await self.jobs.set_wake_enabled(True)
        await self.jobs.skip_next_wake(at_local("05:00", day=26).date())
        await self.jobs.wake_tick(at_local("05:06", day=26))
        self.assertEqual(self.sent(), [])       # skipped — overnight flight
        await self.jobs.wake_tick(at_local("05:06", day=27))
        self.assertEqual(len(self.sent()), 1)   # back on automatically

    async def test_override_stops_the_morning(self):
        from app.heartbeat.gates import GateKeeper
        from app.heartbeat.streaks import Streaks

        self.jobs.gates = GateKeeper(self.db, Streaks(self.db))
        await self.jobs.set_wake_enabled(True)
        await self.jobs.gates.override(["wake"], "red-eye landed at 3am", at_local("05:00").date())
        await self.jobs.wake_tick(at_local("05:06"))
        self.assertEqual(self.sent(), [])

    async def test_hourly_nudge_once_per_hour_with_totals(self):
        day = at_local("10:05").date()
        await self.jobs.log_water(day, 600)
        await self.jobs.log_movement(day)
        await self.jobs.move_water_nudge(at_local("10:05"))
        self.assertEqual(len(self.h.sent_texts()), 1)
        self.assertIn("0.6L", self.h.sent_texts()[0])
        await self.jobs.move_water_nudge(at_local("10:07"))
        self.assertEqual(len(self.h.sent_texts()), 1)   # same hour → once
        await self.jobs.move_water_nudge(at_local("11:05"))
        self.assertEqual(len(self.h.sent_texts()), 2)   # next hour → again

    async def test_no_nudges_after_goodnight(self):
        day = at_local("15:05").date()
        await self.db.execute(
            "INSERT INTO sleep_log (day, goodnight_time, tz) VALUES (?, ?, ?)",
            (day.isoformat(), "2026-07-25T21:30:00+00:00", "Europe/London"),
        )
        await self.jobs.move_water_nudge(at_local("22:05"))
        self.assertEqual(self.h.sent_texts(), [])

    async def test_med_reminder_until_taken(self):
        await self.jobs.med_reminder("adhd", at_local("09:30"))
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("ADHD", self.sent()[0])
        await self.jobs.record_med(at_local("09:30").date(), "adhd")
        self.h.telegram_calls.clear()
        # Simulate the real 24h gap: age out the identical-message dedupe stamp.
        await self.db.execute("DELETE FROM nudge_state WHERE nudge_key LIKE 'msg:%'")
        await self.jobs.med_reminder("adhd", at_local("09:30", day=26))
        self.assertEqual(len(self.sent()), 1)   # next day reminds again

    async def test_trt_only_on_saturdays(self):
        await self.jobs.med_reminder("trt", at_local("10:00", day=26))  # Sunday
        self.assertEqual(self.sent(), [])
        await self.jobs.med_reminder("trt", at_local("10:00", day=25))  # Saturday
        self.assertEqual(len(self.sent()), 1)


class TestDayRhythmRouter(unittest.IsolatedAsyncioTestCase):
    """Voice controls: goodnight, wake arming, travel skip, water logging."""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        from tests.test_router import RouterHarness

        self.h = RouterHarness(self.db)
        self.jobs_harness = JobsHarness(self.db)
        self.jobs = self.jobs_harness.jobs
        self.h.router.heartbeat = self.jobs

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    def texts(self):
        from urllib.parse import parse_qs

        return [
            parse_qs(body.decode())["text"][0]
            for method, body in self.h.telegram_calls
            if method == "sendMessage"
        ]

    async def test_goodnight_closes_the_day(self):
        from tests.test_router import OWNER
        from tests.test_telegram_client import text_update

        await self.h.router.handle_update(text_update("Goodnight Jarvis", OWNER))
        row = await self.db.fetch_one("SELECT day, tz FROM sleep_log")
        self.assertIsNotNone(row)
        self.assertEqual(row["tz"], "Europe/London")
        self.assertIn("Goodnight", " ".join(self.texts()))

    async def test_start_and_stop_the_wake_ups(self):
        from tests.test_router import OWNER
        from tests.test_telegram_client import text_update

        await self.h.router.handle_update(text_update("start the wake-ups", OWNER))
        self.assertTrue(await self.jobs.wake_enabled())
        self.assertIn("05:00", " ".join(self.texts()))
        await self.h.router.handle_update(text_update("stop the wake ups", OWNER))
        self.assertFalse(await self.jobs.wake_enabled())

    async def test_no_wake_up_tomorrow_sets_the_skip(self):
        from datetime import timedelta as td

        from tests.test_router import OWNER
        from tests.test_telegram_client import text_update

        await self.h.router.handle_update(
            text_update("no wake-up tomorrow, I'm flying overnight", OWNER)
        )
        tomorrow = (await self.jobs._today()) + td(days=1)
        self.assertEqual(
            await self.jobs.store.get("wake_skip_date"), tomorrow.isoformat()
        )
        self.assertIn("re-arms automatically", " ".join(self.texts()))

    async def test_goodnight_wake_me_as_normal_arms_tomorrow_only(self):
        from tests.test_router import OWNER
        from tests.test_telegram_client import text_update

        await self.h.router.handle_update(
            text_update("goodnight, wake me up tomorrow as normal", OWNER)
        )
        self.assertFalse(await self.jobs.wake_enabled())   # no standing commitment
        from datetime import datetime as dt, timedelta as td

        real_tomorrow = dt.now(LONDON) + td(days=1)
        armed = real_tomorrow.replace(hour=5, minute=6)
        self.assertTrue(await self.jobs.wake_pending(armed))
        self.assertIn("armed", " ".join(self.texts()).lower())
        row = await self.db.fetch_one("SELECT day FROM sleep_log")
        self.assertIsNotNone(row)   # the goodnight itself was logged too

    async def test_recovery_day_logged_supportively(self):
        from tests.test_router import OWNER
        from tests.test_telegram_client import text_update

        await self.h.router.handle_update(text_update("taking a rest day today", OWNER))
        row = await self.db.fetch_one("SELECT kind FROM activity_log WHERE kind = 'recovery'")
        self.assertIsNotNone(row)
        self.assertIn("Recovery day logged", " ".join(self.texts()))

    async def test_focus_sprint_starts_buzzes_and_completes(self):
        import asyncio

        from app.core.router import JarvisRouter
        from tests.test_router import OWNER
        from tests.test_telegram_client import text_update

        old = JarvisRouter.SPRINT_MINUTE_SECONDS
        JarvisRouter.SPRINT_MINUTE_SECONDS = 0.002
        try:
            await self.h.router.handle_update(
                text_update("start a 25 minute sprint on the BMI email", OWNER)
            )
            row = await self.db.fetch_one("SELECT length_min, task FROM focus_sprints")
            self.assertEqual(row["length_min"], 25)
            self.assertIn("BMI email", row["task"])
            self.assertIn("25 minutes", " ".join(self.texts()))
            await asyncio.sleep(0.2)   # let the buzzer fire
            self.assertIn("Buzzer", " ".join(self.texts()))
            await self.h.router.handle_update(text_update("sprint done, went well", OWNER))
            row = await self.db.fetch_one("SELECT completed FROM focus_sprints")
            self.assertEqual(row["completed"], 1)
        finally:
            JarvisRouter.SPRINT_MINUTE_SECONDS = old

    async def test_just_start_gets_a_five_minute_timer(self):
        from tests.test_router import OWNER
        from tests.test_telegram_client import text_update

        await self.h.router.handle_update(
            text_update("I'm dreading the VAT return, help me start", OWNER)
        )
        row = await self.db.fetch_one("SELECT length_min FROM focus_sprints")
        self.assertEqual(row["length_min"], 5)
        self.assertIn("Smallest possible first step", " ".join(self.texts()))

    async def test_portuguese_practice_extends_streak(self):
        from tests.test_router import OWNER
        from tests.test_telegram_client import text_update

        await self.h.router.handle_update(text_update("practised my portuguese", OWNER))
        today = await self.jobs._today()
        self.assertTrue(await self.h.router.streaks.done_today("portuguese", today))
        self.assertIn("Portuguese streak: 1", " ".join(self.texts()))

    async def test_bedtime_nudge_once_and_respects_goodnight(self):
        await self.jobs.bedtime_nudge()
        self.assertEqual(len(self.h_jobs_texts()), 1)
        self.assertIn("Wind-down", self.h_jobs_texts()[0])
        await self.jobs.bedtime_nudge()
        self.assertEqual(len(self.h_jobs_texts()), 1)   # once per night

    def h_jobs_texts(self):
        # heartbeat jobs go through the JobsHarness telegram, not the router's
        from urllib.parse import unquote_plus

        return [
            unquote_plus(b)
            for m, b in self.jobs_harness.telegram_calls
            if m in ("sendMessage", "sendVoice")
        ]

    async def test_evening_review_leads_with_wins(self):
        today = await self.jobs._today()
        await self.jobs.log_water(today, 1200)
        await self.jobs.evening_review()
        combined = " ".join(self.h_jobs_texts())
        self.assertIn("TODAY'S WINS", combined)
        self.assertIn("1.2L water", combined)

    async def test_water_and_movement_logged_by_voice(self):
        from tests.test_router import OWNER
        from tests.test_telegram_client import text_update

        await self.h.router.handle_update(text_update("500ml down", OWNER))
        today = await self.jobs._today()
        self.assertEqual(await self.jobs.water_total(today), 500)
        await self.h.router.handle_update(text_update("moved", OWNER))
        self.assertEqual(await self.jobs.movement_total(today), 1)


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
        await self.jobs._focus_progress(date.fromisoformat(self.today))
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
            for t in ("run", "workout", "twelve", "meals", "portuguese")
        }
        data = compose_kiefer_data(
            TODAY, 12, 12, snapshot, {"runs": 9, "workouts": 7, "recovery_days": 3}
        )
        assert_no_private_content(data)
        self.assertIn("12/12", data)
        self.assertIn("9 runs", data)   # recovery-aware monthly framing

    def test_morning_skeleton_composition(self):
        snapshot = {
            t: {"current": 2, "best": 4, "done_today": False}
            for t in ("run", "workout", "twelve", "meals", "portuguese")
        }
        text = compose_morning_skeleton(
            TODAY, [{"time": "15:00", "title": "Call"}], snapshot,
            {"runs": 4, "workouts": 3, "recovery_days": 1},
        )
        self.assertIn("5km run", text)
        self.assertIn("15:00", text)
        self.assertIn("Today's Focus 2", text)      # daily streaks
        self.assertIn("4 runs · 3 workouts", text)  # monthly counts
        self.assertIn("rest counts", text)


class TestEmailer(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_returns_false(self):
        emailer = Emailer("", "")
        self.assertFalse(emailer.configured)
        self.assertFalse(await emailer.send("k@x.com", "s", "b"))


if __name__ == "__main__":
    unittest.main()
