"""The Paul Brief — Jarvis's living picture of Paul (Phase A1, 31 Jul 2026).

One evolving profile document injected into every brain turn: who Paul is,
his people, preferences, how he wants to be spoken to, and his CURRENT
situation (location, travel, focus, state of mind). Rewritten nightly from
the day's conversation and on demand ('update your brief'). This is what
turns retrieval into familiarity — Jarvis always knows him, rather than
looking him up.

The private wall holds here too: sobriety-track content never enters the
brief (the message log only ever carries redacted '[private exchange]'
markers, and private-room living facts are excluded at the query).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

BRIEF_KEY = "paul_brief"
PERSONA_NOTES_KEY = "jarvis_persona_notes"

BRIEF_SYSTEM = """\
You maintain THE PAUL BRIEF — a living profile of Paul that his AI
chief-of-staff (Jarvis) carries into every conversation. Rewrite it now,
merging the existing brief with what today's material adds or changes.

Sections, in this order:
1. WHO HE IS — identity, family, partner, kids, health basics (ADHD, gastric
   sleeve, in recovery — no detail beyond that), what drives him.
2. HIS PEOPLE — names and one-line context for everyone who matters.
3. HIS BUSINESSES — each company in a line or two, current headline state.
4. HOW TO BE WITH HIM — tone he responds to, what lands, what grates,
   things he has explicitly asked for.
5. RIGHT NOW — location and timezone, travel coming up, active priorities,
   current worries, anything time-sensitive. Date-stamp each item.

Rules: keep it under 700 words — tight, factual, current. Prefer newer
information; drop stale 'RIGHT NOW' items that have clearly passed. Never
include sobriety-track details beyond 'in recovery'. Output ONLY the brief.\
"""


async def compose_brief(claude, db, store) -> str:
    """Rebuild the brief from the existing one + recent conversation +
    living facts. Returns the new brief ('' on failure — keep the old)."""
    from app.core.store import MessageLog
    from app.memory.store import LivingFacts

    existing = await store.get(BRIEF_KEY, "")
    rows = await MessageLog(db).recent(limit=150)
    convo = "\n".join(
        f"{'Paul' if r['direction'] == 'in' else 'Jarvis'}: {r['transcript'][:400]}"
        for r in rows
        if r["transcript"] and r["transcript"] != "[private exchange]"
    )[-14000:]
    facts = await LivingFacts(db).all_current(exclude_private=True)
    facts_text = "\n".join(f"- {f['key']}: {f['value']}" for f in facts)[:4000]
    material = (
        f"EXISTING BRIEF:\n{existing or '(none yet — write the first one)'}\n\n"
        f"LIVING FACTS ON RECORD:\n{facts_text or '(none)'}\n\n"
        f"RECENT CONVERSATION (oldest first):\n{convo or '(none)'}"
    )
    try:
        brief = (
            await claude.converse(
                BRIEF_SYSTEM, [{"role": "user", "content": material}],
                max_tokens=1500, timeout=180.0,
            )
        ).strip()
    except Exception:
        logger.exception("Brief composition failed — keeping the previous brief")
        return ""
    if brief:
        await store.set(BRIEF_KEY, brief[:8000])
    return brief
