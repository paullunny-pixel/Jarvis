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

EMAIL_HINT = re.compile(
    r"\b(email|e-?mails?|inbox(es)?|unread|mailbox)\b|\breply to\b"
    r"|\bcheck (my )?(mail|gmail)\b|\bdraft (a|an|the|me)\b",
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
Messages he has recently been shown (1-indexed; may be empty):
{listing}

Reply ONLY a JSON array (empty if the message contains no email actions):
- {{"action":"check","account":"<inbox words or 'all'>"}}   (how's my email / anything new)
- {{"action":"read","account":"<inbox words or 'all'>","count":5}}  (read them to me)
- {{"action":"draft","reply_index":<number from the list, or 0>,"to":"<address if he gave one, else empty>","account":"<sending inbox words or empty>","subject":"<subject, or empty>","body":"<the email text, written out properly>"}}
- {{"action":"update","to":"","subject":"","body":"","account":""}}  (he's changing the draft he already has — fill ONLY what changes, leave the rest empty)
- {{"action":"cancel"}}   (he wants to scrap the current draft)
For drafts: write the body COMPLETE and ready to send, signed off 'Paul'.
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
                labels=", ".join(labels), listing=shown or "(none)", style_block=style_block
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


async def execute_actions(service: MailService, actions: list[dict[str, Any]]) -> list[str]:
    results: list[str] = []
    for action in actions[:4]:
        kind = action.get("action")
        try:
            if kind == "check":
                results.append(await service.overview())
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
