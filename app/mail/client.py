"""IMAP/SMTP access to Paul's Google-hosted inboxes via app passwords —
no OAuth, no consent screens, no 7-day token expiry (the Paul-friendly route
to Plan §9's Gmail integration). One client per account.

Deliberately thin and injectable: tests swap the imap/smtp factories for
fakes, mirroring the httpx MockTransport pattern used by the other clients.
Reads use BODY.PEEK — Jarvis never marks Paul's mail as read.
"""
from __future__ import annotations

import asyncio
import email
import email.header
import email.utils
import imaplib
import logging
import smtplib
from dataclasses import dataclass
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

LABELS = {
    "gmail.com": "Personal",
    "dermadirect.co.uk": "Derma Direct",
    "prodermis.com": "Prodermis",
}


def label_for(address: str) -> str:
    domain = address.rsplit("@", 1)[-1].lower()
    return LABELS.get(domain, domain.split(".")[0].title())


@dataclass
class MailAccount:
    address: str
    app_password: str
    label: str = ""
    imap_host: str = "imap.gmail.com"
    smtp_host: str = "smtp.gmail.com"

    def __post_init__(self) -> None:
        if not self.label:
            self.label = label_for(self.address)


def _decode(value: str | None) -> str:
    out = ""
    for text, enc in email.header.decode_header(value or ""):
        out += text.decode(enc or "utf-8", "replace") if isinstance(text, bytes) else text
    return out.strip()


def _strip_html(html: str) -> str:
    """HTML-only emails (form submissions, newsletters) must never leak raw
    tags into what Jarvis reads out."""
    import html as _html
    import re as _re

    text = _re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", html)
    text = _re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>", "\n", text)
    text = _re.sub(r"<[^>]+>", " ", text)
    return " ".join(_html.unescape(text).split())


def _plaintext(msg: email.message.Message) -> str:
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8", "replace")
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True) or b""
                    return _strip_html(
                        payload.decode(part.get_content_charset() or "utf-8", "replace")
                    )
            return ""
        payload = msg.get_payload(decode=True) or b""
        text = payload.decode(msg.get_content_charset() or "utf-8", "replace")
        if msg.get_content_type() == "text/html" or "<html" in text[:200].lower():
            return _strip_html(text)
        return text
    except Exception:
        return ""


