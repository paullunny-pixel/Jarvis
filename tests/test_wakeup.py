"""Wake & Hydrate v2 (5 Aug): the gospel wake time, 'test alarm', the scripted
call, callbacks, proofs, override and the welfare backstop."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx

from app.clients.anthropic_client import ClaudeClient
from app.clients.deepgram_client import DeepgramClient
from app.clients.elevenlabs_client import ElevenLabsClient
from app.clients.telegram_client import TelegramClient
from app.config import Settings
from app.core.router import JarvisRouter
from app.db.sqlite import SqliteDatabase
from app.heartbeat.jobs import HeartbeatJobs
from app.heartbeat.wakeup import FIRED_KEY, STATE_KEY, WAKE_TIME_KEY
from tests.test_telegram_client import text_update

OWNER = 111
TZ = ZoneInfo("Europe/London")


class FakePhone:
    configured = True

    def __init__(self):
        self.calls = []
        self.script_handler = None
        self.listen_timeout = None

    async def call_paul(self, greeting=None):
        self.calls.append(greeting)
        return True

    def prewarm(self, lines):
        self.prewarmed = list(lines)


class Harness:
    def __init__(self, db, phone=None):
        self.telegram_calls = []

        def telegram_handler(request: httpx.Request) -> httpx.Response:
            self.telegram_calls.append((request.url.path.split("/")[-1], request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})

        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.jobs = HeartbeatJobs(
            settings=settings,
            db=db,
            telegram=TelegramClient("TOK", transport=httpx.MockTransport(telegram_handler)),
            claude=ClaudeClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            phone_channel=phone,
        )
        self.routine = self.jobs.wake2

    def texts(self):
        return [
            parse_qs(body.decode())["text"][0]
            for method, body in self.telegram_calls
            if method == "sendMessage"
        ]


class WakeBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.phone = FakePhone()
        self.h = Harness(self.db, phone=self.phone)

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def _state(self):
        return json.loads(await self.h.jobs.store.get(STATE_KEY, "") or "{}")

    async def _put_state(self, **overrides):
        now = datetime.now(TZ)
        state = {
            "date": now.date().isoformat(), "phase": "up", "test": False,
            "started": now.isoformat(timespec="seconds"),
            "last_action": now.isoformat(timespec="seconds"),
            "calls": 1, "silences": 0, "pitch_i": 0,
        }
        state.update(overrides)
        await self.h.jobs.store.set(STATE_KEY, json.dumps(state))
        return state


class TestGospelCommands(WakeBase):
    async def test_set_wake_variants_lock_the_time(self):
        for phrase, expect in [
            ("set wake 05:00", "05:00"),
            ("wake me at 5", "05:00"),
            ("Set the wake up time to 6.30", "06:30"),
            ("wake me at 5pm", "17:00"),
        ]:
            reply = await self.h.routine.handle_command(phrase)
            self.assertIn("Locked", reply, phrase)
            self.assertIn(expect, reply, phrase)
            self.assertEqual(await self.h.jobs.store.get(WAKE_TIME_KEY), expect)

    async def test_non_commands_pass_through(self):
        self.assertIsNone(await self.h.routine.handle_command("what a morning that was"))
        self.assertIsNone(await self.h.routine.handle_command("wake me gently one day"))

    async def test_test_alarm_starts_a_drill(self):
        reply = await self.h.routine.handle_command("test alarm")
        self.assertIn("🧪", reply)
        state = await self._state()
        self.assertEqual(state["phase"], "up")
        self.assertTrue(state["test"])
        self.assertEqual(len(self.phone.calls), 1)   # the call rings immediately
        self.assertTrue(any("drill" in t or "🧪" in t for t in self.h.texts()))


class TestAutoFireAndCallbacks(WakeBase):
    async def test_fires_at_the_gospel_minute_once(self):
        await self.h.jobs.store.set(WAKE_TIME_KEY, "05:00")
        now = datetime.now(TZ).replace(hour=5, minute=0)
        await self.h.routine.tick(now)
        self.assertEqual((await self._state())["phase"], "up")
        self.assertEqual(len(self.phone.calls), 1)
        self.assertEqual(await self.h.jobs.store.get(FIRED_KEY), now.date().isoformat())

    async def test_quiet_minute_is_a_no_op(self):
        await self.h.jobs.store.set(WAKE_TIME_KEY, "05:00")
        await self.h.routine.tick(datetime.now(TZ).replace(hour=9, minute=15))
        self.assertEqual(await self._state(), {})
        self.assertEqual(self.phone.calls, [])

    async def test_callback_fires_after_the_interval(self):
        now = datetime.now(TZ)
        await self._put_state(last_action=(now - timedelta(minutes=3)).isoformat(timespec="seconds"))
        await self.h.routine.tick(now)
        self.assertEqual(len(self.phone.calls), 1)   # callback placed
        self.assertLess((now - datetime.fromisoformat((await self._state())["last_action"])).total_seconds(), 60)

    async def test_callback_holds_inside_the_interval(self):
        now = datetime.now(TZ)
        await self._put_state(last_action=(now - timedelta(seconds=45)).isoformat(timespec="seconds"))
        await self.h.routine.tick(now)
        self.assertEqual(self.phone.calls, [])

    async def test_test_mode_uses_the_short_interval(self):
        now = datetime.now(TZ)
        await self._put_state(test=True, last_action=(now - timedelta(seconds=31)).isoformat(timespec="seconds"))
        await self.h.routine.tick(now)
        self.assertEqual(len(self.phone.calls), 1)

    async def test_override_stands_the_sequence_down(self):
        await self._put_state()
        today = datetime.now(TZ).date()
        # override via the gates lever, exactly as the universal override does
        from app.heartbeat.gates import GateKeeper
        from app.heartbeat.streaks import Streaks

        self.h.jobs.gates = GateKeeper(self.db, Streaks(self.db))
        await self.h.jobs.gates.override(["wake"], "feeling rough", today)
        await self.h.routine.tick(datetime.now(TZ))
        self.assertEqual((await self._state())["phase"], "stood_down")
        self.assertTrue(any("stood down" in t for t in self.h.texts()))

    async def test_welfare_backstop_stops_the_calls(self):
        now = datetime.now(TZ)
        await self._put_state(
            started=(now - timedelta(minutes=91)).isoformat(timespec="seconds"),
            last_action=(now - timedelta(minutes=5)).isoformat(timespec="seconds"),
        )
        await self.h.routine.tick(now)
        self.assertEqual((await self._state())["phase"], "stood_down")
        self.assertEqual(self.phone.calls, [])
        self.assertTrue(any("stopped calling" in t for t in self.h.texts()))


class TestTheScriptedCall(WakeBase):
    async def test_yes_gets_the_prove_line_and_hangup(self):
        await self._put_state()
        reply = await self.h.routine.call_turn("yeah I'm getting up")
        self.assertTrue(reply.startswith("[[bye]]"))
        self.assertIn("selfie", reply)   # both prove variants demand the proof

    async def test_excuses_get_the_pitch_not_the_brain(self):
        await self._put_state()
        reply = await self.h.routine.call_turn("five more minutes")
        self.assertIsNotNone(reply)
        self.assertIn("are you getting up now?", reply)

    async def test_silence_never_ends_the_call_jarvis_leads(self):
        # Paul is ASLEEP — silence is expected. Within the per-call cap every
        # silent turn gets the next motivation line, never a hangup.
        now = datetime.now(TZ)
        await self._put_state(call_started=now.isoformat(timespec="seconds"))
        for _ in range(8):
            reply = await self.h.routine.call_turn("")
            self.assertIsNotNone(reply)
            self.assertFalse(reply.startswith("[[bye]]"), reply)

    async def test_call_cap_ends_gracefully_loop_recalls(self):
        now = datetime.now(TZ)
        await self._put_state(
            call_started=(now - timedelta(seconds=80)).isoformat(timespec="seconds")
        )
        reply = await self.h.routine.call_turn("")
        self.assertTrue(reply.startswith("[[bye]]"))
        self.assertIn("call back", reply)
        # the sequence itself is still live — only proof or override ends it
        self.assertEqual((await self._state())["phase"], "up")

    async def test_pitch_bank_extends_the_rotation(self):
        await self.h.jobs.store.set(
            "wake2_pitch_bank", json.dumps(["Fresh line from the overnight brain."])
        )
        lines = await self.h.routine._pitch_lines()
        self.assertIn("Fresh line from the overnight brain.", lines)

    async def test_placing_a_call_prewarms_the_bank(self):
        await self.h.routine.start(test=True)
        self.assertTrue(any("Eva" in line for line in self.phone.prewarmed))
        self.assertTrue(any("call back" in line for line in self.phone.prewarmed))

    async def test_misspelled_override_still_counts(self):
        # 2:19am, 5 Aug: Paul typed 'overide' and the chase carried on. Never again.
        for word in ("overide", "over ride", "overrride"):
            await self._put_state()
            reply = await self.h.routine.call_turn(word)
            self.assertTrue(reply.startswith("[[bye]]"), word)
            self.assertEqual((await self._state())["phase"], "stood_down", word)

    async def test_a_drill_times_itself_out(self):
        now = datetime.now(TZ)
        await self._put_state(
            test=True,
            started=(now - timedelta(minutes=11)).isoformat(timespec="seconds"),
            last_action=(now - timedelta(minutes=1)).isoformat(timespec="seconds"),
        )
        await self.h.routine.tick(now)
        self.assertEqual((await self._state())["phase"], "stood_down")
        self.assertEqual(self.phone.calls, [])
        self.assertTrue(any("Drill timed out" in t for t in self.h.texts()))

    async def test_spoken_override_ends_everything(self):
        await self._put_state()
        reply = await self.h.routine.call_turn("override, I'm ill")
        self.assertTrue(reply.startswith("[[bye]]"))
        self.assertIn("no penalty", reply)
        self.assertEqual((await self._state())["phase"], "stood_down")

    async def test_long_off_script_speech_goes_to_the_brain(self):
        await self._put_state()
        reply = await self.h.routine.call_turn(
            "actually while I have you can we talk about the Prodermis invoice "
            "situation from yesterday because Kiefer flagged something odd"
        )
        self.assertIsNone(reply)

    async def test_no_active_sequence_means_not_our_call(self):
        self.assertIsNone(await self.h.routine.call_turn("morning"))


class TestProofsAndData(WakeBase):
    async def test_real_proof_records_wake_then_water(self):
        await self._put_state()
        ack = await self.h.routine.proof_up("selfie", "PHOTO1")
        self.assertIn("500ml", ack)
        self.assertEqual((await self._state())["phase"], "hydrate")
        row = await self.db.fetch_one("SELECT * FROM wake_log")
        self.assertEqual(row["method"], "selfie")
        done = await self.h.routine.proof_hydration()
        self.assertTrue(any(w in done for w in ("proud", "day")))
        water = await self.db.fetch_one("SELECT ml FROM water_log")
        self.assertEqual(int(water["ml"]), 500)

    async def test_test_mode_pollutes_nothing(self):
        await self._put_state(test=True)
        ack = await self.h.routine.proof_up("selfie", "PHOTO1")
        self.assertIn("🧪", ack)
        done = await self.h.routine.proof_hydration()
        self.assertIn("🧪", done)
        self.assertIsNone(await self.db.fetch_one("SELECT * FROM wake_log"))
        self.assertIsNone(await self.db.fetch_one("SELECT * FROM water_log"))

    async def test_telling_him_the_water_is_done_counts(self):
        await self._put_state(phase="hydrate")
        reply = await self.h.routine.handle_command("had my water and electrolytes")
        self.assertIsNotNone(reply)
        self.assertEqual((await self._state())["phase"], "done")
        water = await self.db.fetch_one("SELECT ml FROM water_log")
        self.assertEqual(int(water["ml"]), 500)

    async def test_negated_water_claims_never_clear_it(self):
        await self._put_state(phase="hydrate")
        self.assertIsNone(await self.h.routine.handle_command("I have NOT had my water yet"))
        self.assertEqual((await self._state())["phase"], "hydrate")

    async def test_water_words_outside_hydrate_pass_through(self):
        self.assertIsNone(await self.h.routine.handle_command("had my water earlier"))

    async def test_rotating_code_validates_and_expires(self):
        current = await self.h.routine.wake_code()
        previous = await self.h.routine.wake_code(-1)
        self.assertTrue(await self.h.routine.valid_code(current))
        self.assertTrue(await self.h.routine.valid_code(previous))   # clock-edge grace
        self.assertFalse(await self.h.routine.valid_code("NOPE99"))
        self.assertFalse(await self.h.routine.valid_code(""))
        self.assertNotEqual(current, await self.h.routine.wake_code(-2))


class TestRouterLaneAndV1Handover(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(os.path.join(self._dir.name, "t.db"))
        await self.db.init()
        self.h = Harness(self.db, phone=FakePhone())
        settings = Settings(telegram_bot_token="TOK", telegram_owner_chat_id=OWNER, _env_file=None)
        self.router = JarvisRouter(
            settings=settings,
            db=self.db,
            telegram=self.h.jobs.telegram,
            claude=self.h.jobs.claude,
            deepgram=DeepgramClient("K", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            elevenlabs=ElevenLabsClient("K", voice_id="V", transport=httpx.MockTransport(lambda r: httpx.Response(500))),
            heartbeat=self.h.jobs,
        )

    async def asyncTearDown(self):
        await self.db.close()
        self._dir.cleanup()

    async def test_set_wake_rides_the_inviolable_lane(self):
        await self.router.handle_update(text_update("set wake 05:00", OWNER))
        self.assertTrue(any("Locked" in t for t in self.h.texts()))
        self.assertEqual(await self.h.jobs.store.get(WAKE_TIME_KEY), "05:00")

    async def test_test_alarm_rides_the_lane_and_fires(self):
        await self.router.handle_update(text_update("test alarm", OWNER))
        self.assertTrue(any("🧪" in t for t in self.h.texts()))
        state = json.loads(await self.h.jobs.store.get(STATE_KEY, "") or "{}")
        self.assertEqual(state.get("phase"), "up")

    async def test_typed_overide_stops_a_live_sequence_instantly(self):
        # The universal lane knows v2 now, tolerates the spelling, and obeys
        # with no confirm dance.
        await self.h.jobs.store.set(STATE_KEY, json.dumps({
            "date": datetime.now(TZ).date().isoformat(), "phase": "hydrate",
            "test": True, "started": datetime.now(TZ).isoformat(timespec="seconds"),
            "last_action": datetime.now(TZ).isoformat(timespec="seconds"),
            "calls": 3, "silences": 0, "pitch_i": 0,
        }))
        await self.router.handle_update(text_update("overide", OWNER))
        state = json.loads(await self.h.jobs.store.get(STATE_KEY, "") or "{}")
        self.assertEqual(state.get("phase"), "stood_down")
        self.assertTrue(any("stood down instantly" in t for t in self.h.texts()))

    async def test_status_shows_the_gospel_wake_time(self):
        await self.router.handle_update(text_update("set wake 08:30", OWNER))
        await self.router.handle_update(text_update("status", OWNER))
        report = next(t for t in self.h.texts() if "SYSTEM CHECK" in t)
        self.assertIn("Wake-up call: 08:30", report)
        self.assertIn("gospel", report)

    async def test_status_honest_when_no_wake_set(self):
        await self.router.handle_update(text_update("status", OWNER))
        report = next(t for t in self.h.texts() if "SYSTEM CHECK" in t)
        self.assertIn("Wake-up: not set", report)

    async def test_v1_wake_tick_stands_down_when_gospel_set(self):
        await self.h.jobs.set_wake_enabled(True)
        await self.h.jobs.store.set(WAKE_TIME_KEY, "05:00")
        await self.h.jobs.wake_tick(datetime(2026, 8, 5, 5, 6, tzinfo=TZ))
        self.assertEqual(self.h.texts(), [])   # v2 owns the morning — v1 silent
