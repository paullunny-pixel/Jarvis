"""§5 — Jarvis merges. One report component, one set of nine headings, two
lengths of content (§5 Quick Board table) — never a cut-down layout. All
nine keys are ALWAYS present in the returned dict, even when a section has
nothing to say, so a surface can tell 'we checked, it was clean' apart
from 'we didn't check' (§5's own words on why that distinction matters).
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)

SECTION_KEYS = (
    "verdict", "recommendation", "action_points", "disagreement",
    "discarded", "gaps", "confidence", "jarvis_note",
)

EMPTY_REPORT = {
    "verdict": "",
    "recommendation": "",
    "action_points": [],
    "disagreement": "no real split",
    "discarded": "nothing discarded",
    "gaps": "no gaps I can see",
    "confidence": {"level": "low", "would_change": ""},
    "jarvis_note": "",
    "measurable": "",
}

CHAIR_SYSTEM_TEMPLATE = """\
You are Jarvis, chairing a board of three AI models (Anthropic, OpenAI, \
Google) that just debated Paul's question. You are NOT a fourth advisor — \
merge their work, don't add your own strategic opinion into the debate \
(a short, clearly-separate 'jarvis_note' at the end is the one place your \
own read belongs — something you know that the board didn't, like Paul's \
actual capacity this month or a decision already made).

Tier: {tier_label}. {length_guidance}

Write the report as ONLY a JSON object with these keys:
{{"verdict": "<one line — {verdict_guidance}>",
 "recommendation": "<{recommendation_length}>",
 "action_points": [{{"title": "<short, specific, in Paul's words>", "why": "<one-line reason>", "owner_suggestion": "<a name from OWNER CANDIDATES below, or empty for Paul>", "due": "<natural-language date if inferable, else empty>"}}],
 "disagreement": "<{disagreement_length} — where the three seats split and what it hinges on. If they didn't really disagree, say 'no real split' honestly>",
 "discarded": ["<idea considered and rejected>", ...] or "nothing discarded",
 "gaps": "<{gaps_length} — what none of the three seats addressed, your gap check. If genuinely nothing, say 'no gaps I can see'>",
 "confidence": {{"level": "high|medium|low", "would_change": "<what would change the answer>"}},
 "jarvis_note": "<your context-aware reality check, clearly separate from the board's view — or empty if you have nothing to add>",
 "measurable": "{measurable_instruction}"}}

{consensus_note}

OWNER CANDIDATES (People room): {owners}

THE FULL DEBATE TRANSCRIPT (all phases, all seats):
{transcript}
"""


def _length_guidance(tier: str) -> dict:
    if tier == "full":
        return {
            "tier_label": "FULL BOARD",
            "length_guidance": "Write the full-length report — this was an 8-10 minute, business-critical debate.",
            "verdict_guidance": "what to do",
            "recommendation_length": "a short paragraph, plain English",
            "disagreement_length": "full treatment",
            "gaps_length": "full treatment",
            "measurable_instruction": "",
        }
    return {
        "tier_label": "QUICK BOARD",
        "length_guidance": (
            "Write it SHORT — target one screen, not half a page. The saving comes "
            "from brevity per section, never from deleting a section."
        ),
        "verdict_guidance": "is this even worth doing, honestly — say so plainly if not",
        "recommendation_length": "2-3 sentences",
        "disagreement_length": "1-2 sentences, or 'no real split'",
        "gaps_length": "1-2 sentences",
        "measurable_instruction": "<one line: what 'done' looks like and the one number that tells you it worked>",
    }


def _consensus_note(consensus_weak: bool) -> str:
    if not consensus_weak:
        return ""
    return (
        "IMPORTANT: the three seats converged with no real dissent even after "
        "a mandatory push for disagreement. State this plainly in 'disagreement' "
        "and treat it as WEAK evidence, not strong — easy agreement usually means "
        "the question was too easy or too vague, not that the answer is safe."
    )


def chair_prompt(
    tier: str, transcript: list[dict], owners: list[str], consensus_weak: bool,
) -> str:
    guidance = _length_guidance(tier)
    return CHAIR_SYSTEM_TEMPLATE.format(
        transcript=json.dumps(transcript, indent=None)[:40000],
        owners=", ".join(owners) or "Paul only",
        consensus_note=_consensus_note(consensus_weak),
        **guidance,
    )


async def merge_report(claude, tier: str, transcript: list[dict], owners: list[str], consensus_weak: bool) -> dict:
    """Phase 5 — the one chair call that turns the debate into the report.
    Never raises: a failed merge returns EMPTY_REPORT with jarvis_note
    explaining it, so a session always produces SOMETHING (§6 budget-stop
    rule: 'deliver from what exists rather than failing')."""
    prompt = chair_prompt(tier, transcript, owners, consensus_weak)
    try:
        raw = await claude.converse(
            "You write structured JSON reports for a business chief-of-staff. "
            "Reply with ONLY the JSON object, nothing else.",
            [{"role": "user", "content": prompt}],
            max_tokens=3000,
        )
    except Exception:
        logger.exception("War Room chair merge failed")
        report = dict(EMPTY_REPORT)
        report["jarvis_note"] = "The merge itself failed — the raw transcript below is everything the board said."
        return report
    match = JSON_OBJ.search(raw)
    if not match:
        report = dict(EMPTY_REPORT)
        report["jarvis_note"] = "The merge came back malformed — the raw transcript below is everything the board said."
        return report
    try:
        parsed = json.loads(match.group(0))
    except Exception:
        report = dict(EMPTY_REPORT)
        report["jarvis_note"] = "The merge came back malformed — the raw transcript below is everything the board said."
        return report
    report = dict(EMPTY_REPORT)
    report.update({k: v for k, v in parsed.items() if k in report})
    if not isinstance(report.get("action_points"), list):
        report["action_points"] = []
    return report


def layout_parity_headings() -> tuple:
    """The nine (well, eight-plus-transcript) headings that must appear, in
    order, on BOTH tiers — the test in the brief (§8a) diffs against this."""
    return SECTION_KEYS + ("transcript",)
