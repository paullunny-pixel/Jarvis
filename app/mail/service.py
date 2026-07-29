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
RESEARCH_KEY = "last_email_research"
DRAFT_TTL_MINUTES = 45

# A draft must be finished words, never a promise of words — '[SUMMARY TO
# FOLLOW]' presented as a deliverable is a lie with a signature on it.
PLACEHOLDER = re.compile(
    r"\[[^\]\n]{0,80}(to follow|tbc|tbd|to come|placeholder|insert|awaiting|pending)"
    r"[^\]\n]{0,80}\]",
    re.IGNORECASE,
)


class MailService:
    def __init__(self, clients: list[MailClient], db: Database) -> None:
        self._clients = clients
        self._db = db
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
        if PLACEHOLDER.search(body):
            return (
                "I stopped that draft — it had a placeholder where the substance "
                "should be, and I don't hand you hollow emails. Ask me to do the "
                "reading first ('summarise everything from X about Y'), review "
                "what I find, then we'll draft it properly."
            )
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
            if PLACEHOLDER.search(body):
                return (
                    "Not putting a placeholder into the draft — the words have to "
                    "be real before I'll hold them. Give me the substance (or ask "
                    "me to research the emails first) and I'll drop it in."
                )
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

    # ---------------------------------------------------- research (real work)

    RESEARCH_LIMIT = 60  # newest matches read per search term, per account

    async def research(
        self, claude, query: str, instruction: str, account_hint: str = ""
    ) -> str:
        """'Read all my emails from BMI and summarise where we're up to' —
        search the WHOLE mailbox (read and unread), then produce exactly what
        Paul asked for from what was actually found. Multiple comma-separated
        terms each get their own search (BMI-only queries missed the EUDAMED
        threads that never say 'BMI'); any truncation is declared, never
        silent. The output is remembered so 'draft that to Sarah' has real
        substance behind it."""
        clients = self.match(account_hint)
        terms = [t.strip() for t in re.split(r"[,;/]", query) if t.strip()][:5] or [query]
        limit = self.RESEARCH_LIMIT
        if re.search(
            r"\b(deeper|everything|entire|full history|all of it|the lot)\b",
            f"{query} {instruction}", re.IGNORECASE,
        ):
            limit = 200  # 'go deeper' widens the read, it isn't a platitude
        seen: set = set()
        found: list[tuple[str, dict]] = []
        term_notes, truncated = [], False
        for term in terms:
            results = await asyncio.gather(
                *(c.search_messages(term, limit=limit) for c in clients),
                return_exceptions=True,
            )
            fresh, matches = 0, 0
            for client, result in zip(clients, results):
                if isinstance(result, Exception):
                    logger.warning("Search failed for %s: %s", client.account.label, result)
                    continue
                messages, total = result
                matches += total
                if total > len(messages):
                    truncated = True
                for m in messages:
                    key = (m["from_address"], m["subject"], m["date"])
                    if key in seen:
                        continue
                    seen.add(key)
                    fresh += 1
                    found.append((client.account.label, m))
            note = f"'{term}': {matches} matches"
            if matches > fresh:
                note += f", {fresh} new"
            term_notes.append(note)
        searched = ", ".join(c.account.label for c in clients)
        if not found:
            return (
                f"I searched {searched} end to end for {query!r} and found nothing. "
                "Different search words might do it — what would the sender's "
                "address or a subject line contain?"
            )

        def _stamp(entry) -> float:
            try:
                from email.utils import parsedate_to_datetime

                return parsedate_to_datetime(entry[1]["date"]).timestamp()
            except Exception:
                return 0.0

        found.sort(key=_stamp)  # chronological — 'where are we up to' reads forward
        parts = [
            f"[{label}] {m['date']} — {m['from']} ({m['from_address']}) — "
            f"{m['subject']}\n{m['snippet']}"
            for label, m in found
        ]
        corpus = "\n\n---\n\n".join(parts)[:100000]
        system = (
            "You are Jarvis, Paul's aide. Below are real emails found by searching "
            "his mailbox, oldest first. Produce EXACTLY what Paul asked for:\n"
            f"{instruction or 'a tight summary of these emails'}\n\n"
            "Work ONLY from these emails. Where they don't answer something, say "
            "so plainly — never invent, pad, or promise content 'to follow'. Some "
            "may be marked '(body not read ...)': list those as gaps at the end "
            "rather than guessing their contents. British, structured, ready to "
            "forward."
        )
        try:
            summary = (
                await claude.converse(
                    system, [{"role": "user", "content": corpus}], max_tokens=2500
                )
            ).strip()
        except Exception:
            logger.exception("Research summarisation failed")
            summary = ""
        if not summary:
            return (
                f"Found {len(found)} matching emails on {searched} but the write-up "
                "failed mid-flight — ask me again in a minute."
            )
        header = f"Read {len(found)} emails across {searched} ({' · '.join(term_notes)})."
        if truncated:
            header += (
                " That's the newest slice of a bigger archive — older matches "
                "weren't read; say 'go deeper on <term>' for another pass."
            )
        out = f"{header}\n\n{summary}"
        await self._settings.set(RESEARCH_KEY, out[:12000])
        return out

    async def last_research(self) -> str:
        return await self._settings.get(RESEARCH_KEY, "")

    async def cancel_draft(self) -> str:
        if await self.pending_draft() is None:
            return "Nothing pending to scrap."
        await self._settings.set(DRAFT_KEY, "")
        return "Scrapped. Nothing was sent."

    # -------------------------------------------------------------- status

    async def health(self) -> list[dict]:
        return list(await asyncio.gather(*(c.check() for c in self._clients)))

    # ------------------------------------- per-person voices (Kiefer ≠ everyone)

    CONTACTS_KEY = "style_contacts"

    async def contacts(self) -> list[dict]:
        try:
            return json.loads(await self._settings.get(self.CONTACTS_KEY, "[]"))
        except Exception:
            return []

    async def add_contact(self, name: str, phone: str = "", email: str = "", note: str = "") -> str:
        name = name.strip().title()
        if not name:
            return "Give me at least a name, sir."
        people = [c for c in await self.contacts() if c["name"].lower() != name.lower()]
        people.append({"name": name, "phone": phone.strip(), "email": email.strip().lower(),
                       "note": note.strip()})
        await self._settings.set(self.CONTACTS_KEY, json.dumps(people))
        bits = " · ".join(b for b in (phone, email, note) if b)
        return (
            f"{name} registered as a style contact{(' (' + bits + ')') if bits else ''}. "
            f"Now either 'learn my {name} style' (I'll study emails you sent them) or "
            f"'teach {name} style: <paste how you actually write to them>'."
        )

    def _contact(self, people: list[dict], name: str) -> dict | None:
        name = name.strip().lower()
        return next((c for c in people if c["name"].lower() == name), None)

    async def add_person_sample(self, name: str, text: str) -> str:
        person = self._contact(await self.contacts(), name)
        if person is None:
            return f"I don't have {name.title()} as a style contact yet — 'add style contact {name.title()}' first."
        key = f"style_samples:{person['name'].lower()}"
        try:
            samples = json.loads(await self._settings.get(key, "[]"))
        except Exception:
            samples = []
        samples.append(text.strip())
        await self._settings.set(key, json.dumps(samples[-20:]))
        return (
            f"Noted — that's {len(samples)} example(s) of your {person['name']} voice. "
            f"Say 'learn my {person['name']} style' when you've fed me enough."
        )

    async def learn_person_style(self, claude, name: str) -> str:
        from app.mail.style import build_person_profile

        person = self._contact(await self.contacts(), name)
        if person is None:
            return (
                f"I don't know {name.title()} yet — say 'add style contact {name.title()}, "
                "<mobile>, <email>' first and I'll map them."
            )
        key = person["name"].lower()
        try:
            samples = json.loads(await self._settings.get(f"style_samples:{key}", "[]"))
        except Exception:
            samples = []
        if person.get("email"):
            results = await asyncio.gather(
                *(c.sent_samples(limit=10, to_address=person["email"]) for c in self._clients),
                return_exceptions=True,
            )
            for r in results:
                if not isinstance(r, Exception):
                    samples.extend(r)
        if not samples:
            return (
                f"Nothing of yours addressed to {person['name']} yet. Fastest fix: "
                f"'teach {person['name']} style: <paste a message you really sent them>'."
            )
        guide = await build_person_profile(claude, person["name"], samples, person.get("note", ""))
        if not guide:
            return "That study didn't take — try again shortly."
        await self._settings.set(f"style_person:{key}", guide)
        return (
            f"Got it — your {person['name']} voice is learned from {len(samples)} message(s) "
            "and kept separate from generic-you. Drafts to them switch over automatically."
        )

    async def person_styles(self) -> dict[str, dict]:
        """name → {email, phone, guide} for everyone with a learned voice."""
        result = {}
        for person in await self.contacts():
            guide = await self._settings.get(f"style_person:{person['name'].lower()}", "")
            if guide:
                result[person["name"]] = {
                    "email": person.get("email", ""), "phone": person.get("phone", ""),
                    "guide": guide,
                }
        return result

    # -------------------------------------------------- Paul's writing voice

    async def style_profile(self) -> str:
        from app.mail.style import STYLE_KEY

        return await self._settings.get(STYLE_KEY, "")

    async def learn_style(self, claude) -> str:
        """Distill Paul's REAL voice: his Telegram voice-note transcripts and
        typed messages, plus WhatsApp messages he wrote himself. Emails are
        deliberately not used — many were AI-drafted and don't sound like him."""
        from app.mail.style import STYLE_KEY, build_style_profile

        samples: dict[str, list[str]] = {}
        rows = await self._db.fetch_all(
            "SELECT transcript, kind FROM messages WHERE direction = 'in'"
            " AND transcript NOT LIKE '[%' ORDER BY id DESC LIMIT 400"
        )
        spoken = [
            r["transcript"] for r in rows
            if r["kind"] == "voice" and len(r["transcript"]) > 40
        ][:60]
        typed = [
            r["transcript"] for r in rows
            if r["kind"] != "voice" and len(r["transcript"]) > 40
        ][:40]
        if spoken:
            samples["voice notes (Paul speaking, transcribed)"] = spoken
        if typed:
            samples["typed Telegram messages"] = typed
        # Paul's own messages in the work groups — attributed by his Telegram
        # user id (exact), with a name match as fallback.
        owner = await self._settings.get("owner_chat_id", "0")
        group_rows = await self._db.fetch_all(
            "SELECT message FROM telegram_ingest WHERE kind IN ('text', 'voice')"
            " AND (sender_id = ? OR LOWER(sender) LIKE 'paul%')"
            " AND LENGTH(message) > 30 ORDER BY id DESC LIMIT 60",
            (int(owner or 0),),
        )
        if group_rows:
            samples["work-group messages Paul wrote"] = [r["message"] for r in group_rows]
        if not samples:
            return (
                "I've not got enough of your own words yet to learn from — a few days "
                "of voice notes will do it, then say 'learn my style' again."
            )
        profile = await build_style_profile(claude, samples)
        if not profile:
            return "The style study didn't take — try 'learn my style' again shortly."
        await self._settings.set(STYLE_KEY, profile)
        count = sum(len(v) for v in samples.values())
        sources = ", ".join(samples.keys())
        return (
            f"Studied {count} of your own messages ({sources}) — that's the real you, "
            "not the AI-polished email version. Every draft from here on is written in "
            "your voice. Say 'learn my style' again any time to refresh it."
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
