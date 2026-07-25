"""The private sobriety track (Milestone 6).

Walled-off and warm. Everything here: is stored encrypted (PrivateBox), never
joins business queries, never appears in the Kiefer note or any outbound
report, and speaks in the supportive-coach voice — never the drill sergeant.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone as _tz
from typing import Any

from app.clients.anthropic_client import ClaudeClient
from app.core.store import utc_now_iso
from app.db.base import Database
from app.memory.crypto import PrivateBox
from app.memory.store import LivingFacts, MemoryStore

logger = logging.getLogger(__name__)

SOS = re.compile(r"^\s*sos\b|\bjarvis[, ]+sos\b", re.IGNORECASE)

PRIVATE_TOPIC = re.compile(
    r"\bsober|sobriety\b|\bcraving|\burge to drink\b|\btempted\b|\brelaps|\bslipped( up)?\b"
    r"|\bwant (a|to) drink\b|\bdrinking\b|\bstay(ing)? dry\b|\bday \d+ (sober|dry)\b",
    re.IGNORECASE,
)

MILESTONES = {7, 14, 30, 50, 75, 100, 150, 200, 365}

SUPPORT_SYSTEM = """\
You are Jarvis in the private room with Paul — his sobriety track. Drop the \
drill sergeant entirely: here you are a warm, steady, supportive coach. Never \
shame, never lecture, never a scoreboard. "I've got you" is the tone.

What you know: Paul's known triggers are flying/travel days, work events \
(especially trade shows), loneliness (a big one), feeling unfulfilled, and \
overwhelm around the kids. When he's isolated, gently nudge him to call John, \
Kiefer or Steph. Celebrate milestones warmly. If he reports a slip, zero \
judgement — the next right step matters, not the fall; quietly restart the \
count with him and find what triggered it, together.

If he is in real distress or at risk, gently encourage professional support \
(his GP, or a helpline) alongside your own presence.

