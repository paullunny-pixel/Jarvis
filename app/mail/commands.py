"""Voice → email actions, in the daily12/commands mould.

A keyword gate keeps the parser off ordinary conversation; a cheap Haiku call
turns "anything new on the Prodermis inbox? reply to Alicia saying the
registration pack's on its way" into structured actions. Sending is NEVER an
action the parser can trigger — only the explicit confirm phrases, checked
against a live pending draft, release an email.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.clients.anthropic_client import ClaudeClient
from app.mail.service import MailService

logger = logging.getLogger(__name__)

# The gate needs an email VERB, not just the word 'email' — "apologies for
# the delay in emails" inside a message Paul is composing must never hijack
# the conversation into the mail pipeline.
EMAIL_HINT = re.compile(
    r"\b(check|read|show|draft|write|send|forward|reply)\b.{0,30}\b(e-?mails?|inbox|mailbox)\b"
    r"|\b(any|how'?s|what'?s)\b.{0,20}\b(e-?mails?|inbox(es)?)\b"
    r"|\breply to\b|\binbox(es)?\b|\bunread\b|\bmailbox\b"
    r"|\bcheck (my )?(mail|gmail)\b",
    re.IGNORECASE,
)

# Only consulted when a draft is actually pending — so a stray "send it"
# in task talk can't touch email, and email can't send without a read-back.
CONFIRM_SEND = re.compile(
    r"^\s*(yes[,.!]?\s*)?(send( it| that| them)?|fire it off|off it goes|send the (email|reply|message))"
    r"[.!\s]*$|\byes,? send( it| that)?\b",
    re.IGNORECASE,
)
CANCEL_SEND = re.compile(
    r"\b(don'?t send|do not send|scrap (it|that|the (email|draft))|bin (it|that)"
    r"|cancel (it|that|the (email|draft)))\b",
    re.IGNORECASE,
)

PARSER_SYSTEM = """\
You convert Paul's message into email actions. His connected inboxes: {labels}.

CRITICAL: only output actions Paul EXPLICITLY requested about EMAIL. He often
composes or edits OTHER messages (WhatsApp/group/text) in conversation — if
he's dictating or amending wording, reply [] even when the words 'email' or
'emails' appear INSIDE that content. Never start a fresh draft when he's
clearly changing something already being worked on elsewhere.

REGISTER RULE (from Paul): anyone who is NOT one of his registered
person-contacts is a BUSINESS contact — polite, professional, formal, no
matey sign-offs. His casual register is reserved ONLY for the named people.
Messages he has recently been shown (1-indexed; may be empty):
{listing}

