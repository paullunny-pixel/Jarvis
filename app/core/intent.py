"""The understanding layer (3 Aug): when no exact phrase matches, the fast
model reads Paul's message — dyslexia, autocorrect, garbled voice transcripts
and all — and says whether it was a known command in disguise. Understanding
by model, execution by the same deterministic machinery as ever; anything it
isn't sure about falls through to the brain conversation unchanged."""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Messages longer than this are conversation, not commands — skip the triage.
TRIAGE_MAX_CHARS = 200

# build_list_add is deliberately NOT here (4 Aug): a garbled voice note
# ('add to Trello' transcribed as 'Activate') got confidently filed as a
# build-list wish. Additions need judgement and context — brain only.
INTENTS = (
    "quiet_day", "notifications_on", "wake_skip", "wake_delay", "status_check",
    "group_digest", "trello_sync", "update_brief",
    "build_list_show", "timezone_change", "none",
)

TRIAGE_SYSTEM = """You read ONE short message from Paul to his assistant Jarvis and decide whether it is one of the known COMMANDS in disguise. Paul has dyslexia: spellings wobble, autocorrect mangles words ('quite day' means 'quiet day'), voice transcripts garble. Read for MEANING, never spelling.

COMMANDS:
- quiet_day — silence today's nudges/notifications/reminders ("quite day", "leave me be today", "no more pings")
- notifications_on — resume the nudges
- wake_skip — skip tomorrow's wake-up sequence entirely
- wake_delay — move tomorrow's wake-up later (set "hour", 4–11)
- status_check — is everything connected / system status
- group_digest — summarise the work groups now
- trello_sync — refresh/resync the board
- update_brief — rebuild your brief/picture of Paul
- build_list_show — read the build list back
- timezone_change — Paul says where he is now (set "place", e.g. "dubai", "uk")
- none — ordinary conversation, a question, or anything you are not SURE about

Task/board/card talk, email talk, logging things as done, water/meds/run logging, adding wishes to the build list: ALWAYS "none" — other machinery owns those.

Voice transcripts GARBLE badly — 'add to Trello' can arrive as 'Activate'. If the wording looks mangled, or you have to guess at what any word was meant to be, it is "none": the brain reads it with full context instead. Acting confidently on a garble is the worst outcome.

Reply with ONLY JSON:
{"intent": "<command or none>", "hour": <int|null>, "wish": <string|null>, "place": <string|null>, "confident": <true|false>}

confident=true ONLY when the meaning is unmistakable. In doubt → "none"."""


async def classify(claude, transcript: str, recent: str = "") -> dict[str, Any] | None:
    """One fast-model look at an unmatched message. None = not a command
    (or the classifier failed) — the caller carries on to the brain."""
    prompt = ""
    if recent:
        prompt = f"RECENT CONVERSATION (context only):\n{recent[-1500:]}\n\n"
    prompt += f"PAUL'S MESSAGE:\n{transcript}"
    try:
        raw = await claude.quick(prompt, system=TRIAGE_SYSTEM, max_tokens=150)
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        logger.exception("Intent triage failed — falling through to the brain")
        return None
    intent = data.get("intent")
    if not data.get("confident") or intent not in INTENTS or intent == "none":
        return None
    return data
