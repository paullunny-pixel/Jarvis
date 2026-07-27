"""Inbox triage and confirmed sending across all of Paul's accounts.

The safety contract (Paul chose it): Jarvis reads and drafts freely, but
NOTHING is sent until Paul confirms the read-back draft ("send it"). Exactly
one draft is pending at a time; it may sit without a recipient until Paul
supplies one, and once it's older than 45 minutes a "send it" gets the draft
read back once more and needs a second confirm — a forgotten draft can never
fire off a stray email, but it never silently disappears either.
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
        """Store the one pending draft and read it back. A missing recipient is
        fine — the draft is held and the address can arrive later by voice."""
        account_label = ""
        if reply_index:
            listing = await self.last_listing()
            if not (1 <= reply_index <= len(listing)):
                return "I've lost track of which email that was — ask me to read the inbox again first."
            target = listing[reply_index - 1]
            to = to or target["from_address"]
            subject = subject or f"Re: {target['subject']}"
            account_label = target["account"]
        if not body.strip() and await self.pending_draft() is not None:
            # No new words = he's amending the held draft (address, account,
            # subject), not dictating a fresh one — never clobber his words.
            return await self.update_draft(
                to=to, subject=subject, account_hint=account_hint or account_label
            )
        if not body.strip():
            return "Nothing drafted yet — give me the words and I'll read them back."
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
        return self._readback(draft)

    async def update_draft(
        self, to: str = "", subject: str = "", body: str = "", account_hint: str = ""
    ) -> str:
        """Merge changes into the held draft; empty fields keep what's there."""
        draft = await self.pending_draft()
        if draft is None:
            return "There's no draft on the go — dictate one and I'll hold it."
        if to:
            draft["to"] = to
        if subject:
            draft["subject"] = subject
        if body.strip():
            draft["body"] = body.strip()
        if account_hint:
            client = self.match(account_hint)[0]
            draft["from"] = client.account.address
            draft["from_label"] = client.account.label
        draft["created"] = utc_now_iso()
        await self._settings.set(DRAFT_KEY, json.dumps(draft))
        return self._readback(draft)

    def _readback(self, draft: dict) -> str:
        recipient = draft["to"] or "(no recipient yet)"
        text = (
            f"Drafted from {draft['from_label']} ({draft['from']}) to {recipient}\n"
            f"Subject: {draft['subject']}\n\n{draft['body']}\n\n"
        )
        if draft["to"]:
            return text + "Say 'send it' and it goes — or 'scrap it', or tell me what to change."
        return text + (
            "Held in draft, sir — give me the address whenever you're ready, "
            "then 'send it'. 'Scrap it' bins it."
        )

    async def pending_draft(self) -> dict | None:
        raw = await self._settings.get(DRAFT_KEY, "")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    @staticmethod
    def _is_stale(draft: dict) -> bool:
        created = datetime.fromisoformat(draft["created"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - created > timedelta(minutes=DRAFT_TTL_MINUTES)

    async def send_pending(self) -> str:
        draft = await self.pending_draft()
        if draft is None:
            return "There's no draft waiting to send — dictate one and I'll read it back first."
        if not draft["to"]:
            return (
                "It's drafted but has no recipient yet — give me the address "
                "and then say 'send it'."
            )
        if self._is_stale(draft):
            # It's been sitting a while: fresh eyes once more before it flies.
            draft["created"] = utc_now_iso()
            await self._settings.set(DRAFT_KEY, json.dumps(draft))
            return (
                "That draft's been sitting a while, so once more before it goes:\n\n"
                + self._readback(draft)
            )
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

    # -------------------------------------------------- Paul's writing voice

    async def style_profile(self) -> str:
        from app.mail.style import STYLE_KEY

        return await self._settings.get(STYLE_KEY, "")

    async def learn_style(self, claude) -> str:
        """Sample every Sent folder, distill Paul's voice, store the guide."""
        from app.mail.style import STYLE_KEY, build_style_profile

        results = await asyncio.gather(
            *(c.sent_samples(limit=15) for c in self._clients), return_exceptions=True
        )
        samples = {
            c.account.label: r
            for c, r in zip(self._clients, results)
            if not isinstance(r, Exception) and r
        }
        if not samples:
            return (
                "I couldn't read any sent mail to learn from — the Sent folders "
                "came back empty. Send a few emails and try 'learn my email style' again."
            )
        profile = await build_style_profile(claude, samples)
        if not profile:
            return "The style study didn't take — try 'learn my email style' again shortly."
        await self._settings.set(STYLE_KEY, profile)
        count = sum(len(v) for v in samples.values())
        return (
            f"Studied {count} of your sent emails across {len(samples)} account(s) — "
            "I've got your voice now, sir. Every draft from here on is written as you. "
            "Run 'learn my email style' again any time to refresh it."
        )

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
