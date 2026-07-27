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
You are analysing emails PAUL himself wrote, to capture his authentic
writing voice. Produce a compact style guide (max ~250 words) another writer
can follow to draft emails indistinguishable from Paul's own:
- greetings and sign-offs he actually uses (quote them exactly)
- sentence length, rhythm, punctuation habits (dashes? exclamation marks?)
- formality level and how it shifts between business and personal
- characteristic phrases, warmth level, how direct he is, quirks
Do NOT include any confidential facts, names of deals, figures or content
from the emails — style ONLY. Reply with just the guide.\
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
