"""Inbox triage and confirmed sending across all of Paul's accounts.

The safety contract (Paul chose it): Jarvis reads and drafts freely, but
NOTHING is sent until Paul confirms the read-back draft ("send it"). Exactly
one draft is pending at a time and it goes stale after 45 minutes, so a
throwaway "send it" hours later can never fire off a forgotten email.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from app.core.store import SettingsStore, utc_now_iso
from app.db.base import Database
from app.mail.client import MailClient

logger = logging.getLogger(__name__)

DRAFT_KEY = "pending_email_draft"
LISTING_KEY = "last_inbox_listing"
DRAFT_TTL_MINUTES = 45


class MailService:
    def __init__(self, clients: list[MailClient], db: Database) -> None:
        self._clients = clients
        self._settings = SettingsStore(db)

    @property
    def labels(self) -> list[str]:
        return [c.account.label for c in self._clients]

    # ------------------------------------------------------------- reading

    def match(self, hint: str) -> list[MailClient]:
        """'prodermis' / 'the derma inbox' / an address → the account(s)."""
        hint = (hint or "").strip().lower()
        if not hint or hint in ("all", "everything", "every account"):
            return list(self._clients)
        noise = {"the", "inbox", "email", "mail", "account", "one", "and", "for"}
        words = [w for w in re.findall(r"[a-z0-9@.\-]+", hint) if len(w) > 2 and w not in noise]

        def score(client: MailClient) -> int:
            hay = f"{client.account.label} {client.account.address}".lower()
            return sum(1 for w in words if w in hay)

        best = max((score(c) for c in self._clients), default=0)
        if best == 0:
            return list(self._clients)
        return [c for c in self._clients if score(c) == best]

    async def overview(self) -> str:
        """One line per inbox + headline unread — the 'how's my email' answer."""
        results = await asyncio.gather(
            *(c.unread(limit=3) for c in self._clients), return_exceptions=True
        )
        lines, listing = [], []
        for client, result in zip(self._clients, results):
            label = client.account.label
            if isinstance(result, Exception):
                logger.warning("Inbox check failed for %s: %s", label, result)
                lines.append(f"{label}: couldn't reach it just now")
                continue
            lines.append(f"{label}: {result['unread']} unread")
            for m in result["messages"]:
                listing.append({"account": label, **m})
                lines.append(f"  · {m['from']} — {m['subject']}")
        await self._remember_listing(listing)
        return "\n".join(lines) if lines else "No inboxes connected."

    async def read(self, hint: str = "", limit: int = 5) -> str:
        """Fuller read of one account (or all): senders, subjects, snippets."""
        clients = self.match(hint)
        results = await asyncio.gather(
            *(c.unread(limit=limit) for c in clients), return_exceptions=True
        )
        lines, listing = [], []
        for client, result in zip(clients, results):
            label = client.account.label
            if isinstance(result, Exception):
                lines.append(f"{label}: couldn't reach it just now")
                continue
            lines.append(f"{label} — {result['unread']} unread")
            if not result["messages"]:
                lines.append("  (nothing waiting)")
            for i, m in enumerate(result["messages"], start=len(listing) + 1):
                listing.append({"account": label, **m})
                snippet = f" — {m['snippet'][:110]}" if m["snippet"] else ""
                lines.append(f"{i}. {m['from']}: {m['subject']}{snippet}")
        await self._remember_listing(listing)
        return "\n".join(lines)

    async def _remember_listing(self, listing: list[dict]) -> None:
        slim = [
            {"account": m["account"], "from": m["from"], "from_address": m["from_address"],
             "subject": m["subject"]}
            for m in listing[:20]
        ]
        await self._settings.set(LISTING_KEY, json.dumps(slim))

    async def last_listing(self) -> list[dict]:
        try:
            return json.loads(await self._settings.get(LISTING_KEY, "[]"))
        except Exception:
            return []

    # ------------------------------------------------------------- drafting

    async def draft(
        self, body: str, to: str = "", subject: str = "", account_hint: str = "",
        reply_index: int = 0,
    ) -> str:
        """Store the one pending draft and read it back for confirmation."""
        account_label = ""
        if reply_index:
            listing = await self.last_listing()
            if not (1 <= reply_index <= len(listing)):
                return "I've lost track of which email that was — ask me to read the inbox again first."
            target = listing[reply_index - 1]
            to = to or target["from_address"]
            subject = subject or f"Re: {target['subject']}"
            account_label = target["account"]
        if not to:
            return "Who's it going to? Give me the address (or which email to reply to) and I'll draft it."
        client = self.match(account_hint or account_label)[0]
        draft = {
            "from": client.account.address,
            "from_label": client.account.label,
            "to": to,
            "subject": subject or "(no subject)",
            "body": body.strip(),
            "created": utc_now_iso(),
        }
        await self._settings.set(DRAFT_KEY, json.dumps(draft))
        return (
            f"Drafted from {draft['from_label']} ({draft['from']}) to {to}\n"
            f"Subject: {draft['subject']}\n\n{draft['body']}\n\n"
            "Say 'send it' and it goes — or 'scrap it', or tell me what to change."
        )

    async def pending_draft(self) -> dict | None:
        raw = await self._settings.get(DRAFT_KEY, "")
        if not raw:
            return None
        try:
            draft = json.loads(raw)
        except Exception:
            return None
        created = datetime.fromisoformat(draft["created"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created > timedelta(minutes=DRAFT_TTL_MINUTES):
            await self._settings.set(DRAFT_KEY, "")
            return None
        return draft

    async def send_pending(self) -> str:
        draft = await self.pending_draft()
        if draft is None:
            return "There's no draft waiting to send — dictate one and I'll read it back first."
        client = next(
            (c for c in self._clients if c.account.address == draft["from"]), self._clients[0]
        )
        try:
            await client.send(draft["to"], draft["subject"], draft["body"])
        except Exception:
            logger.exception("Email send failed")
            return (
                f"Couldn't get that away from {draft['from_label']} — the draft's still here; "
                "say 'send it' to try again shortly."
            )
        await self._settings.set(DRAFT_KEY, "")
        return f"Sent — '{draft['subject']}' to {draft['to']} from {draft['from_label']}."

    async def cancel_draft(self) -> str:
        if await self.pending_draft() is None:
            return "Nothing pending to scrap."
        await self._settings.set(DRAFT_KEY, "")
        return "Scrapped. Nothing was sent."

    # -------------------------------------------------------------- status

    async def health(self) -> list[dict]:
        return list(await asyncio.gather(*(c.check() for c in self._clients)))

    async def brief_line(self) -> str:
        """'INBOXES  Personal 4 · Derma Direct 2 · Prodermis 0' for the brief."""
        results = await asyncio.gather(
            *(c.unread(limit=0) for c in self._clients), return_exceptions=True
        )
        parts = []
        for client, result in zip(self._clients, results):
            if isinstance(result, Exception):
                parts.append(f"{client.account.label} ?")
            else:
                parts.append(f"{client.account.label} {result['unread']}")
        return "INBOXES  " + " · ".join(parts) if parts else ""
