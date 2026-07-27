"""Paul's writing voice, learned from Paul's own sent emails.

'Learn my email style' samples the Sent folder of every connected account,
strips the quoted reply-chains so only HIS words remain, and has the brain
distill a style guide (greetings, sign-offs, rhythm, formality per account).
The guide is stored in settings and injected into every draft prompt from
then on — re-run the command any time to refresh it.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

STYLE_KEY = "email_style_profile"

QUOTE_MARKERS = re.compile(
    r"^On .{5,80}wrote:\s*$|^-{2,}\s*Original Message\s*-{2,}$|^From: .+$|^_{5,}\s*$",
    re.IGNORECASE | re.MULTILINE,
)

ANALYSIS_SYSTEM = """\
You are analysing how PAUL genuinely communicates — transcripts of his own
voice notes, his typed chat messages, and WhatsApp messages he wrote himself.
(His emails are deliberately NOT the source: many were AI-drafted and don't
sound like him. This material is the real Paul.)
Produce a compact style guide (max ~250 words) another writer can follow to
draft emails and messages that sound EXACTLY like Paul writing them:
- his characteristic words, phrases and openers (quote them exactly)
- sentence length, rhythm, punctuation habits, how blunt or warm he is
- humour, energy, how he greets and signs off with different people
- how to adapt his spoken voice into written form WITHOUT flattening it
  into generic business English — keep it unmistakably him
Do NOT include any confidential facts, names of deals, figures or content —
style ONLY. Reply with just the guide.\
"""


PERSON_ANALYSIS_SYSTEM = """\
You are analysing messages PAUL wrote to ONE specific person: {name}
({relationship_hint}). Capture how Paul talks to THIS person, which differs
from his default register. Produce a compact guide (max ~200 words):
- the register (banter? blunt shorthand? in-jokes? how casual?)
- exact greetings, nicknames and sign-offs he uses with them
- rhythm, warmth, directness, humour level with this person specifically
Style ONLY — no confidential facts or content. Reply with just the guide.\
"""


def strip_quoted(text: str) -> str:
    """Keep only Paul's own words: cut everything from the first quoted-reply
    marker down, and drop '>' quoted lines."""
    match = QUOTE_MARKERS.search(text)
    if match:
        text = text[: match.start()]
    lines = [l for l in text.splitlines() if not l.lstrip().startswith(">")]
    return "\n".join(lines).strip()


async def build_style_profile(claude, samples_by_account: dict[str, list[str]]) -> str:
    """Distill the style guide from cleaned samples across all accounts."""
    sections = []
    for label, samples in samples_by_account.items():
        cleaned = [strip_quoted(s) for s in samples]
        cleaned = [c for c in cleaned if len(c) > 40][:10]
        if cleaned:
            joined = "\n---\n".join(cleaned)
            sections.append(f"## Emails Paul sent from his {label} account\n{joined}")
    if not sections:
        return ""
    corpus = "\n\n".join(sections)[:24000]
    history = [{"role": "user", "content": corpus}]
    guide = await claude.converse(ANALYSIS_SYSTEM, history)
    return (guide or "").strip()


async def build_person_profile(claude, name: str, samples: list[str], hint: str = "") -> str:
    cleaned = [strip_quoted(s) for s in samples]
    cleaned = [c for c in cleaned if len(c) > 20][:15]
    if not cleaned:
        return ""
    corpus = "\n---\n".join(cleaned)[:16000]
    system = PERSON_ANALYSIS_SYSTEM.format(
        name=name.title(), relationship_hint=hint or "someone Paul writes to often"
    )
    guide = await claude.converse(system, [{"role": "user", "content": corpus}])
    return (guide or "").strip()
