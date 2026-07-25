"""Auto-filing: anything Paul says gets mined for facts worth remembering and
filed to the right room and type (Plan §3) — using the cheap/fast model so it
costs pennies. Runs after each exchange, off the reply's critical path.
"""
from __future__ import annotations

import json
import logging
import re

from app.clients.anthropic_client import ClaudeClient
from app.memory.store import ROOMS, TYPES, LivingFacts, MemoryStore

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM = """\
You maintain the private knowledge base of a personal assistant. From the \
user's message, extract durable facts worth remembering long-term. Ignore \
chit-chat, questions, and one-off logistics. Reply with ONLY a JSON array \
(possibly empty), each item:
{"content": "<self-contained fact, third person>",
 "room": "you|companies|health|finances|people|private",
 "type": "STABLE|LIVING|PRIVATE",
 "tags": ["..."],
 "living_key": "<dotted.key or empty — set ONLY for LIVING numeric/status facts, e.g. health.weight_kg, villa.paid_pct>"}

Rules:
- Sobriety and medication → room "private", type "PRIVATE".
- Family and relationships (kids, partner, exes) → room "people", type "STABLE" —
  known to the assistant for planning, never shared onward.
- Numbers that change over time (weight, balances, targets, deadlines) → type "LIVING" with a living_key.
- Company/product/people facts → their rooms, usually STABLE.
- At most 5 items. Quality over quantity.\
"""

JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


async def extract_and_file(
    claude: ClaudeClient,
    memory: MemoryStore,
    living: LivingFacts,
    text: str,
    *,
    source: str = "conversation",
) -> int:
    """Returns how many facts were filed. Never raises — memory-writing must
    never break the conversation."""
    try:
        raw = await claude.quick(text, system=EXTRACTION_SYSTEM, max_tokens=800)
        match = JSON_ARRAY.search(raw)
        if not match:
            return 0
        items = json.loads(match.group(0))
        count = 0
        for item in items[:5]:
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            room = item.get("room", "you")
            type_ = item.get("type", "STABLE")
            room = room if room in ROOMS else "you"
            type_ = type_ if type_ in TYPES else "STABLE"
            living_key = str(item.get("living_key", "") or "").strip()
            if type_ == "LIVING" and living_key:
                await living.set(living_key, content, room=room)
            await memory.add_chunk(
                content,
                room=room,
                type_=type_,
                source=source,
                tags=[str(t) for t in item.get("tags", [])][:6],
            )
            count += 1
        return count
    except Exception:
        logger.exception("Memory extraction failed (conversation unaffected)")
        return 0


def format_memory_context(chunks: list[dict], living_facts: list[dict]) -> str:
    """Build the recalled-knowledge block for the system prompt."""
    lines: list[str] = []
    if living_facts:
        lines.append("Current living facts (always up to date, trust these):")
        lines.extend(f"- {f['key']}: {f['value']}" for f in living_facts)
    if chunks:
        if lines:
            lines.append("")
        lines.append("Recalled memory:")
        lines.extend(f"- [{c['room']}] {c['content']}" for c in chunks)
    return "\n".join(lines)
