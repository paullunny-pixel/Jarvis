"""The heartbeat's jobs — Jarvis runs Paul's day even when Paul is silent.

06:30 protect-the-run ping · 07:00 morning brief (gentle first hour) ·
13:30 nudge (escalates to hound mode) · hound pings while hound is active ·
21:00 evening review + co-plan + private check-in · 21:00 Kiefer note.

Every message composes deterministically from real data first; Claude adds the
coaching voice on top and any Claude failure still leaves a useful message.
THE WALL: the Kiefer note and all business output draw only from business data.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
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

logger = logging.getLogger(__name__)

HOUND_KEY = "hound_date"
MIDDAY_TARGET_KEY = "midday_target"  # tasks expected done by 13:30 (default 4)


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
        self.store = SettingsStore(db)
        self.streaks = Streaks(db)
        self.log = MessageLog(db)

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

    async def _send_text(self, text: str) -> None:
        chat_id = await self._owner_chat()
        if not chat_id:
            logger.warning("No owner chat yet — heartbeat message skipped")
            return
        await self.log.log("out", text, chat_id=chat_id, meta={"heartbeat": True})
        await self.telegram.send_text(chat_id, text)

    async def _send_voice(self, text: str) -> None:
        chat_id = await self._owner_chat()
        if not chat_id:
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
                    "You are Jarvis, Paul's British AI chief-of-staff in the mould of the great "
                    "fictional AI butlers: impeccably courteous, composed, precise, dry affectionate "
                    "wit, occasionally 'sir'. Direct and candid but never passive-aggressive, never "
                    "sarcastic, never guilt-tripping. Concise — this is a Telegram message."
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
        snapshot = await self.streaks.snapshot(today)
        streak = snapshot["run"]["current"]
        line = f"Run o'clock. 5k before the day gets its hands on you — streak's at {streak}."
        if streak == 0:
            line = "Run o'clock. Fresh streak starts with today's 5k — shoes on, one song, go."
        await self._send_voice(line)

    # ------------------------------------------------------------ 07:00 brief

    async def morning_brief(self) -> None:
        today = await self._today()
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

        skeleton = compose_morning_skeleton(today, events, snapshot)
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
        done, total = await self._twelve_progress(today)
        run_done = await self.streaks.done_today("run", today)
        target = int(await self.store.get(MIDDAY_TARGET_KEY, "4") or 4)

        behind = total > 0 and done < min(target, total)
        if (not run_done or behind) and not await self.hound_active():
            await self.store.set(HOUND_KEY, today.isoformat())
            logger.info("Hound mode auto-triggered (run_done=%s, done=%d/%d)", run_done, done, total)

        status = f"{done} of {total or 12} done" + ("" if run_done else ", run still not in")
        if self.gates is not None:
            tz = await self._tz()
            for gate in await self.gates.outstanding(datetime.now(tz)):
                if gate["id"] != "run":  # run already reported above
                    status += f", {gate['label']} unconfirmed"
        if await self.hound_active():
            fallback = (
                f"Sir — {status}. May I suggest the top task on the twelve: two minutes to start it, "
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
        done, total = await self._twelve_progress(today)
        if total and done >= total:
            await self.store.set(HOUND_KEY, "")
            await self._send_voice("All twelve down. Hound's back in the kennel — enormous day. ")
            return
        run_done = await self.streaks.done_today("run", today)
        status = f"{done}/{total or 12}" + ("" if run_done else ", run missing")
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
        done, total = await self._twelve_progress(today)
        snapshot = await self.streaks.snapshot(today)
        summary = compose_evening_summary(today, done, total, snapshot)
        review = await self._flourish(
            "Evening review. Reflect the day honestly (wins first), then ask Paul to co-plan "
            "tomorrow hour-by-hour with you — invite him to voice-note his shape of tomorrow. "
            "He does his best work in the evening, so this can have substance. 3–5 sentences.",
            summary,
            "Day's summary below. Voice me the shape of tomorrow — meetings, where you're training, "
            "what must move — and I'll build the hour-by-hour.",
        )
        await self._send_text(review + "\n\n" + summary)
        # The private check-in rides separately, a few minutes of space apart in
        # tone: warm, brief, no business. (Milestone 6 deepens this flow.)
        await self._send_voice(
            "Separately — how are you in yourself tonight? One word will do. I've got you either way."
        )

    async def kiefer_note(self) -> bool:
        """The friendly 9pm 'here's what Paul's been up to' email. Business and
        training data ONLY — the private room does not exist to this function."""
        today = await self._today()
        done, total = await self._twelve_progress(today)
        snapshot = await self.streaks.snapshot(today)
        body_data = compose_kiefer_data(today, done, total, snapshot)
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
        return sent

    # ------------------------------------------------------------ shared

    async def _twelve_progress(self, today: date) -> tuple[int, int]:
        rows = await self.db.fetch_all(
            "SELECT done FROM daily_12 WHERE plan_date = ? AND position != 0",
            (today.isoformat(),),
        )
        done = sum(1 for r in rows if r["done"])
        if rows and done >= len(rows):
            await self.streaks.record("twelve", today)
        return done, len(rows)


# ---------------------------------------------------------------- composers

def compose_morning_skeleton(today: date, events: list[dict], snapshot: dict) -> str:
    lines = [f"YOUR DAY — {today.strftime('%A %d %B')}"]
    lines.append("06:30  5km run (the keystone — everything else follows it)")
    lines.append("07:00  Coffee, meals 1–2, ease in — no heavy lifting yet")
    if events:
        lines.append("")
        lines.append("CALENDAR")
        lines.extend(f"{e['time']:>5}  {e['title']}" for e in events)
    lines.append("")
    lines.append("STREAKS  " + " · ".join(
        f"{STREAK_LABELS[t]} {snapshot[t]['current']}" for t in ("run", "workout", "twelve", "meals")
    ))
    return "\n".join(lines)


def compose_evening_summary(today: date, done: int, total: int, snapshot: dict) -> str:
    lines = [f"TODAY — {today.strftime('%A %d %B')}"]
    lines.append(f"The 12: {done}/{total or 12} done")
    for t in ("run", "workout", "meals"):
        s = snapshot[t]
        state = "✅ done" if s["done_today"] else "▫️ not logged"
        lines.append(f"{STREAK_LABELS[t]}: {state} (streak {s['current']}, best {s['best']})")
    return "\n".join(lines)


def compose_kiefer_data(today: date, done: int, total: int, snapshot: dict) -> str:
    parts = [f"Daily 12: {done}/{total or 12} cleared."]
    for t in ("run", "workout", "twelve", "meals"):
        s = snapshot[t]
        if s["current"]:
            parts.append(f"{STREAK_LABELS[t]} streak: {s['current']} days (best {s['best']}).")
    if done >= (total or 12) and total:
        parts.append("Full board cleared — big day.")
    return " ".join(parts)


PRIVATE_MARKERS = ("sobriety", "sober", "relapse", "sos", "trt", "testosterone", "adhd medication")


def assert_no_private_content(text: str) -> None:
    """Hard guard: outbound reports must never carry private-track content.
    Raises (and therefore blocks the send) if a marker slips in."""
    lowered = text.lower()
    for marker in PRIVATE_MARKERS:
        if marker in lowered:
            raise RuntimeError(f"Private content blocked from outbound report (marker: {marker})")
