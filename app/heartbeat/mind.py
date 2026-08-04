"""The Continuous Mind (Phase A3, 4 Aug): Jarvis thinks between conversations.

Once an hour through the waking day, the BRAIN model (not a template, not a
cron string) reads the whole situation — time where Paul actually is, his
rhythm switches, the board, the calendar, the day's conversation, his living
brief — and decides the only question that matters: is anything worth saying
right now? Usually the answer is silence. When it speaks, it speaks because a
sharp, warm friend who runs his life would have.

The existing machinery (briefs, med chases, bedtime, wake-ups) stays as the
safety net underneath; the mind is told what the machinery already covers so
it never duplicates. A nightly reflection (riding the 21:55 brief refresh)
writes MIND NOTES for tomorrow — what to watch, how to open, what today
taught — injected into tomorrow's brain turns and morning brief.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

MIND_ENABLED_KEY = "mind_enabled"      # anything but "off" = thinking
MIND_NOTES_KEY = "mind_notes"          # {"date": iso, "notes": str}

MIND_SYSTEM = """\
You are Jarvis, Paul's AI chief-of-staff, on your hourly THINKING PASS — Paul \
hasn't messaged; you woke yourself to look at his situation and decide whether \
anything is worth saying, unprompted.

THE BAR: would a sharp, warm friend who runs his life send this message right \
now, uninvited? Silence is usually the right answer — a quiet hour is a good \
hour, and an unnecessary ping teaches Paul to ignore you. Speak when you \
genuinely notice something: a calendar event he may not be ready for, a long \
silent stretch on a day he planned big things, encouragement at the moment it \
lands, a pattern worth naming gently. Recovery weeks need the steady friend, \
not the coach.

YOU CANNOT ACT on this pass — no board changes, no switches, no promises of \
either. You can only speak or stay silent. If something needs doing, say it \
to Paul in plain words and it gets done in conversation.

HARD RULES:
- NEVER repeat, rephrase or top up anything already said today — the day's \
transcript is below; if your message resembles anything in it, don't send.
- The machinery already chases meds, the run, bedtime and the morning brief \
on its own schedule. Never duplicate its chasing. An excused gate (skipped \
run/meds) is SETTLED — never raise it.
- Quiet day active → speak only if genuinely important.
- Paul said goodnight, or it's before his wake hour → silence (reply speak=false).
- He messaged in the last few minutes → he's mid-conversation; stay out.
- Never comment on spelling; read for meaning. Comedy off when he's struggling.

Reply ONLY with JSON:
{"speak": true|false, "reason": "<one line, for the log>", "channel": "voice"|"text", "message": "<what to say — empty when silent>"}\
"""

REFLECTION_SYSTEM = """\
You are Jarvis reflecting at the end of Paul's day — the conversation and \
situation are below. Write MIND NOTES for tomorrow-you: 3-6 short lines. \
What happened that matters. What to watch for tomorrow. How to open the \
morning (tone, not script). Anything promised that must not be dropped. \
Plain text, no headers, under 120 words. Never include private-room content \
beyond 'recovery week' if relevant.\
"""


async def think(claude, situation: str) -> dict[str, Any] | None:
    """One thinking pass → the decision dict, or None (parse/API failure =
    silence; the mind never crashes the heartbeat)."""
    try:
        raw = await claude.converse(
            MIND_SYSTEM, [{"role": "user", "content": situation}], max_tokens=500
        )
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        logger.exception("Mind pass failed — staying silent")
        return None
    if not isinstance(data, dict) or not data.get("speak"):
        return None
    message = str(data.get("message") or "").strip()
    if not message:
        return None
    return {
        "message": message,
        "channel": "voice" if data.get("channel") == "voice" else "text",
        "reason": str(data.get("reason") or "")[:200],
    }


async def reflect(claude, situation: str) -> str:
    """The nightly reflection → MIND NOTES text ('' on failure)."""
    try:
        notes = await claude.converse(
            REFLECTION_SYSTEM, [{"role": "user", "content": situation}], max_tokens=400
        )
        return notes.strip()[:2000]
    except Exception:
        logger.exception("Nightly reflection failed — tomorrow starts without notes")
        return ""
