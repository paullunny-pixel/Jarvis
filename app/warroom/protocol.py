"""§4 — the protocol. No free-form chat between models, ever: each phase is
a single structured call per seat, run in parallel, JSON in and out so the
chair (service.py/report.py) can merge programmatically rather than
re-parsing three essays.

Word limits are enforced by a hard trim after the call rather than a
second corrective round-trip per seat — cheaper, and the brief's 'one
correction, then trimmed' outcome is what a wandering seat gets either way.
"""
from __future__ import annotations

import json
import logging
import re

from app.warroom.seats import FALLBACK_CHAIN, Seat

logger = logging.getLogger(__name__)

JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)

# $/1M tokens (input, output). Fable/Sol/Terra rates are the brief's own
# numbers (§1); the Gemini rates are NOT specified there — estimated at a
# reasonable Pro/Flash tier and flagged as such wherever cost is reported.
COST_TABLE = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "gpt-5.6-sol": (5.0, 30.0),
    "gpt-5.6-terra": (2.5, 15.0),
    "gemini-3.1-pro-preview": (1.25, 5.0),    # estimated — not in the brief
    "gemini-3.6-flash": (0.10, 0.40),          # estimated — not in the brief
}
DEFAULT_COST = (5.0, 20.0)   # unknown model id — conservative, keeps budget checks safe
ESTIMATED_MODELS = {"gemini-3.1-pro-preview", "gemini-3.6-flash"}


def _estimate_cost(model: str, usage: dict) -> float:
    inp_rate, out_rate = COST_TABLE.get(model, DEFAULT_COST)
    return (usage.get("input", 0) / 1_000_000) * inp_rate + (usage.get("output", 0) / 1_000_000) * out_rate


def _cap_words(text: str, limit: int) -> str:
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]) + " […trimmed to the word limit]"