class MailClient:
    """One inbox: unread listing (read-only), full send, live health check."""

    def __init__(self, account: MailAccount, imap_factory=None, smtp_factory=None) -> None:
        self.account = account
        self._imap = imap_factory or (
            lambda: imaplib.IMAP4_SSL(account.imap_host, 993, timeout=30)
        )
        self._smtp = smtp_factory or (
            lambda: smtplib.SMTP_SSL(account.smtp_host, 465, timeout=30)
        )

    async def unread(self, limit: int = 8) -> dict:
        """{'unread': total count, 'messages': newest-first details}."""
        return await asyncio.to_thread(self._unread_sync, limit)

    # Full-body fetches are capped: a newsletter is fine, a 25MB attachment
    # email must never be pulled into RAM just for a snippet (512MB box!).
    FULL_FETCH_CEILING_BYTES = 200_000

    def _message_size(self, imap, msg_id) -> int:
        import re as _re

        try:
            _, meta = imap.fetch(msg_id, "(RFC822.SIZE)")
            blob = b" ".join(
                part if isinstance(part, bytes) else part[0] for part in meta if part
            )
            match = _re.search(rb"RFC822\.SIZE (\d+)", blob)
            return int(match.group(1)) if match else 0
        except Exception:
            return 0

    def _unread_sync(self, limit: int) -> dict:
        with self._imap() as imap:
            imap.login(self.account.address, self.account.app_password)
            imap.select("INBOX", readonly=True)
            _, data = imap.search(None, "UNSEEN")
            ids = data[0].split() if data and data[0] else []
            messages = []
            for msg_id in reversed(ids[-limit:]):
                size = self._message_size(imap, msg_id)
                spec = (
                    "(BODY.PEEK[HEADER])"
                    if size > self.FULL_FETCH_CEILING_BYTES
                    else "(BODY.PEEK[])"
                )
                _, fetched = imap.fetch(msg_id, spec)
                raw = next((part[1] for part in fetched if isinstance(part, tuple)), None)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                from_name, from_addr = email.utils.parseaddr(_decode(msg.get("From")))
                messages.append(
                    {
                        "from": from_name or from_addr,
                        "from_address": from_addr,
                        "subject": _decode(msg.get("Subject")) or "(no subject)",
                        "date": _decode(msg.get("Date")),
                        "snippet": " ".join(_plaintext(msg).split())[:240],
                    }
                )
            return {"unread": len(ids), "messages": messages}

    # Research reads real correspondence, and Paul's business email is heavy
    # with PDFs and images — a 200KB ceiling would blank exactly the emails
    # that matter. Fetch up to 4MB each (one at a time), flag anything bigger.
    RESEARCH_FETCH_CEILING_BYTES = 4_000_000

    async def search_messages(self, query: str, limit: int = 60) -> tuple[list[dict], int]:
        """ALL mail matching a query — read or unread, the whole account (All
        Mail covers Paul's own replies). Gmail's native search (X-GM-RAW) with
        a plain FROM/SUBJECT fallback. Returns (newest `limit` messages, total
        match count) so the caller can be honest about truncation."""
        return await asyncio.to_thread(self._search_sync, query, limit)

    def _search_sync(self, query: str, limit: int) -> tuple[list[dict], int]:
        query = query.replace('"', "").strip()
        if not query:
            return [], 0
        with self._imap() as imap:
            imap.login(self.account.address, self.account.app_password)
            # All Mail covers Paul's own replies too — 'all communication with
            # BMI' means both directions, not just what landed in the inbox.
            for folder in ('"[Gmail]/All Mail"', "INBOX"):
                status, _ = imap.select(folder, readonly=True)
                if status == "OK":
                    break
            ids: list[bytes] = []
            try:
                _, data = imap.search(None, "X-GM-RAW", f'"{query}"')
                ids = data[0].split() if data and data[0] else []
            except Exception:
                ids = []
            if not ids:
                try:
                    _, data = imap.search(None, f'(OR FROM "{query}" SUBJECT "{query}")')
                    ids = data[0].split() if data and data[0] else []
                except Exception:
                    ids = []
            messages = []
            for msg_id in reversed(ids[-limit:]):
                size = self._message_size(imap, msg_id)
                too_big = size > self.RESEARCH_FETCH_CEILING_BYTES
                spec = "(BODY.PEEK[HEADER])" if too_big else "(BODY.PEEK[])"
                _, fetched = imap.fetch(msg_id, spec)
                raw = next((part[1] for part in fetched if isinstance(part, tuple)), None)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                from_name, from_addr = email.utils.parseaddr(_decode(msg.get("From")))
                if too_big:
                    snippet = (
                        "(body not read — message too large to fetch, likely heavy "
                        "attachments; treat as a gap)"
                    )
                else:
                    snippet = " ".join(_plaintext(msg).split())[:3000] or (
                        "(no readable text in this email — treat as a gap)"
                    )
                messages.append(
                    {
                        "from": from_name or from_addr,
                        "from_address": from_addr,
                        "subject": _decode(msg.get("Subject")) or "(no subject)",
                        "date": _decode(msg.get("Date")),
                        "snippet": snippet,
                    }
                )
            return messages, len(ids)

    SENT_FOLDERS = ('"[Gmail]/Sent Mail"', "Sent", '"Sent Items"')

    async def sent_samples(self, limit: int = 15, to_address: str = "") -> list[str]:
        """Bodies of Paul's OWN sent emails — optionally only those addressed
        to one person (per-person style). Size-capped like everything else."""
        return await asyncio.to_thread(self._sent_samples_sync, limit, to_address)

    def _sent_samples_sync(self, limit: int, to_address: str = "") -> list[str]:
        with self._imap() as imap:
            imap.login(self.account.address, self.account.app_password)
            selected = False
            for folder in self.SENT_FOLDERS:
                status, _ = imap.select(folder, readonly=True)
                if status == "OK":
                    selected = True
                    break
            if not selected:
                return []
            criterion = f'TO "{to_address}"' if to_address else "ALL"
            _, data = imap.search(None, criterion)
            ids = data[0].split() if data and data[0] else []
            samples = []
            for msg_id in reversed(ids[-limit:]):
                if self._message_size(imap, msg_id) > self.FULL_FETCH_CEILING_BYTES:
                    continue
                _, fetched = imap.fetch(msg_id, "(BODY.PEEK[])")
                raw = next((part[1] for part in fetched if isinstance(part, tuple)), None)
                if not raw:
                    continue
                text = _plaintext(email.message_from_bytes(raw)).strip()
                if text:
                    samples.append(text[:1500])
            return samples

    async def send(self, to: str, subject: str, body: str) -> None:
        def _send() -> None:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = self.account.address
            msg["To"] = to
            with self._smtp() as smtp:
                smtp.login(self.account.address, self.account.app_password)
                smtp.sendmail(self.account.address, [to], msg.as_string())

        await asyncio.to_thread(_send)

    async def check(self) -> dict:
        """Live connection check for 'status' — login + select, report truth."""
        def _check() -> dict:
            with self._imap() as imap:
                imap.login(self.account.address, self.account.app_password)
                imap.select("INBOX", readonly=True)
            return {"label": self.account.label, "ok": True, "error": ""}

        try:
            return await asyncio.to_thread(_check)
        except Exception as exc:
            return {"label": self.account.label, "ok": False, "error": str(exc)[:120]}
