"""Watch-wearing chaser + smart move reminder (7 Aug build slice).

Watch chaser: no heart-rate sample for watch_gap_minutes = watch off wrist.
One call ever per incident, a Telegram explainer if it goes unanswered, a
silent re-check, then Telegram-only chasing — and Paul's own word stands
the whole thing down. Move reminder: pace-vs-goal exactly like water —
silent on track, one honest line when behind, silent when the watch (and
so the data) is off.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.telegram_client import TelegramClient
from app.config import Settings
from app.core.store import utc_now_iso
from app.db.sqlite import SqliteDatabase
from app.heartbeat.jobs import HeartbeatJobs
from app.heartbeat.wakeup import WAKE_TIME_KEY
from tests.test_wakeup import FakePhone

OWNER = 111
TZ = ZoneInfo("Europe/London")


class Harness:
    def __init__(self, db, phone=None):
        self.telegram_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.telegram_calls.append((request.url.path.split("/")[-1], request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})

        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.jobs = HeartbeatJobs(
            settings=settings, db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(handler)),
            claude=ClaudeClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            phone_channel=phone,
        )

    def texts(self):
        return [
            parse_qs(body.decode())["text"][0]
            for method, body in self.telegram_calls
            if method == "sendMessage"
        ]


class WatchMoveBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.phone = FakePhone()
        self.h = Harness(self.db, phone=self.phone)
        self.today = datetime.now(TZ).date()
        self.noon = datetime.now(TZ).replace(hour=12, minute=5)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def _set_hr_gap_minutes(self, minutes_ago, at=None):
        at = at or self.noon
        stamp = (at.astimezone(ZoneInfo("UTC")) - timedelta(minutes=minutes_ago)).isoformat()
        await self.h.jobs.store.set(self.h.jobs.LAST_HR_KEY, stamp)


class TestWatchChaser(WatchMoveBase):
    async def test_never_seen_hr_data_is_never_chased(self):
        await self.h.jobs.watch_check_tick(self.noon)
        self.assertEqual(self.phone.calls, [])
        self.assertEqual(self.h.texts(), [])

    async def test_fresh_sample_is_silence(self):
        await self._set_hr_gap_minutes(10)
        await self.h.jobs.watch_check_tick(self.noon)
        self.assertEqual(self.phone.calls, [])
        self.assertEqual(self.h.texts(), [])

    async def test_stale_sample_places_one_call_and_waits(self):
        await self._set_hr_gap_minutes(65)
        await self.h.jobs.watch_check_tick(self.noon)
        self.assertEqual(len(self.phone.calls), 1)
        self.assertIn("watch isn't on your wrist", self.phone.calls[0])
        self.assertEqual(self.h.texts(), [])  # give the call a chance first

    async def test_second_tick_unanswered_sends_one_telegram_message(self):
        await self._set_hr_gap_minutes(65)
        await self.h.jobs.watch_check_tick(self.noon)
        await self.h.jobs.watch_check_tick(self.noon + timedelta(minutes=15))
        self.assertEqual(len(self.phone.calls), 1)  # never a second call
        [msg] = self.h.texts()
        self.assertIn("no heart-rate since", msg)
        self.assertIn("watch is off", msg)

    async def test_immediate_retick_after_telegram_stays_silent(self):
        await self._set_hr_gap_minutes(65)
        await self.h.jobs.watch_check_tick(self.noon)
        await self.h.jobs.watch_check_tick(self.noon + timedelta(minutes=15))
        await self.h.jobs.watch_check_tick(self.noon + timedelta(minutes=20))
        self.assertEqual(len(self.h.texts()), 1)  # not due for another hour yet

    async def test_chase_continues_telegram_only_after_an_hour(self):
        await self._set_hr_gap_minutes(65)
        await self.h.jobs.watch_check_tick(self.noon)
        await self.h.jobs.watch_check_tick(self.noon + timedelta(minutes=15))
        await self.h.jobs.watch_check_tick(self.noon + timedelta(hours=1, minutes=20))
        self.assertEqual(len(self.phone.calls), 1)  # still only the one call
        self.assertEqual(len(self.h.texts()), 2)

    async def test_hr_resuming_closes_the_incident_silently(self):
        await self._set_hr_gap_minutes(65)
        await self.h.jobs.watch_check_tick(self.noon)
        await self._set_hr_gap_minutes(2, at=self.noon + timedelta(minutes=30))
        await self.h.jobs.watch_check_tick(self.noon + timedelta(minutes=30))
        self.assertEqual(self.h.texts(), [])  # bath, not a mystery — no announcement
        incident = await self.h.jobs._watch_incident()
        self.assertEqual(incident, {})
        # A fresh gap afterwards is treated as a brand new incident (a new call).
        await self._set_hr_gap_minutes(65, at=self.noon + timedelta(hours=2))
        await self.h.jobs.watch_check_tick(self.noon + timedelta(hours=2))
        self.assertEqual(len(self.phone.calls), 2)

    async def test_standdown_pauses_calls_completely(self):
        await self.h.jobs.stand_down_watch_chase(self.noon)
        await self._set_hr_gap_minutes(65)
        await self.h.jobs.watch_check_tick(self.noon + timedelta(minutes=5))
        self.assertEqual(self.phone.calls, [])
        self.assertEqual(self.h.texts(), [])

    async def test_standdown_expires_after_its_window(self):
        await self.h.jobs.stand_down_watch_chase(self.noon)
        await self._set_hr_gap_minutes(65, at=self.noon + timedelta(hours=1, minutes=5))
        await self.h.jobs.watch_check_tick(self.noon + timedelta(hours=1, minutes=5))
        self.assertEqual(len(self.phone.calls), 1)

    async def test_before_gospel_wake_is_silence(self):
        await self.h.jobs.store.set(WAKE_TIME_KEY, "08:30")
        await self._set_hr_gap_minutes(90, at=datetime.now(TZ).replace(hour=8, minute=4))
        await self.h.jobs.watch_check_tick(datetime.now(TZ).replace(hour=8, minute=4))
        self.assertEqual(self.phone.calls, [])

    async def test_after_goodnight_is_silence(self):
        await self.db.execute(
            "INSERT INTO sleep_log (day, goodnight_time, tz) VALUES (?, ?, 'Europe/London')",
            (self.today.isoformat(), utc_now_iso()),
        )
        await self._set_hr_gap_minutes(90, at=datetime.now(TZ).replace(hour=21, minute=5))
        await self.h.jobs.watch_check_tick(datetime.now(TZ).replace(hour=21, minute=5))
        self.assertEqual(self.phone.calls, [])

    async def test_gate_override_silences_the_chase(self):
        from app.heartbeat.gates import GateKeeper
        from app.heartbeat.streaks import Streaks

        self.h.jobs.gates = GateKeeper(self.db, Streaks(self.db))
        await self.h.jobs.gates.override(["watch"], "travelling without it", self.today)
        await self._set_hr_gap_minutes(90)
        await self.h.jobs.watch_check_tick(self.noon)
        self.assertEqual(self.phone.calls, [])

    async def test_test_trigger_places_a_call_without_touching_incident(self):
        result = await self.h.jobs.test_watch_chase()
        self.assertIn("Test call placed", result)
        self.assertEqual(len(self.phone.calls), 1)
        self.assertIn("test of the watch chase", self.phone.calls[0])
        self.assertEqual(await self.h.jobs._watch_incident(), {})

    async def test_test_trigger_without_phone_configured_says_so(self):
        no_phone_jobs = Harness(self.db, phone=None).jobs
        result = await no_phone_jobs.test_watch_chase()
        self.assertIn("isn't configured", result)


class TestMovePace(WatchMoveBase):
    async def _log_kcal(self, kcal):
        from app.main import merge_active_kcal_total

        await merge_active_kcal_total(self.db, self.today.isoformat(), kcal)

    async def test_ahead_of_the_curve_is_silence(self):
        # goal 500 kcal / 14h ≈ 35.7/hr; awake since 08:00 default → ~146 expected by noon.
        await self._log_kcal(300)
        await self.h.jobs.move_pace_nudge(self.noon)
        self.assertEqual(self.h.texts(), [])

    async def test_slightly_behind_is_still_silence(self):
        await self._log_kcal(120)
        await self.h.jobs.move_pace_nudge(self.noon)
        self.assertEqual(self.h.texts(), [])

    async def test_a_full_hour_behind_earns_one_line(self):
        await self._log_kcal(10)
        await self.h.jobs.move_pace_nudge(self.noon)
        [nudge] = self.h.texts()
        self.assertIn("behind the move pace", nudge)
        self.assertIn("kcal", nudge)

    async def test_watch_off_suppresses_the_nudge_entirely(self):
        await self._set_hr_gap_minutes(90)
        await self._log_kcal(0)
        await self.h.jobs.move_pace_nudge(self.noon)
        self.assertEqual(self.h.texts(), [])

    async def test_fresh_hr_sample_does_not_suppress(self):
        await self._set_hr_gap_minutes(5)
        await self._log_kcal(0)
        await self.h.jobs.move_pace_nudge(self.noon)
        self.assertEqual(len(self.h.texts()), 1)

    async def test_before_gospel_wake_is_silence(self):
        await self.h.jobs.store.set(WAKE_TIME_KEY, "08:30")
        eight_oh_four = datetime.now(TZ).replace(hour=8, minute=4)
        await self.h.jobs.move_pace_nudge(eight_oh_four)
        self.assertEqual(self.h.texts(), [])

    async def test_after_goodnight_is_silence(self):
        await self.db.execute(
            "INSERT INTO sleep_log (day, goodnight_time, tz) VALUES (?, ?, 'Europe/London')",
            (self.today.isoformat(), utc_now_iso()),
        )
        await self.h.jobs.move_pace_nudge(datetime.now(TZ).replace(hour=21, minute=5))
        self.assertEqual(self.h.texts(), [])

    async def test_status_math(self):
        status = await self.h.jobs.move_pace_status(self.noon)
        self.assertAlmostEqual(status["pace"], 500 / 14.0, places=1)
        self.assertEqual(status["actual"], 0)

    async def test_test_trigger_always_sends_a_line(self):
        result = await self.h.jobs.test_move_reminder()
        self.assertIn("Move pace", result)
        self.assertEqual(len(self.h.texts()), 1)


if __name__ == "__main__":
    unittest.main()