Reply ONLY a JSON array (empty if the message contains no email actions):
- {{"action":"check","account":"<inbox words or 'all'>"}}   (how's my email / anything new)
- {{"action":"read","account":"<inbox words or 'all'>","count":5}}  (read them to me — NEW mail only)
- {{"action":"research","account":"<inbox words or 'all'>","query":"<the 1-3 words to search the WHOLE mailbox for, e.g. a sender or company name like 'BMI'>","instruction":"<what Paul wants produced from those emails, restated in full>"}}   (read/summarise/dig through ALL email about a sender or topic — 'read all my emails from X and...', 'where are we up to with...', anything needing old mail. Emit research ALONE, never with a draft — Paul reviews the result first)
- {{"action":"draft","reply_index":<number from the list, or 0>,"to":"<address if he gave one, else empty>","account":"<sending inbox words or empty>","subject":"<subject, or empty>","body":"<the email text, written out properly>"}}
- {{"action":"update","to":"","subject":"","body":"","account":""}}  (he's changing the draft he already has — fill ONLY what changes, leave the rest empty)
- {{"action":"cancel"}}   (he wants to scrap the current draft)
For drafts: write the body COMPLETE and ready to send, signed off 'Paul'.
A body must be FINISHED words only — NEVER a placeholder like '[summary to
follow]'. If the content requires reading emails first, that is research, not
a draft. When Paul asks to send/forward the research he was just shown, use
its text as the body.
LAST RESEARCH RESULT Paul has seen (may be empty):
{research}
{style_block}
NEVER invent a recipient address — if he named a
person from the shown list use reply_index; otherwise leave "to" empty. A
draft with no recipient is fine: it's held until he gives the address ("leave
it in draft" / "no address yet" = exactly that — use update with all fields
empty if a draft already exists, never ask for the address).
Only include actions he clearly asked for.\
"""


def mentions_email(text: str) -> bool:
    return bool(EMAIL_HINT.search(text))


def confirms_send(text: str) -> bool:
    return bool(CONFIRM_SEND.search(text)) and not bool(CANCEL_SEND.search(text))


def cancels_send(text: str) -> bool:
    return bool(CANCEL_SEND.search(text))


async def parse_actions(
    claude: ClaudeClient, text: str, labels: list[str], listing: list[dict],
    style: str = "", person_styles: dict[str, dict] | None = None,
    research: str = "",
) -> list[dict[str, Any]]:
    shown = "\n".join(
        f"{i}. [{m['account']}] {m['from']} — {m['subject']}" for i, m in enumerate(listing, 1)
    )
    style_block = (
        "Write in PAUL'S OWN VOICE — match this style guide exactly (learned from "
        "his real voice notes and messages):\n" + style
        if style
        else "Write in Paul's professional voice."
    )
    if person_styles:
        lines = [
            f"— {name} ({info.get('email') or info.get('phone') or 'no address'}): {info['guide']}"
            for name, info in person_styles.items()
        ]
        style_block += (
            "\nPERSON-SPECIFIC voices — when the draft is TO one of these people "
            "(matched by name or address), write in THEIR voice instead of the default:\n"
            + "\n".join(lines)
        )
    try:
        raw = await claude.quick(
            text,
            system=PARSER_SYSTEM.format(
                labels=", ".join(labels), listing=shown or "(none)",
                style_block=style_block, research=research or "(none)",
            ),
            max_tokens=800,
        )
        start, end = raw.find("["), raw.rfind("]")
        if start < 0:
            return []
        actions = json.loads(raw[start : end + 1])
        return [a for a in actions if isinstance(a, dict) and a.get("action")]
    except Exception:
        logger.exception("Email action parsing failed")
        return []


async def execute_actions(
    service: MailService, actions: list[dict[str, Any]], claude: ClaudeClient | None = None
) -> list[str]:
    results: list[str] = []
    for action in actions[:4]:
        kind = action.get("action")
        try:
            if kind == "check":
                results.append(await service.overview())
            elif kind == "research" and claude is not None:
                results.append(
                    await service.research(
                        claude,
                        str(action.get("query", "") or ""),
                        str(action.get("instruction", "") or ""),
                        account_hint=str(action.get("account", "") or ""),
                    )
                )
            elif kind == "read":
                results.append(
                    await service.read(
                        str(action.get("account", "") or ""),
                        limit=min(int(action.get("count") or 5), 10),
                    )
                )
            elif kind == "draft":
                results.append(
                    await service.draft(
                        str(action.get("body", "") or ""),
                        to=str(action.get("to", "") or ""),
                        subject=str(action.get("subject", "") or ""),
                        account_hint=str(action.get("account", "") or ""),
                        reply_index=int(action.get("reply_index") or 0),
                    )
                )
            elif kind == "update":
                results.append(
                    await service.update_draft(
                        to=str(action.get("to", "") or ""),
                        subject=str(action.get("subject", "") or ""),
                        body=str(action.get("body", "") or ""),
                        account_hint=str(action.get("account", "") or ""),
                    )
                )
            elif kind == "cancel":
                results.append(await service.cancel_draft())
        except Exception:
            logger.exception("Email action failed: %s", action)
            results.append(f"Something went wrong with the email {kind} — try that again.")
    return results
