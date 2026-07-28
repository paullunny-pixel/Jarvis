"""The heartbeat's jobs — Jarvis runs Paul's day even when Paul is silent.

06:30 protect-the-run ping · 07:00 morning brief (gentle first hour) ·
13:30 nudge (escalates to hound mode) · hound pings while hound is active ·
21:00 evening review + co-plan + private check-in · 21:00 Kiefer note.

Every message composes deterministically from real data first; Claude adds the
coaching voice on top and any Claude failure still leaves a useful message.
THE WALL: the Kiefer note and all business output draw only from business data.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.clients.anthropic_client import ClaudeClient
from app.clients.elevenlabs_client import ElevenLabsClient, SynthesisError
from app.clients.telegram_client import TelegramClient
from app.config import Settings
from app.core.reply_policy import strip_for_speech
from app.core.store import MessageLog, SettingsStore, utc_now_iso
from app.daily12.scoring import COMPANY_NAMES
from app.daily12.service import Daily12Service
from app.db.base import Database
from app.heartbeat.calendar_ics import IcsCalendar, travel_or_event_flags
from app.heartbeat.emailer import Emailer
from app.heartbeat.streaks import STREAK_LABELS, Streaks
from app.heartbeat.wake_channels import PhoneCallWakeChannel, TelegramWakeChannel

logger = logging.getLogger(__name__)

HOUND_KEY = "hound_date"
MIDDAY_TARGET_KEY = "midday_target"  # tasks expected done by 13:30 (default 4)

# --- Wake-up system (Master Update §4) — built but OFF until Paul says
# "start the wake-ups". Follows the current timezone; "no wake-up tomorrow"
# skips exactly one day; 'override' stops today's sequence.
WAKE_ENABLED_KEY = "wake_enabled"          # "on" | "" (default off)
WAKE_SKIP_KEY = "wake_skip_date"           # ISO date to sit out
WAKE_WINDOW = (5, 9)                       # escalate from 05:00 until 09:00 local

WAKE_LINES = [
    "It's {time}, sir — 05:00 has been and gone. Mirror selfie when you're vertical.",
    "{time}. The day is loitering by the door. Up you get — selfie to confirm.",
    "Still nothing at {time}, sir. Alarmy's done its bit; I need the mirror selfie.",
    "{time} and counting. Feet on floor, lights on, selfie over — then I'll stand down.",
    "Sir. {time}. Every minute now is borrowed from tonight's you. Selfie, please.",
    "I remain at my post — {time}. One photo in the mirror ends this politely.",
]

# --- Med & supplement schedule (Master Update §6), current timezone.
MED_SCHEDULE = {
    "adhd": {"label": "ADHD medication", "window": "09:30–10:00, after breakfast"},
    "supplements": {"label": "supplements", "window": "14:00–15:00, after food"},
    "trt": {"label": "TRT (weekly)", "window": "Saturday"},
}


class HeartbeatJobs:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        telegram: TelegramClient,
        claude: ClaudeClient,
        elevenlabs: ElevenLabsClient | None = None,
        daily12: Daily12Service | None = None,
        calendar: IcsCalendar | None = None,
        emailer: Emailer | None = None,
        kiefer_email: str = "",
        private_track=None,   # PrivateTrack — trigger-aware support (Milestone 6)
        gates=None,           # GateKeeper — the non-skippables
        mail=None,            # MailService — inbox counts in the brief (Phase 2)
        voice_engine=None,    # VoiceEngine — live calls (Build Slice: Voice Access)
    ) -> None:
        self.settings = settings
        self.db = db
        self.telegram = telegram
        self.claude = claude
        self.elevenlabs = elevenlabs
        self.daily12 = daily12
        self.calendar = calendar
        self.emailer = emailer
        self.kiefer_email = kiefer_email
        self.private_track = private_track
        self.gates = gates
        self.mail = mail
        self.store = SettingsStore(db)
        self.streaks = Streaks(db)
        self.log = MessageLog(db)
        self.wake_channel = TelegramWakeChannel(
            telegram, elevenlabs, self._owner_chat, self.log
        )
        # Phone-call escalation joins in when the number is wired up.
        self.phone_wake = None
        if (
            voice_engine is not None
            and settings.elevenlabs_phone_number_id
            and settings.paul_phone_number
        ):
            self.phone_wake = PhoneCallWakeChannel(
                voice_engine, settings.elevenlabs_phone_number_id, settings.paul_phone_number
            )

    # ------------------------------------------------------------ plumbing

    async def _owner_chat(self) -> int:
        if self.settings.telegram_owner_chat_id:
            return self.settings.telegram_owner_chat_id
        stored = await self.store.get("owner_chat_id")
        return int(stored) if stored else 0

    async def _tz(self) -> ZoneInfo:
        return ZoneInfo(await self.store.get("current_timezone", self.settings.timezone_default))

    async def _today(self) -> date:
        return datetime.now(await self._tz()).date()

    # --- Nudge idempotency (Master Update §14): a proactive message may ask
    # once, then it waits for a reply or a REAL change — never a repeat loop.

    async def _seen_within(self, key: str, hours: float) -> bool:
        row = await self.db.fetch_one(
            "SELECT last_sent_at FROM nudge_state WHERE nudge_key = ?", (key,)
        )
        if not row:
            return False
        last = datetime.fromisoformat(row["last_sent_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - last < timedelta(hours=hours)

    async def _stamp(self, key: str) -> None:
        if self.db.dialect == "postgres":
            await self.db.execute(
                "INSERT INTO nudge_state (nudge_key, last_sent_at) VALUES (?, ?)"
                " ON CONFLICT (nudge_key) DO UPDATE SET last_sent_at = EXCLUDED.last_sent_at",
                (key, utc_now_iso()),
            )
        else:
            await self.db.execute(
                "INSERT OR REPLACE INTO nudge_state (nudge_key, last_sent_at) VALUES (?, ?)",
                (key, utc_now_iso()),
            )

    async def _once(self, key: str, hours: float = 20.0) -> bool:
        """True exactly once per window — the per-job send guard."""
        if await self._seen_within(key, hours):
            logger.info("Nudge '%s' already sent this window — skipping", key)
            return False
        await self._stamp(key)
        return True

    async def _not_a_repeat(self, text: str) -> bool:
        """Global outbound dedupe: an identical proactive message can never
        fire twice within the window, whatever job produced it."""
        key = "msg:" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]
        if await self._seen_within(key, 6.0):
            logger.info("Identical heartbeat message suppressed (dedupe)")
            return False
        await self._stamp(key)
        return True

    async def _send_text(self, text: str) -> None:
        chat_id = await self._owner_chat()
        if not chat_id:
            logger.warning("No owner chat yet — heartbeat message skipped")
            return
        if not await self._not_a_repeat(text):
            return
        await self.log.log("out", text, chat_id=chat_id, meta={"heartbeat": True})
        await self.telegram.send_text(chat_id, text)

    async def _send_voice(self, text: str) -> None:
        chat_id = await self._owner_chat()
        if not chat_id:
            return
        if not await self._not_a_repeat(text):
            return
        await self.log.log("out", text, chat_id=chat_id, kind="voice", meta={"heartbeat": True})
        if self.elevenlabs is not None:
            try:
                audio = await self.elevenlabs.synthesize(strip_for_speech(text))
                await self.telegram.send_voice(chat_id, audio, transcript=text)
                return
            except SynthesisError:
                logger.exception("Heartbeat TTS failed — sending text")
        await self.telegram.send_text(chat_id, text)

    async def _flourish(self, instruction: str, data: str, fallback: str, max_tokens: int = 300) -> str:
        """One Claude line in Jarvis's voice; deterministic fallback if it fails."""
        try:
            out = await self.claude.quick(
                f"{instruction}\n\nData:\n{data}\n\nReply with the message only — no preamble.",
                system=(
                    "You are Jarvis, Paul's British AI chief-of-staff — composed and precise but "
                    "genuinely good company: warm, playful, a proper sense of humour, occasionally "
                    "'sir'. Quips and affectionate banter welcome; celebrate wins with real feeling. "
                    "Direct and candid but never passive-aggressive, never guilt-tripping; when he's "
                    "struggling, drop the comedy and be the steady friend. Concise — this is a "
                    "Telegram message."
                ),
                max_tokens=max_tokens,
            )
            return out.strip() or fallback
        except Exception:
            logger.exception("Flourish failed — using fallback")
            return fallback

    # ------------------------------------------------------------ 06:30 run

    async def run_protect(self) -> None:
        today = await self._today()
        if await self.streaks.done_today("run", today):
            return
        if not await self._once(f"runprotect:{today.isoformat()}"):
            return
        snapshot = await self.streaks.snapshot(today)
        streak = snapshot["run"]["current"]
        line = f"Run o'clock. 5k before the day gets its hands on you — streak's at {streak}."
        if streak == 0:
            line = "Run o'clock. Fresh streak starts with today's 5k — shoes on, one song, go."
        await self._send_voice(line)

    # ------------------------------------------------------------ 07:00 brief

    async def morning_brief(self) -> None:
        today = await self._today()
        if not await self._once(f"brief:{today.isoformat()}"):
            return
        tz = await self._tz()
        plan_text = ""
        if self.daily12 is not None:
            try:
                await self.daily12.generate(today)
                plan_text = await self.daily12.format_plan(today)
            except Exception:
                logger.exception("Daily 12 generation failed for the brief")
        events = await self.calendar.events_for(today, tz) if self.calendar else []
        snapshot = await self.streaks.snapshot(today)
        monthly = await self.streaks.monthly_activity(today)

        skeleton = compose_morning_skeleton(today, events, snapshot, monthly)
        if self.mail is not None:
            try:
                inbox_line = await self.mail.brief_line()
                if inbox_line:
                    skeleton += "\n" + inbox_line
            except Exception:
                logger.exception("Inbox counts failed for the brief — continuing without")
        if self.gates is not None:
            gate_lines = " · ".join(
                f"{g['label']} by {g['by']}" for g in await self.gates.config()
            )
            skeleton += (
                f"\n\nNON-NEGOTIABLES — {gate_lines}. Confirm each to me; "
                "the board stays shut until they're done."
            )
        opener = await self._flourish(
            "Write a 2–3 sentence opener for Paul's 7am brief. He's rough in his first hour — "
            "gentle, no heavy asks yet. Name the single most important thing today from the data, "
            "and protect the 5km run.",
            f"{skeleton}\n\n{plan_text[:800]}",
            "Morning. Coffee first, then the 5k — the rest of the day falls in behind it.",
        )
        message = opener + "\n\n" + skeleton
        if plan_text:
            message += "\n\n" + plan_text
        # Eat the frog (§8): the most-avoided item goes first, every day.
        if self.daily12 is not None:
            try:
                frog = await self.daily12.frog(today)
                if frog:
                    message += f"\n\n🐸 FROG FIRST: {frog} — before anything else. Ten minutes and it's dead."
            except Exception:
                logger.exception("Frog selection failed — brief goes out without it")
        await self._send_text(message)

        # Private trigger radar (Milestone 6): flights/trade shows get a warm,
        # separate heads-up — never merged into the business brief.
        if self.private_track is not None and events:
            flags = travel_or_event_flags(events)
            if flags:
                heads_up = await self.private_track.trigger_message(flags, today)
                if heads_up:
                    await self._send_voice(heads_up)

    # ------------------------------------------------------------ 13:30 nudge

    async def midday_nudge(self) -> None:
        today = await self._today()
        if not await self._once(f"midday:{today.isoformat()}"):
            return
        done, total = await self._focus_progress(today)
        run_done = await self.streaks.done_today("run", today) or (
            await self.streaks.recovery_today(today)  # declared rest day = handled
        )
        target = int(await self.store.get(MIDDAY_TARGET_KEY, "4") or 4)

        behind = total > 0 and done < min(target, total)
        if (not run_done or behind) and not await self.hound_active():
            await self.store.set(HOUND_KEY, today.isoformat())
            logger.info("Hound mode auto-triggered (run_done=%s, done=%d/%d)", run_done, done, total)

        status = (
            f"{done} of {total} done" if total else "the focus list is empty"
        ) + ("" if run_done else ", run still not in")
        if self.gates is not None:
            tz = await self._tz()
            for gate in await self.gates.outstanding(datetime.now(tz)):
                if gate["id"] != "run":  # run already reported above
                    status += f", {gate['label']} unconfirmed"
        if await self.hound_active():
            fallback = (
                f"Sir — {status}. May I suggest the top task on the focus list: two minutes to start it, "
                f"now, and tell me when it's moving." + ("" if run_done else " The run still happens today — that one isn't negotiable.")
            )
            nudge = await self._flourish(
                "Hound mode: Paul is stuck and needs the push. Calm, composed urgency — one single "
                "concrete next step, a two-minute start, total confidence in him. Firm through candour "
                "and grace, never scolding, never passive-aggressive. If the run is missed, make it the "
                "first move. 2–4 sentences.",
                status,
                fallback,
            )
        else:
            nudge = await self._flourish(
                "Midday check-in, on track. One sharp encouraging line plus the single next task. 1–2 sentences.",
                status,
                f"Halfway check: {status}. Keep the chain moving — next one, go.",
            )
        await self._send_voice(nudge)

    async def hound_ping(self) -> None:
        """Extra pressure while hound mode is on (called at the extra slots)."""
        if not await self.hound_active():
            return
        today = await self._today()
        done, total = await self._focus_progress(today)
        if total and done >= total:
            await self.store.set(HOUND_KEY, "")
            await self._send_voice("Focus list clear. Hound's back in the kennel — enormous day. ")
            return
        run_done = await self.streaks.done_today("run", today) or (
            await self.streaks.recovery_today(today)
        )
        status = f"{done}/{total}" + ("" if run_done else ", run missing")
        await self._send_voice(
            await self._flourish(
                "Hound mode ping. Short, composed, quietly insistent — the perfectly courteous aide "
                "who is not going anywhere. One next action. Max 2 sentences.",
                status,
                f"Still {status}, sir. One task, two minutes — I'll be right here when it's done.",
            )
        )

    async def hound_active(self) -> bool:
        return await self.store.get(HOUND_KEY) == (await self._today()).isoformat()

    async def set_hound(self, on: bool) -> None:
        await self.store.set(HOUND_KEY, (await self._today()).isoformat() if on else "")

    # ------------------------------------------------------------ 21:00 review

    async def evening_review(self) -> None:
        today = await self._today()
        if not await self._once(f"review:{today.isoformat()}"):
            return
        done, total = await self._focus_progress(today)
        snapshot = await self.streaks.snapshot(today)

        # §9 daily wins — what actually happened, framed positively.
        wins = await self._wins_recap(today, done, total, snapshot)
        summary = compose_evening_summary(today, done, total, snapshot)

        # §3 the evening ritual: walk This Week, choose tomorrow's cards.
        week_block = ""
        if self.daily12 is not None:
            try:
                is_sunday = datetime.now(await self._tz()).strftime("%a") == "Sun"
                week_block = await self.daily12.week_preview(sunday=is_sunday)
            except Exception:
                logger.exception("Week preview failed — review goes out without it")

        review = await self._flourish(
            "Evening review. Lead with the WINS (below) — celebrate what he actually did, "
            "specifically. Unfinished Paul Today cards roll to tomorrow automatically. Then ask "
            "him to pick tomorrow's cards from the This Week list and to co-plan the evening "
            "wind-down. 3–5 sentences.",
            f"{wins}\n\n{summary}",
            "Day's summary below. Pick tomorrow's cards from This Week — 'queue 2 and 5' does it — "
            "and voice me the shape of tomorrow.",
        )
        message = review + "\n\n" + wins + "\n\n" + summary
        if week_block:
            message += "\n\n" + week_block
        await self._send_text(message)
        # The private check-in rides separately, a few minutes of space apart in
        # tone: warm, brief, no business. (Milestone 6 deepens this flow.)
        await self._send_voice(
            "Separately — how are you in yourself tonight? One word will do. I've got you either way."
        )

    async def kiefer_note(self) -> bool:
        """The friendly 9pm 'here's what Paul's been up to' email. Business and
        training data ONLY — the private room does not exist to this function."""
        today = await self._today()
        if await self._seen_within(f"kiefer:{today.isoformat()}", 20.0):
            return False
        done, total = await self._focus_progress(today)
        snapshot = await self.streaks.snapshot(today)
        body_data = compose_kiefer_data(
            today, done, total, snapshot, await self.streaks.monthly_activity(today)
        )
        note = await self._flourish(
            "Write the nightly note to Kiefer (Paul's CFO and right-hand man) about Paul's day. "
            "Friendly and warm, never shaming, like a mate's update. Mention the Daily 12 progress "
            "and any streaks worth celebrating. Sign off as Jarvis. 4–7 sentences, plain text email.",
            body_data,
            f"Evening Kiefer — quick one from Jarvis. {body_data} More tomorrow. — Jarvis",
            max_tokens=500,
        )
        assert_no_private_content(note)
        if self.emailer is None or not self.kiefer_email:
            logger.warning("Kiefer email not configured — note skipped")
            return False
        sent = await self.emailer.send(self.kiefer_email, f"Paul's day — {today.strftime('%a %d %b')}", note)
        if sent:
            await self.store.set("last_kiefer_note", utc_now_iso())
            await self._stamp(f"kiefer:{today.isoformat()}")
        return sent

    # ------------------------------------------------ §4 wake-up (OFF by default)

    async def wake_enabled(self) -> bool:
        return await self.store.get(WAKE_ENABLED_KEY) == "on"

    async def set_wake_enabled(self, on: bool) -> None:
        await self.store.set(WAKE_ENABLED_KEY, "on" if on else "")

    async def skip_next_wake(self, tomorrow: date) -> None:
        await self.store.set(WAKE_SKIP_KEY, tomorrow.isoformat())

    async def arm_wake_for(self, day: date) -> None:
        """One-shot arm: 'goodnight, wake me as normal' arms just tomorrow —
        Paul's nightly ritual, no standing commitment needed."""
        await self.store.set("wake_armed_date", day.isoformat())

    async def woke_today(self, today: date) -> bool:
        row = await self.db.fetch_one(
            "SELECT id FROM wake_log WHERE day = ?", (today.isoformat(),)
        )
        return row is not None

    async def record_wake(self, today: date, method: str, photo_ref: str = "") -> None:
        if await self.woke_today(today):
            return
        await self.db.execute(
            "INSERT INTO wake_log (day, wake_time, photo_ref, method) VALUES (?, ?, ?, ?)",
            (today.isoformat(), utc_now_iso(), photo_ref, method),
        )

    async def wake_pending(self, now: datetime | None = None) -> bool:
        """The sequence is live right now and unconfirmed (override consults this)."""
        now = now or datetime.now(await self._tz())
        today = now.date()
        armed_tonight = await self.store.get("wake_armed_date") == today.isoformat()
        if not (await self.wake_enabled() or armed_tonight):
            return False
        if not (WAKE_WINDOW[0] <= now.hour < WAKE_WINDOW[1]):
            return False
        if await self.store.get(WAKE_SKIP_KEY) == today.isoformat():
            return False
        return not await self.woke_today(today)

    async def wake_tick(self, now: datetime | None = None) -> None:
        """Every ~3 minutes from 05:00 local until the mirror selfie lands."""
        now = now or datetime.now(await self._tz())
        if not await self.wake_pending(now):
            return
        today = now.date()
        if self.gates is not None and await self.gates.is_overridden("wake", today):
            return
        step = ((now.hour - WAKE_WINDOW[0]) * 60 + now.minute) // 3
        text = WAKE_LINES[step % len(WAKE_LINES)].format(time=now.strftime("%H:%M"))
        # Escalation: Telegram every tick; a real phone call joins in every
        # fifth step (~15 min) once the Twilio channel is configured.
        if self.phone_wake is not None and step > 0 and step % 5 == 0:
            await self.phone_wake.escalate(step, text)
        await self.wake_channel.escalate(step, text)

    # ------------------------------------------ §5 hourly movement + water

    async def water_total(self, today: date) -> int:
        row = await self.db.fetch_one(
            "SELECT ml FROM water_log WHERE day = ?", (today.isoformat(),)
        )
        return int(row["ml"]) if row else 0

    async def movement_total(self, today: date) -> int:
        row = await self.db.fetch_one(
            "SELECT count FROM movement_log WHERE day = ?", (today.isoformat(),)
        )
        return int(row["count"]) if row else 0

    async def log_water(self, today: date, ml: int) -> int:
        total = await self.water_total(today) + max(0, ml)
        if self.db.dialect == "postgres":
            await self.db.execute(
                "INSERT INTO water_log (day, ml) VALUES (?, ?)"
                " ON CONFLICT (day) DO UPDATE SET ml = EXCLUDED.ml",
                (today.isoformat(), total),
            )
        else:
            await self.db.execute(
                "INSERT OR REPLACE INTO water_log (day, ml) VALUES (?, ?)",
                (today.isoformat(), total),
            )
        return total

    async def log_movement(self, today: date) -> int:
        total = await self.movement_total(today) + 1
        if self.db.dialect == "postgres":
            await self.db.execute(
                "INSERT INTO movement_log (day, count) VALUES (?, ?)"
                " ON CONFLICT (day) DO UPDATE SET count = EXCLUDED.count",
                (today.isoformat(), total),
            )
        else:
            await self.db.execute(
                "INSERT OR REPLACE INTO movement_log (day, count) VALUES (?, ?)",
                (today.isoformat(), total),
            )
        return total

    async def move_water_nudge(self, now: datetime | None = None) -> None:
        """One combined move + 300ml nudge per waking hour."""
        if await self.store.get("hourly_nudges", "on") != "on":
            return
        now = now or datetime.now(await self._tz())
        today = now.date()
        if await self.wake_enabled() and not await self.woke_today(today) and now.hour < 12:
            return  # still asleep — the wake system owns the morning
        gone_to_bed = await self.db.fetch_one(
            "SELECT id FROM sleep_log WHERE day = ?", (today.isoformat(),)
        )
        if gone_to_bed and now.hour >= 12:
            return  # day already closed with "goodnight"
        if not await self._once(f"movewater:{today.isoformat()}:{now.hour}", hours=0.9):
            return
        water = await self.water_total(today)
        target = self.settings.water_target_ml
        moves = await self.movement_total(today)
        await self._send_text(
            f"{now.strftime('%H:%M')}, sir — one minute on your feet and 300ml down. "
            f"Water {water / 1000:.1f}L of {target / 1000:.1f}L · movements {moves}. "
            f"Say 'moved' and '300ml' and I'll log them."
        )

    # --------------------------------------------------- §6 meds & supplements

    async def med_taken(self, today: date, item: str) -> bool:
        row = await self.db.fetch_one(
            "SELECT id FROM med_adherence WHERE day = ? AND item = ?",
            (today.isoformat(), item),
        )
        return row is not None

    async def record_med(self, today: date, item: str) -> None:
        if item in MED_SCHEDULE and not await self.med_taken(today, item):
            await self.db.execute(
                "INSERT INTO med_adherence (day, item, taken_at) VALUES (?, ?, ?)",
                (today.isoformat(), item, utc_now_iso()),
            )

    async def med_reminder(self, item: str, now: datetime | None = None) -> None:
        now = now or datetime.now(await self._tz())
        today = now.date()
        if item == "trt" and now.strftime("%a") != "Sat":
            return
        if await self.med_taken(today, item):
            return
        if self.gates is not None and await self.gates.is_overridden(item, today):
            return
        if not await self._once(f"med:{item}:{today.isoformat()}"):
            return
        med = MED_SCHEDULE[item]
        await self._send_voice(
            f"Reminder, sir — {med['label']} ({med['window']}). "
            "Tell me when it's in and I'll log it."
        )

    async def _wins_recap(self, today: date, done: int, total: int, snapshot: dict) -> str:
        """§9: the day's actual wins, no shame anywhere."""
        lines = ["TODAY'S WINS"]
        done_rows = await self.db.fetch_all(
            "SELECT t.title FROM daily_12 d JOIN tasks t ON t.id = d.task_id"
            " WHERE d.plan_date = ? AND d.done = 1 AND d.position != 0",
            (today.isoformat(),),
        )
        for row in done_rows[:8]:
            lines.append(f"✅ {row['title']}")
        monthly = await self.streaks.monthly_activity(today)
        physical = []
        if snapshot["run"]["done_today"]:
            physical.append("run in")
        if snapshot["workout"]["done_today"]:
            physical.append("workout in")
        physical.append(f"{monthly['runs']} runs and {monthly['workouts']} workouts this month")
        lines.append("🏃 " + " · ".join(physical))
        water = await self.water_total(today)
        moves = await self.movement_total(today)
        if water or moves:
            lines.append(f"💧 {water / 1000:.1f}L water · {moves} movement breaks")
        if snapshot.get("portuguese", {}).get("done_today"):
            lines.append("🇧🇷 Portuguese practised — Steph will approve")
        if len(lines) == 1 and not done and not total:
            lines.append("A quiet board today — and that's allowed.")
        return "\n".join(lines)

    # -------------------------------------------- §10 bedtime nudge (~21:45)

    async def bedtime_nudge(self) -> None:
        """The 9-o'clock-ish check-in Paul asked for: wind down towards a
        05:00-friendly bedtime and arm tomorrow's wake with the goodnight."""
        today = await self._today()
        gone = await self.db.fetch_one(
            "SELECT id FROM sleep_log WHERE day = ?", (today.isoformat(),)
        )
        if gone:
            return
        if not await self._once(f"bedtime:{today.isoformat()}"):
            return
        armed = await self.wake_enabled()
        arm_line = (
            "Wake sequence already armed for 05:00."
            if armed
            else "Say 'goodnight — wake me as normal' and I'll run the 05:00 sequence."
        )
        await self._send_voice(
            f"Wind-down time, sir. Screens dimming, tomorrow's cards are set — lights out at "
            f"half ten, that's the deal we made. {arm_line}"
        )

    async def lights_out(self) -> None:
        """22:30 sharp, Paul's rule: lights out, no negotiation — protects the
        six hours before the 05:00 wake."""
        today = await self._today()
        gone = await self.db.fetch_one(
            "SELECT id FROM sleep_log WHERE day = ?", (today.isoformat(),)
        )
        if gone:
            return
        if not await self._once(f"lightsout:{today.isoformat()}"):
            return
        await self._send_voice(
            "Half past ten, sir — lights out, your rule, and a rather good one. Phone on the "
            "nightstand, 'goodnight' to me, and 05:00-you wakes up a hero."
        )

    async def lights_out_chaser(self) -> None:
        """23:00: still up? One firmer word — then Jarvis lets the man sleep."""
        today = await self._today()
        gone = await self.db.fetch_one(
            "SELECT id FROM sleep_log WHERE day = ?", (today.isoformat(),)
        )
        if gone:
            return
        if not await self._once(f"lightsout2:{today.isoformat()}"):
            return
        await self._send_voice(
            "Eleven o'clock, sir. Every minute now comes straight out of tomorrow's 05:00. "
            "Lights out — I'll take the goodnight as you fade."
        )

    # ------------------------------------ 3× daily work-group digest

    GROUP_DIGEST_SYSTEM = (
        "You are Jarvis, digesting Paul's work Telegram groups since his last update. "
        "Structure the digest with EXACTLY these sections, omitting any that are empty, "
        "plain text, concise, warm but businesslike:\n"
        "BY COMPANY — one tight paragraph per company/group on what's moved.\n"
        "⚡ ACTION NEEDED FROM YOU — decisions or replies only Paul can give.\n"
        "📋 TRELLO CANDIDATES — concrete tasks worth carding (he can say 'put a card…').\n"
        "🧱 STICKING POINTS — blocked or circling items, and who's stuck.\n"
        "⚠️ PAIN POINTS — friction, complaints, risks brewing.\n"
        "🏆 DONE & WINS — completed, shipped, good news worth a cheer.\n"
        "👀 WORTH KNOWING — anything else he'd want flagged.\n"
        "Never invent content; if a section has nothing, leave it out entirely."
    )

    async def group_digest(self, force: bool = False) -> bool:
        """Everything from the work groups since the last digest — scheduled
        three times a day, or on demand via 'group digest'. Watermarked by
        ingest row id so nothing is ever covered twice or missed."""
        last_id = int(await self.store.get("group_digest_last_id", "0") or 0)
        rows = await self.db.fetch_all(
            "SELECT id, ts, chat_title, company_tag, sender, message FROM telegram_ingest"
            " WHERE id > ? ORDER BY id ASC LIMIT 400",
            (last_id,),
        )
        if not rows:
            return False
        now = datetime.now(await self._tz())
        if not force and not await self._once(
            f"groupdigest:{now.date().isoformat()}:{now.hour}", hours=3.0
        ):
            return False
        corpus = "\n".join(
            f"[{r['chat_title']}] {r['sender']}: {r['message'][:300]}" for r in rows
        )[:24000]
        try:
            digest = await self.claude.converse(
                self.GROUP_DIGEST_SYSTEM, [{"role": "user", "content": corpus}]
            )
        except Exception:
            logger.exception("Group digest composition failed — deterministic fallback")
            digest = ""
        if not digest:
            tail = "\n".join(
                f"[{r['chat_title']}] {r['sender']}: {r['message'][:120]}" for r in rows[-10:]
            )
            digest = f"Summary engine hiccuped — the raw tail instead:\n{tail}"
        header = f"GROUP DIGEST — {len(rows)} message(s) since the last one\n\n"
        await self._send_text(header + digest)
        await self.store.set("group_digest_last_id", str(rows[-1]["id"]))
        return True

    # -------------------------------------------- hourly Trello re-sync

    async def trello_resync(self) -> None:
        """Keep the cache honest through the day: board moves, deletions and
        Trello-side ticks land within the hour, not at tomorrow's 07:00."""
        if self.daily12 is None:
            return
        try:
            await self.daily12.sync()
        except Exception:
            logger.exception("Hourly Trello resync failed — cache continues")

    # ------------------------------------------------------------ shared

    async def _focus_progress(self, today: date) -> tuple[int, int]:
        rows = await self.db.fetch_all(
            "SELECT done FROM daily_12 WHERE plan_date = ? AND position != 0",
            (today.isoformat(),),
        )
        done = sum(1 for r in rows if r["done"])
        if rows and done >= len(rows):
            await self.streaks.record("twelve", today)
        return done, len(rows)