Keep replies short, spoken-word natural (they're delivered as voice notes), \
and never mention business, tasks or the Daily 12 here unless he does.\
"""

SOS_OPENER = (
    "I'm right here, Paul. Wherever you are, stop for a second and breathe with me — "
    "in for four, hold for four, out for four. You don't have to fight the next hour, "
    "just the next ten minutes, and I'll stay with you the whole way. Talk to me — "
    "what's going on around you right now?"
)

ANALYSIS_SYSTEM = """\
Extract check-in signals from Paul's private message. Reply ONLY JSON:
{"mood": <1-5 or 0 if unclear>, "lapse": <true ONLY if he clearly reports drinking/slipping>,
 "day_claim": <number if he states his day count, else null>,
 "lonely": <true if loneliness/isolation is expressed>}\
"""


class PrivateTrack:
    def __init__(
        self,
        db: Database,
        claude: ClaudeClient,
        box: PrivateBox,
        memory: MemoryStore | None = None,
        living: LivingFacts | None = None,
    ) -> None:
        self._db = db
        self._claude = claude
        self._box = box
        self._memory = memory
        self._living = living

    # ------------------------------------------------------------ detection

    @staticmethod
    def is_sos(text: str) -> bool:
        return bool(SOS.search(text))

    @staticmethod
    def is_private_topic(text: str) -> bool:
        return bool(PRIVATE_TOPIC.search(text))

    # ------------------------------------------------------------ day count

    async def day_count(self, today: date) -> int | None:
        if self._living is None:
            return None
        start = await self._living.get("sobriety.start_date")
        if not start:
            return None
        try:
            return max(0, (today - date.fromisoformat(start[:10])).days)
        except ValueError:
            return None

    async def set_start_date(self, start: date) -> None:
        if self._living is not None:
            await self._living.set("sobriety.start_date", start.isoformat(), room="private")
            await self._living.set(
                "sobriety.days", str((datetime.now(_tz.utc).date() - start).days), room="private"
            )

    async def _refresh_days_fact(self, today: date) -> None:
        count = await self.day_count(today)
        if count is not None and self._living is not None:
            await self._living.set("sobriety.days", str(count), room="private")

    # ------------------------------------------------------------ the flows

    async def respond(self, transcript: str, today: date) -> str:
        """One private-room exchange: analyse, record, reply supportively."""
        if self.is_sos(transcript):
            await self._record("sos", transcript, 0, await self.day_count(today) or 0)
            return SOS_OPENER

        signals = await self._analyse(transcript)
        day = await self.day_count(today)

        if signals.get("day_claim") and not day:
            claimed = int(signals["day_claim"])
            from datetime import timedelta

            await self.set_start_date(today - timedelta(days=claimed))
            day = claimed

        if signals.get("lapse"):
            await self.set_start_date(today)
            await self._record("reset", transcript, signals.get("mood", 0), 0)
            reply = await self._speak(
                transcript,
                extra="Paul has told you about a slip. Zero shame — steady him, remind him one hard "
                "night doesn't erase the run of good ones, restart the count together, and gently "
                "explore what set it off.",
            )
            return reply

        day = await self.day_count(today)
        await self._refresh_days_fact(today)
        kind = "milestone" if day in MILESTONES else "checkin"
        await self._record(kind, transcript, signals.get("mood", 0), day or 0)

        extra = ""
        if kind == "milestone":
            extra = f"Today is day {day} — a real milestone. Celebrate it warmly (no scoreboard talk)."
        elif signals.get("lonely"):
            extra = (
                "Loneliness is showing — his biggest trigger. Gently suggest calling John, Kiefer "
                "or Steph tonight, and stay present."
            )
        elif day is not None:
            extra = f"He's on day {day}. Acknowledge naturally if it fits, don't make it a scoreboard."
        return await self._speak(transcript, extra=extra)

    async def _speak(self, transcript: str, extra: str = "") -> str:
        system = SUPPORT_SYSTEM + (f"\n\nContext for this reply: {extra}" if extra else "")
        context = await self._private_context(transcript)
        if context:
            system += "\n\nPrivate memory (yours and his alone):\n" + context
        try:
            return await self._claude.converse(
                system, [{"role": "user", "content": transcript}], max_tokens=400
            )
        except Exception:
            logger.exception("Private reply failed — sending steady fallback")
            return (
                "I'm here and I'm not going anywhere. Say a bit more when you're ready — "
                "or if it's heavy right now, call John or Kiefer, and message me after."
            )

    async def _private_context(self, query: str) -> str:
        if self._memory is None:
            return ""
        try:
            hits = await self._memory.search(
                query, k=4, rooms=["private"], include_private=True, min_score=0.1
            )
            return "\n".join(f"- {h['content']}" for h in hits)
        except Exception:
            logger.exception("Private recall failed")
            return ""

    async def _analyse(self, transcript: str) -> dict[str, Any]:
        try:
            raw = await self._claude.quick(transcript, system=ANALYSIS_SYSTEM, max_tokens=150)
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            return json.loads(match.group(0)) if match else {}
        except Exception:
            logger.exception("Private analysis failed")
            return {}

    async def _record(self, kind: str, content: str, mood: int, day_count: int) -> None:
        await self._db.execute(
            "INSERT INTO sobriety (ts, kind, content, mood, day_count) VALUES (?, ?, ?, ?, ?)",
            (utc_now_iso(), kind, self._box.seal(content), int(mood or 0), day_count),
        )

    # ------------------------------------------------------------ trigger radar

    async def trigger_message(self, travel_flags: list[str], today: date) -> str:
        """A separate warm heads-up on flight/trade-show days (heartbeat calls
        this AFTER the business brief; sent as its own message, never merged)."""
        if not travel_flags:
            return ""
        day = await self.day_count(today)
        day_part = f" Day {day} travels with you." if day else ""
        return (
            "One more thing, just between us — today has travel or an event in it, and days like "
            "this can pull at you. Plan the journey, keep water in hand, and message me from the "
            f"lounge, the stand, wherever. I'm with you all day.{day_part}"
        )
