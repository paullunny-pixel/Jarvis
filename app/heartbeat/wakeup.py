"""Wake & Hydrate v2 (Paul's brief, 5 Aug) — the call-led morning routine.

The shape of a morning: at the gospel wake time Jarvis RINGS Paul and talks
him upright — short scripted turns, no LLM lag — then calls back every couple
of minutes until proof-of-up lands (mirror selfie, desktop screenshot, or a
photo of the rotating code on the cockpit), then holds for the 500ml
electrolyte water photo, then opens the day with a warm word.

Three things are sacred here:
- Setting the wake time ('set wake 05:00' / 'wake me at 5') and 'test alarm'
  are INVIOLABLE — the router checks them before every other lane, no gate,
  quiet day or state can swallow them, and a failure answers honestly rather
  than silently.
- 'override' ends the sequence instantly, on the call or in Telegram —
  illness and emergencies outrank the drill, logged, no penalty.
- The welfare backstop: max_escalation_min of total silence means something
  might be wrong — stop calling, stand down, optionally tell a human.

Test mode ('test alarm') runs the WHOLE sequence right now with shortened
intervals and writes NO real data — wake log, streaks and water stay clean.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

WAKE_TIME_KEY = "wake2_time"     # "HH:MM" — the gospel, set from Telegram
STATE_KEY = "wake2_state"        # JSON — the live sequence's state machine
FIRED_KEY = "wake2_fired"        # ISO date — once-a-day guard on auto-fire
CODE_SECRET_KEY = "wake2_code_secret"

SET_WAKE = re.compile(
    r"\b(?:set\s+(?:the\s+)?wake(?:\s*-?\s*up)?(?:\s+time)?|wake\s+me)\s*(?:up\s*)?"
    r"(?:at|to|for)?\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)
TEST_ALARM = re.compile(r"^\s*(?:jarvis[,!.\s]+)?test\s+alarm\b", re.IGNORECASE)
YES = re.compile(
    r"\b(yes|yeah|yep|i'?m up|i am up|getting up|up now|on it|i'?m getting up)\b",
    re.IGNORECASE,
)
OVERRIDE = re.compile(r"\boverride\b", re.IGNORECASE)

# ---------------------------------------------------------------- the copy
# Short, punchy, his 'why' — Eva, Kiefer, the dream life, best shape of his
# life. Persona rules hold: never guilt-tripping, comedy drops when he's
# struggling. Pressure yes, sniping never.

OPEN_LINES = [
    "Morning Paul. It's {time} — your wake-up call. How you feeling?",
    "Paul. {time}, as ordered. How are you feeling?",
    "Morning sir. {time} on the dot. Talk to me — how you doing?",
]
PITCH_LINES = [
    "Skip this and you'll feel flat and behind all day. You know it.",
    "Get up now and you own the morning. That's the day you want.",
    "This is for Eva. This is your dream life. You with me?",
    "Best shape of your life doesn't start at nine. It starts now.",
    "Kiefer's counting on the man who gets up. So am I.",
    "One minute of brave gets you the whole day. Feet on the floor.",
]
ASK_LINE = "OK — are you getting up now?"
PROVE_LINES = [
    "That's it. Prove it — selfie, desktop, or the code on the cockpit. "
    "I'll keep calling till it lands.",
    "Good man. Now show me — mirror selfie, desktop screenshot, or snap the "
    "cockpit code. Calls keep coming till I see it.",
]
SIGNOFF_LINE = "I'll call back in a couple of minutes. Be up when I do."
HYDRATE_LINES = [
    "Good man. 500ml water with electrolytes, straight down. Send me a photo "
    "— glass, sachet or bottle, full or empty.",
    "Verified — you're up. Now water: 500ml with electrolytes. Photo when "
    "it's done, glass or sachet or bottle.",
]
HYDRATE_NUDGE = "Water's still owed, sir — 500ml with electrolytes. Photo to me and we're done."
WELLDONE_LINES = [
    "You should be proud of yourself, Paul. Morning's yours — go take the day.",
    "That's how a day starts, sir. Proud of you. Now go win it.",
    "Up, watered, before most people's alarms. You should be proud of "
    "yourself. The day is open.",
]
STALE_CODE_LINE = "That code's stale, sir — read me the one the cockpit shows right now."
OVERRIDE_ACK = (
    "Understood — override. Standing down completely, no penalty. "
    "If you're unwell, rest; I'm here when you surface."
)


def code_for(secret: str, refresh_min: int, offset: int = 0) -> str:
    """Derive the rotating cockpit code — single source, shared with the
    cockpit display so the two can never drift."""
    import time as _time

    window = int(_time.time() // (max(1, refresh_min) * 60)) + offset
    digest = _hmac.new(secret.encode(), f"wake2:{window}".encode(), hashlib.sha256).hexdigest()
    return digest[:6].upper()


class WakeRoutine:
    """The v2 state machine. Rides HeartbeatJobs for its plumbing (store, db,
    telegram, phone channel, gates) and is driven by a per-minute tick."""

    def __init__(self, jobs) -> None:
        self.jobs = jobs
        self.settings = jobs.settings
        self.store = jobs.store

    # ------------------------------------------------------------ state

    async def _state(self) -> dict:
        try:
            return json.loads(await self.store.get(STATE_KEY, "") or "{}")
        except Exception:
            return {}

    async def _save(self, state: dict) -> None:
        await self.store.set(STATE_KEY, json.dumps(state))

    async def _now(self) -> datetime:
        return datetime.now(await self.jobs._tz())

    async def wake_time(self) -> str:
        return await self.store.get(WAKE_TIME_KEY, "")

    async def active_phase(self) -> str:
        """'up' | 'hydrate' while a sequence is live today, else ''."""
        state = await self._state()
        now = await self._now()
        if state.get("date") == now.date().isoformat() and state.get("phase") in ("up", "hydrate"):
            return state["phase"]
        return ""

    # ------------------------------------------------ §0 + §0.5 the commands

    async def handle_command(self, transcript: str) -> str | None:
        """'set wake HH:MM' / 'wake me at 5' / 'test alarm'. Returns the reply
        when a command matched (executing it), None otherwise. A matched
        command NEVER errors out silently — failures answer honestly (§0)."""
        if TEST_ALARM.search(transcript):
            try:
                await self.start(test=True)
                return (
                    "🧪 TEST — this is a drill. Full wake sequence running now: "
                    "phone's about to ring, callbacks every "
                    f"{self.settings.test_callback_interval_sec}s until proof, then the "
                    "water step. Nothing counts — no wake, streak or water is recorded."
                )
            except Exception:
                logger.exception("Test alarm failed to start")
                return "The test alarm hit an error starting — logs have the why. Say it again and I'll retry."
        if len(transcript) > 60:
            return None
        hit = SET_WAKE.search(transcript)
        if hit:
            try:
                hour = int(hit.group(1))
                minute = int(hit.group(2) or 0)
                meridiem = (hit.group(3) or "").lower()
                if meridiem == "pm" and hour < 12:
                    hour += 12
                if meridiem == "am" and hour == 12:
                    hour = 0
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    return "That time doesn't parse, sir — try 'set wake 05:00'."
                gospel = f"{hour:02d}:{minute:02d}"
                await self.store.set(WAKE_TIME_KEY, gospel)
                await self.store.set(FIRED_KEY, "")  # re-arm even if today already fired
                return (
                    f"Locked — {gospel} wake. That's gospel now: the call comes at "
                    f"{gospel} every day, nothing blocks it, and only proof you're up "
                    "ends it. Change it any time the same way. 'override' always stops "
                    "a running sequence — that stays sacred."
                )
            except Exception:
                logger.exception("Set-wake failed")
                return "Setting the wake time hit an error — logs have it. Say it once more."
        return None

    # ------------------------------------------------------------ lifecycle

    async def start(self, test: bool = False) -> None:
        """Begin the sequence NOW (auto-fire or 'test alarm')."""
        now = await self._now()
        state = {
            "date": now.date().isoformat(),
            "phase": "up",
            "test": test,
            "started": now.isoformat(timespec="seconds"),
            "last_action": "",     # forces the first call on the next beat
            "calls": 0,
            "silences": 0,
            "pitch_i": 0,
        }
        await self._save(state)
        await self._code_secret()   # cockpit code displayable from second one
        prefix = "🧪 TEST — this is a drill. " if test else ""
        await self._tell(
            f"{prefix}Wake sequence live. Phone's ringing until you prove you're up: "
            "mirror selfie, desktop screenshot, or a photo of the cockpit code."
        )
        await self._place_call(state)

    async def stand_down(self, reason: str) -> None:
        state = await self._state()
        state["phase"] = "stood_down"
        state["reason"] = reason
        await self._save(state)
        phone = self.jobs.phone_channel
        if phone is not None:
            phone.script_handler = None
            phone.listen_timeout = None
        logger.info("Wake v2 stood down: %s", reason)

    async def _tell(self, text: str) -> None:
        """Direct Telegram send — wake traffic bypasses the quiet-day funnel
        and dedupe on purpose, exactly like the v1 wake channel."""
        chat_id = await self.jobs._owner_chat()
        if not chat_id:
            return
        await self.jobs.log.log("out", text, chat_id=chat_id, meta={"wake2": True})
        try:
            await self.jobs.telegram.send_text(chat_id, text)
        except Exception:
            logger.exception("Wake v2 Telegram send failed")

    # ------------------------------------------------------------ the call

    async def _pitch_lines(self) -> list[str]:
        """The built-in pitch plus the refreshable bank ('wake2_pitch_bank') —
        the brain expands the bank OFFLINE (after a completed morning), never
        during a call."""
        try:
            extra = json.loads(await self.store.get("wake2_pitch_bank", "") or "[]")
            extra = [str(x).strip() for x in extra if str(x).strip()][:12]
        except Exception:
            extra = []
        return list(PITCH_LINES) + extra

    async def _place_call(self, state: dict) -> None:
        now = await self._now()
        phone = self.jobs.phone_channel
        pitches = await self._pitch_lines()
        if state["calls"] == 0:
            line = OPEN_LINES[now.minute % len(OPEN_LINES)].format(time=now.strftime("%H:%M"))
        else:
            line = pitches[state["pitch_i"] % len(pitches)] + " " + ASK_LINE
            state["pitch_i"] += 1
        state["calls"] += 1
        state["silences"] = 0
        state["call_started"] = now.isoformat(timespec="seconds")
        state["last_action"] = now.isoformat(timespec="seconds")
        await self._save(state)
        if phone is not None and phone.configured:
            phone.script_handler = self.call_turn
            phone.listen_timeout = self.settings.wake_response_wait
            # Every standard line pre-synthesized: the call never waits on TTS.
            phone.prewarm(
                [OPEN_LINES[m % len(OPEN_LINES)].format(time=now.strftime("%H:%M")) for m in range(len(OPEN_LINES))]
                + pitches
                + [p + " " + ASK_LINE for p in pitches]
                + list(PROVE_LINES)
                + [SIGNOFF_LINE, HYDRATE_NUDGE, OVERRIDE_ACK]
            )
            if not await phone.call_paul(line):
                await self._tell("Tried to ring you and Twilio refused — " + line)
        else:
            # No phone line configured: the loop still chases on Telegram.
            await self._tell(line)

    async def call_turn(self, speech: str) -> str | None:
        """Scripted turns for the wake call — instant, no LLM. Returns None to
        hand a genuinely off-script utterance to the brain. A reply starting
        '[[bye]]' tells the phone channel to speak it and hang up."""
        state = await self._state()
        now = await self._now()
        if state.get("date") != now.date().isoformat() or state.get("phase") not in ("up", "hydrate"):
            phone = self.jobs.phone_channel
            if phone is not None:
                phone.script_handler = None   # sequence over — stop intercepting
                phone.listen_timeout = None
            return None
        speech = (speech or "").strip()
        if OVERRIDE.search(speech):
            await self.stand_down("override (spoken on the call)")
            await self._tell("Override taken on the call — sequence stood down, no penalty.")
            return "[[bye]]" + OVERRIDE_ACK
        if state["phase"] == "hydrate":
            return "[[bye]]" + HYDRATE_NUDGE
        # SILENCE NEVER ENDS THE CALL (5 Aug fix — Paul is ASLEEP; silence is
        # the expected state). Jarvis leads: next motivation line, ~5s listen
        # between lines, until the per-call cap — then a graceful sign-off and
        # the 2-minute loop re-calls. Only override or proof stops the morning.
        call_started = state.get("call_started") or state.get("last_action") or state["started"]
        on_air = (now - datetime.fromisoformat(call_started)).total_seconds()
        if on_air > self.settings.max_call_seconds:
            return "[[bye]]" + SIGNOFF_LINE
        if YES.search(speech):
            state["silences"] = 0
            await self._save(state)
            return "[[bye]]" + PROVE_LINES[state["calls"] % len(PROVE_LINES)]
        if speech and len(speech) > 80:
            return None   # genuinely off-script — the brain can have it
        pitches = await self._pitch_lines()
        line = pitches[state["pitch_i"] % len(pitches)]
        state["pitch_i"] += 1
        if not speech:
            state["silences"] += 1
            if state["silences"] % 3 == 0:
                line += " " + ASK_LINE   # an occasional direct ask amid the lead
        else:
            state["silences"] = 0
            line += " " + ASK_LINE       # he's talking but not committing — ask
        await self._save(state)
        return line

    # ------------------------------------------------------------ the tick

    async def tick(self, now: datetime | None = None) -> None:
        """Once a minute: fire at the gospel time; drive callbacks, the
        hydration grace, override and the welfare backstop."""
        now = now or await self._now()
        today = now.date()
        state = await self._state()
        live = state.get("date") == today.isoformat() and state.get("phase") in ("up", "hydrate")
        if not live:
            gospel = await self.wake_time()
            if not gospel or now.strftime("%H:%M") != gospel:
                return
            if await self.store.get(FIRED_KEY, "") == today.isoformat():
                return
            if await self.jobs.woke_today(today):
                return
            # Paul's own levers still count: an advance override or a declared
            # skip day is HIS instruction, and his word outranks the alarm.
            if self.jobs.gates is not None and await self.jobs.gates.is_overridden("wake", today):
                return
            if await self.store.get("wake_skip_date", "") == today.isoformat():
                return
            await self.store.set(FIRED_KEY, today.isoformat())
            await self.start(test=False)
            return

        # --- a sequence is live ---
        if self.jobs.gates is not None and await self.jobs.gates.is_overridden("wake", today):
            await self.stand_down("override")
            await self._tell("Override honoured — wake sequence stood down, no penalty.")
            return
        started = datetime.fromisoformat(state["started"])
        minutes_in = (now - started).total_seconds() / 60
        if minutes_in >= self.settings.max_escalation_min:
            await self.stand_down("welfare backstop — no response")
            await self._tell(
                "No response for a long while — I've stopped calling. No penalty; "
                "message me when you surface so I know you're alright."
            )
            contact = self.settings.welfare_contact
            if contact and self.jobs.emailer is not None:
                try:
                    await self.jobs.emailer.send(
                        contact,
                        "Jarvis welfare note",
                        f"Paul hasn't responded to his {state.get('date')} wake sequence "
                        f"for {self.settings.max_escalation_min} minutes. This is an "
                        "automated backstop notice — probably nothing, worth a look.",
                    )
                except Exception:
                    logger.exception("Welfare contact email failed")
            return
        last = state.get("last_action") or state["started"]
        elapsed = (now - datetime.fromisoformat(last)).total_seconds()
        if state.get("test"):
            due = self.settings.test_callback_interval_sec
        elif state["phase"] == "hydrate":
            due = self.settings.hydrate_grace_min * 60
        else:
            due = self.settings.wake_callback_interval_min * 60
        if elapsed < due:
            return
        if state["phase"] == "up":
            await self._place_call(state)
        else:
            state["last_action"] = now.isoformat(timespec="seconds")
            await self._save(state)
            await self._tell(HYDRATE_NUDGE)
            phone = self.jobs.phone_channel
            if phone is not None and phone.configured:
                phone.script_handler = self.call_turn
                await phone.call_paul(HYDRATE_NUDGE)

    # ------------------------------------------------------------ proofs

    async def proof_up(self, method: str, ref: str = "") -> str:
        """Selfie / desktop / valid code seen — §2 complete, hydration opens."""
        state = await self._state()
        now = await self._now()
        state["phase"] = "hydrate"
        state["last_action"] = now.isoformat(timespec="seconds")
        await self._save(state)
        if not state.get("test"):
            await self.jobs.record_wake(now.date(), method, photo_ref=ref)
        prefix = "🧪 TEST — nothing recorded. " if state.get("test") else ""
        return prefix + HYDRATE_LINES[now.minute % len(HYDRATE_LINES)]

    async def proof_hydration(self) -> str:
        """Water photo seen — the deactivation sequence completes, day opens."""
        state = await self._state()
        now = await self._now()
        state["phase"] = "done"
        await self._save(state)
        phone = self.jobs.phone_channel
        if phone is not None:
            phone.script_handler = None
            phone.listen_timeout = None
        if not state.get("test"):
            await self.jobs.log_water(now.date(), self.settings.hydration_ml)
        # Morning complete = offline: the brain refreshes tomorrow's pitch
        # bank now, never during a call (5 Aug fix).
        import asyncio as _asyncio

        task = _asyncio.create_task(self.refresh_bank())
        self._bank_tasks: set = getattr(self, "_bank_tasks", set())
        self._bank_tasks.add(task)
        task.add_done_callback(self._bank_tasks.discard)
        prefix = "🧪 TEST complete — drill over, nothing recorded. " if state.get("test") else ""
        return prefix + WELLDONE_LINES[now.minute % len(WELLDONE_LINES)]

    async def refresh_bank(self) -> None:
        """Offline only (after a completed morning): the brain writes fresh
        motivation lines for tomorrow's bank — same why (Eva, Kiefer, dream
        life, best shape, the two-futures contrast), new words."""
        try:
            raw = await self.jobs.claude.converse(
                "You write wake-up call motivation lines for Paul, in Jarvis's "
                "voice: short (max 14 words each), punchy, warm, direct — never "
                "guilt-tripping, never sniping. Draw on his why: Eva, Kiefer, the "
                "dream life, best shape of his life, the two-futures contrast "
                "(the day he wants vs the flat day). Reply ONLY a JSON array of "
                "6 strings.",
                [{"role": "user", "content": "Six fresh lines for tomorrow's wake call."}],
                max_tokens=300,
            )
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            lines = (
                [str(x).strip() for x in json.loads(match.group(0)) if str(x).strip()]
                if match else []
            )
            if lines:
                await self.store.set("wake2_pitch_bank", json.dumps(lines[:12]))
                logger.info("Wake pitch bank refreshed: %d lines", len(lines))
        except Exception:
            logger.exception("Pitch-bank refresh failed — the standing bank carries on")

    # ------------------------------------------------ §2 the rotating code

    async def _code_secret(self) -> str:
        secret = await self.store.get(CODE_SECRET_KEY, "")
        if not secret:
            import os as _os

            secret = _os.urandom(16).hex()
            await self.store.set(CODE_SECRET_KEY, secret)
        return secret

    async def wake_code(self, window_offset: int = 0) -> str:
        """Anti-cheat: the cockpit shows this code, rotating every
        wake_code_refresh_min — an old screenshot can never validate."""
        secret = await self._code_secret()
        return code_for(secret, self.settings.wake_code_refresh_min, window_offset)

    async def valid_code(self, seen: str) -> bool:
        seen = re.sub(r"[^A-F0-9]", "", (seen or "").upper())
        if not seen:
            return False
        return seen in (await self.wake_code(0), await self.wake_code(-1))