# ---------------------------------------------------------------- composers

def compose_morning_skeleton(
    today: date, events: list[dict], snapshot: dict, monthly: dict | None = None
) -> str:
    lines = [f"YOUR DAY — {today.strftime('%A %d %B')}"]
    lines.append("06:30  5km run (the keystone — everything else follows it)")
    lines.append("07:00  Coffee, meals 1–2, ease in — no heavy lifting yet")
    if events:
        lines.append("")
        lines.append("CALENDAR")
        lines.extend(f"{e['time']:>5}  {e['title']}" for e in events)
    lines.append("")
    # Daily-doable streaks only; training reads as monthly counts (§11).
    lines.append("STREAKS  " + " · ".join(
        f"{STREAK_LABELS[t]} {snapshot[t]['current']}"
        for t in ("twelve", "meals", "portuguese")
        if t in snapshot
    ))
    if monthly is not None:
        lines.append(
            f"MONTH  {monthly['runs']} runs · {monthly['workouts']} workouts"
            f" · {monthly['recovery_days']} recovery days (rest counts)"
        )
    return "\n".join(lines)


def compose_evening_summary(today: date, done: int, total: int, snapshot: dict) -> str:
    lines = [f"TODAY — {today.strftime('%A %d %B')}"]
    lines.append(
        f"Today's Focus: {done}/{total} done" if total else "Today's Focus: clear day (nothing queued)"
    )
    for t in ("run", "workout"):
        state = "✅ done" if snapshot[t]["done_today"] else "▫️ not logged (rest is allowed)"
        lines.append(f"{STREAK_LABELS[t]}: {state}")
    for t in ("meals", "portuguese"):
        s = snapshot.get(t)
        if s is None:
            continue
        state = "✅ done" if s["done_today"] else "▫️ not logged"
        lines.append(f"{STREAK_LABELS[t]}: {state} (streak {s['current']}, best {s['best']})")
    return "\n".join(lines)