async def _call_anthropic(claude, model: str, system: str, prompt: str, max_tokens: int) -> dict:
    from app.clients.anthropic_client import ClaudeError

    payload = {
        "model": model, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        data = await claude._call(payload)
    except ClaudeError as exc:
        if "404" in str(exc) or "not_found" in str(exc).lower():
            return {"text": "", "refused": False, "model_missing": True, "usage": {"input": 0, "output": 0}}
        raise
    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    usage_raw = data.get("usage") or {}
    return {
        "text": text,
        "refused": data.get("stop_reason") == "refusal",
        "model_missing": False,
        "usage": {"input": usage_raw.get("input_tokens", 0), "output": usage_raw.get("output_tokens", 0)},
    }


async def call_seat(clients: dict, claude, seat: Seat, system: str, prompt: str, word_limit: int) -> dict:
    """One structured call to one seat. Returns {'text','refused','cost_usd',
    'fallback_from','error','parsed'}. `seat.model` is mutated in place if a
    fallback is used, so the caller's record of who-sat-where stays honest."""
    max_tokens = max(300, word_limit * 3)

    async def _dispatch(model: str) -> dict:
        if seat.vendor == "anthropic":
            return await _call_anthropic(claude, model, system, prompt, max_tokens)
        if seat.vendor == "openai":
            client = clients.get("openai")
            if client is None:
                return {"text": "", "refused": False, "model_missing": False,
                         "usage": {"input": 0, "output": 0}, "error": "OPENAI_API_KEY not configured"}
            return await client.chat(model, system, prompt, max_tokens=max_tokens, effort=seat.effort)
        if seat.vendor == "google":
            client = clients.get("google")
            if client is None:
                return {"text": "", "refused": False, "model_missing": False,
                         "usage": {"input": 0, "output": 0}, "error": "GOOGLE_AI_API_KEY not configured"}
            return await client.generate(model, system, prompt, max_tokens=max_tokens)
        raise ValueError(f"unknown vendor {seat.vendor}")

    result = await _dispatch(seat.model)
    if result.get("error"):
        return {"text": "", "refused": False, "cost_usd": 0.0, "fallback_from": "", "error": result["error"], "parsed": {}}

    fallback_from = ""
    if result.get("model_missing"):
        fallback = FALLBACK_CHAIN.get(seat.vendor, {}).get(seat.model, "")
        if not fallback:
            return {
                "text": "", "refused": False, "cost_usd": 0.0, "fallback_from": seat.model,
                "error": f"{seat.model} is unavailable and no fallback is configured for {seat.vendor}",
                "parsed": {},
            }
        fallback_from = seat.model
        seat.model = fallback
        logger.warning("War Room: %s unavailable, falling back to %s", fallback_from, fallback)
        result = await _dispatch(fallback)

    cost = _estimate_cost(seat.model, result.get("usage", {}))
    # Parse from the FULL untrimmed text — word-capping raw JSON risks cutting
    # mid-object and breaking parseability entirely. The word limit is instead
    # a generation-time budget (max_tokens above) plus a prompt instruction;
    # here it only trims the individual prose VALUES inside the parsed object,
    # so structure survives even if a seat ran long.
    raw = result.get("text", "")
    match = JSON_OBJ.search(raw)
    try:
        parsed = json.loads(match.group(0)) if match else {}
    except Exception:
        parsed = {}
    if isinstance(parsed, dict):
        parsed = {
            k: (_cap_words(v, word_limit) if isinstance(v, str) else v)
            for k, v in parsed.items()
        }
    return {
        "text": raw, "refused": result.get("refused", False), "cost_usd": cost,
        "fallback_from": fallback_from, "error": "", "parsed": parsed,
    }


# --------------------------------------------------------------- phase prompts

def phase1_system(seat: Seat, brief: str) -> str:
    from app.warroom.seats import ROLE_BRIEFS

    return f"""\
{ROLE_BRIEFS[seat.role]}

You are one of three independent seats reviewing Paul's business question, \
each from a different AI vendor. You CANNOT see the other seats' answers — \
this is deliberate, so give your own genuinely independent read, not a \
hedge. Reply with ONLY a JSON object:
{{"assessment": "<your read of the situation, {seat.role} lens>",
 "recommendation": "<what you'd do>",
 "reasoning": "<why, briefly>",
 "confidence": "high|medium|low",
 "need_to_know": "<what would make you more sure>"}}

BUSINESS CONTEXT:
{brief}"""


def phase2_system(seat: Seat, other_two: list[dict], quick: bool) -> str:
    others = "\n\n".join(
        f"SEAT {o['key']} ({o['role']}): {json.dumps(o['parsed'])}" for o in other_two
    )
    closing = (
        '"final_position": "<your final position, under 100 words, since this closes the session>"'
        if quick else '"final_position": ""'
    )
    return f"""\
You are still the {seat.role.upper()} seat. You've now been shown the other \
two seats' Phase 1 answers verbatim, below. "I agree with everything" is a \
REJECTED response — you must name something. Reply with ONLY a JSON object:
{{"missed_point": "<the strongest point another seat made that you'd missed — forced, name one>",
 "disagreement": "<where you disagree, specifically, and why — not 'I broadly agree but would add'>",
 "collective_gap": "<what all three of you have collectively ignored>",
 {closing}}}

THE OTHER TWO SEATS' PHASE 1 ANSWERS:
{others}"""


DISSENT_PUSH = (
    "\n\nYour last answer read as agreement with no real dissent. That is "
    "REJECTED — push harder. Find the specific place you'd actually do "
    "something differently, or a risk the others underweighted. A board "
    "that agrees instantly is weak evidence, not strong; earn your seat."
)


def looks_like_agreement(parsed: dict) -> bool:
    disagreement = str(parsed.get("disagreement", "")).strip().lower()
    if len(disagreement) < 15:
        return True
    agreement_phrases = ("i agree with everything", "no disagreement", "nothing to add", "fully agree")
    return any(p in disagreement for p in agreement_phrases)


def phase3_system(seat: Seat, phase2_all: list[dict]) -> str:
    from app.warroom.seats import ROLE_BRIEFS

    others = "\n\n".join(
        f"SEAT {o['key']} ({o['role']}): {json.dumps(o['parsed'])}" for o in phase2_all
    )
    return f"""\
{ROLE_BRIEFS[seat.role]}

Real disagreement survived the cross-examination round. This is the ONE \
rebuttal round — make it count. Reply with ONLY a JSON object:
{{"rebuttal": "<your response to what the others said, and whether it changes your position>"}}

ALL THREE SEATS' PHASE 2 CROSS-EXAMINATION:
{others}"""


def phase4_system(seat: Seat) -> str:
    return f"""\
You are the {seat.role.upper()} seat. Jarvis (the chair) is calling time — \
final conclusions now, under 200 words. Reply with ONLY a JSON object:
{{"final_recommendation": "<your final call>",
 "confidence": "high|medium|low",
 "would_change_mind": "<what would change it>",
 "must_not_get_wrong": "<the one thing Paul must not get wrong>"}}"""
