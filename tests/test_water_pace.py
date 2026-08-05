"""Intelligent water pacing (5 Aug): ahead of the curve = silence; a full
hour behind earns one honest line; never outside waking hours."""
import os
import tempfile
import unittest
from datetime import datetime
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

OWNER = 111
TZ = ZoneInfo("Europe/London")


class Harness:
    def __init__(self, db):
        self.telegram_calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.telegram_calls.append((request.url.path.split("/")[-1], request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})

        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.jobs = HeartbeatJobs(
            settings=settings, db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(handler)),
            claude=ClaudeClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
        )

    def texts(self):
        return [
            parse_qs(body.decode())["text"][0]
            for method, body in self.telegram_calls
            if method == "sendMessage"
        ]


class TestWaterPace(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = Harness(self.db)
        self.today = datetime.now(TZ).date()
        self.noon = datetime.now(TZ).replace(hour=12, minute=5)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def _log_water(self, ml):
        await self.h.jobs.log_water(self.today, ml)

    async def test_ahead_of_the_curve_means_silence(self):
        # Awake since 08:00 default → 12:05 expected ≈ 816ml; he's on 1200.
        await self._log_water(1200)
        await self.h.jobs.move_water_nudge(self.noon)
        self.assertEqual(self.h.texts(), [])

    async def test_slightly_behind_still_earns_silence(self):
        # ~816 expected, 700 down: behind, but less than an hour's pace.
        await self._log_water(700)
        await self.h.jobs.move_water_nudge(self.noon)
        self.assertEqual(self.h.texts(), [])

    async def test_a_full_hour_behind_earns_one_honest_line(self):
        await self._log_water(300)   # ~516 behind at 12:05
        await self.h.jobs.move_water_nudge(self.noon)
        [nudge] = self.h.texts()
        self.assertIn("behind the curve", nudge)
        self.assertIn("300ml down", nudge)
        self.assertIn("200ml an hour", nudge)

    async def test_run_day_paces_up(self):
        await self.db.execute(
            "INSERT INTO runs (run_date, distance_km, duration_min, source)"
            " VALUES (?, 5.2, 31, 'told')",
            (self.today.isoformat(),),
        )
        status = await self.h.jobs.water_pace_status(self.noon)
        self.assertEqual(status["pace"], 275)

    async def test_declared_heat_day_paces_up(self):
        await self.h.jobs.store.set("water_heat_day", self.today.isoformat())
        status = await self.h.jobs.water_pace_status(self.noon)
        self.assertEqual(status["pace"], 275)

    async def test_expected_counts_from_the_proven_wake(self):
        await self.db.execute(
            "INSERT INTO wake_log (day, wake_time, photo_ref, method) VALUES (?, ?, '', 'selfie')",
            (self.today.isoformat(), utc_now_iso()),
        )
        status = await self.h.jobs.water_pace_status(datetime.now(TZ))
        self.assertLess(status["hours_awake"], 0.2)   # just woke — nothing expected yet
        self.assertEqual(status["expected"], 0)

    async def test_before_gospel_is_his_night_minute_accurate(self):
        # The 5 Aug bug: gospel 08:30, water nudge at 08:04. Never again.
        await self.h.jobs.store.set(WAKE_TIME_KEY, "08:30")
        eight_oh_four = datetime.now(TZ).replace(hour=8, minute=4)
        self.assertTrue(await self.h.jobs.before_wake(eight_oh_four))
        await self.h.jobs.move_water_nudge(eight_oh_four)   # zero water, way behind
        self.assertEqual(self.h.texts(), [])
        # ...and run o'clock at 06:29 is equally his night
        self.assertTrue(await self.h.jobs.before_wake(datetime.now(TZ).replace(hour=6, minute=29)))

    async def test_wake_call_chasing_keeps_water_quiet(self):
        import json

        await self.h.jobs.store.set(WAKE_TIME_KEY, "08:00")
        await self.h.jobs.store.set("wake2_state", json.dumps({
            "date": self.today.isoformat(), "phase": "up", "test": False,
            "started": self.noon.isoformat(timespec="seconds"),
            "last_action": self.noon.isoformat(timespec="seconds"),
            "calls": 1, "silences": 0, "pitch_i": 0,
        }))
        await self.h.jobs.move_water_nudge(self.noon)
        self.assertEqual(self.h.texts(), [])   # the wake call owns the morning

    async def test_after_proof_the_day_is_open_again(self):
        await self.h.jobs.store.set(WAKE_TIME_KEY, "08:00")
        await self.db.execute(
            "INSERT INTO wake_log (day, wake_time, photo_ref, method) VALUES (?, ?, '', 'selfie')",
            (self.today.isoformat(), utc_now_iso()),
        )
        self.assertFalse(await self.h.jobs.before_wake(self.noon))

    async def test_after_goodnight_means_silence(self):
        await self.db.execute(
            "INSERT INTO sleep_log (day, goodnight_time, tz) VALUES (?, ?, 'Europe/London')",
            (self.today.isoformat(), utc_now_iso()),
        )
        await self.h.jobs.move_water_nudge(datetime.now(TZ).replace(hour=21, minute=5))
        self.assertEqual(self.h.texts(), [])