def compose_kiefer_data(
    today: date, done: int, total: int, snapshot: dict, monthly: dict | None = None
) -> str:
    parts = [
        f"Today's Focus: {done}/{total} cleared." if total else "A clear-list day."
    ]
    if snapshot["run"]["done_today"]:
        parts.append("Run in today.")
    if snapshot["workout"]["done_today"]:
        parts.append("Workout in today.")
    if monthly:
        parts.append(
            f"This month: {monthly['runs']} runs, {monthly['workouts']} workouts."
        )
    for t in ("twelve", "meals", "portuguese"):
        s = snapshot.get(t)
        if s and s["current"]:
            parts.append(f"{STREAK_LABELS[t]} streak: {s['current']} days (best {s['best']}).")
    if total and done >= total:
        parts.append("Full list cleared — big day.")
    return " ".join(parts)


PRIVATE_MARKERS = ("sobriety", "sober", "relapse", "sos", "trt", "testosterone", "adhd medication")


def assert_no_private_content(text: str) -> None:
    """Hard guard: outbound reports must never carry private-track content.
    Raises (and therefore blocks the send) if a marker slips in."""
    lowered = text.lower()
    for marker in PRIVATE_MARKERS:
        if marker in lowered:
            raise RuntimeError(f"Private content blocked from outbound report (marker: {marker})")
