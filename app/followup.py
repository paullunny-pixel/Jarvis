"""Follow-up chaser (7 Aug build slice): outbound items awaiting a reply —
ones Paul flags ('chase this') plus important ones auto-detected from the
Sent folder — chased after a tunable threshold. Stops promises and asks
from silently vanishing.

Auto-detection is best-effort and conservative on purpose: a wrong nag
('you never replied' when he did) is worse than a missed one, so an item
only auto-flags once it's genuinely unmatched by any reply — never on a
guess.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.core.store import utc_now_iso
from app.db.base import Database

logger = logging.getLogger(__name__)

DEFAULT_CHASE_DAYS = 3
AUTO_DETECT_LOOKBACK_DAYS = 7


class OutboundWatch:
    def __init__(self, db: Database, mail) -> None:
        self._db = db
        self._mail = mail   # MailService — None-safe callers check first

    async def flag(self, recipient: str, subject: str, note: str = "") -> str:
        recipient = recipient.strip()
        subject = subject.strip()
        if not recipient or not subject:
            return "NOTHING FLAGGED — need both who it went to and what it was about."
        twin = await self._db.fetch_one(
            "SELECT id FROM outbound_watch WHERE recipient = ? AND subject = ? AND status = 'open'",
            (recipient, subject),
        )
        if twin:
            return f"Already chasing '{subject}' with {recipient} — not duplicating it."
        await self._db.execute(
            "INSERT INTO outbound_watch (subject, recipient, note, sent_at, source, status)"
            " VALUES (?, ?, ?, ?, 'paul_flagged', 'open')",
            (subject, recipient, note, utc_now_iso()),
        )
        return f"Chasing '{subject}' with {recipient} — I'll flag it if it goes quiet."

    async def resolve(self, subject_or_recipient: str) -> str:
        term = f"%{subject_or_recipient.strip()}%"
        row = await self._db.fetch_one(
            "SELECT id, subject, recipient FROM outbound_watch"
            " WHERE status = 'open' AND (subject LIKE ? OR recipient LIKE ?)"
            " ORDER BY sent_at DESC LIMIT 1",
            (term, term),
        )
        if row is None:
            return f"Nothing open matching '{subject_or_recipient}'."
        await self._db.execute(
            "UPDATE outbound_watch SET status = 'resolved' WHERE id = ?", (row["id"],)
        )
        return f"'{row['subject']}' with {row['recipient']} — off the chase list."

    async def list_open(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT id, subject, recipient, sent_at, source, last_chased_at"
            " FROM outbound_watch WHERE status = 'open' ORDER BY sent_at"
        )
        return [dict(r) for r in rows]

    async def due_chases(self, today: date, threshold_days: int = DEFAULT_CHASE_DAYS) -> list[dict[str, Any]]:
        """Open items past the threshold with no reply, that haven't been
        chased in at least `threshold_days` — repeats gently, doesn't nag daily."""
        out = []
        for row in await self.list_open():
            try:
                sent = datetime.fromisoformat(row["sent_at"]).date()
            except ValueError:
                continue
            if (today - sent).days < threshold_days:
                continue
            last_chased = row.get("last_chased_at") or ""
            if last_chased:
                try:
                    since = datetime.fromisoformat(last_chased).date()
                    if (today - since).days < threshold_days:
                        continue
                except ValueError:
                    pass
            out.append({**row, "days_waiting": (today - sent).days})
        return out

    async def mark_chased(self, item_id: int) -> None:
        await self._db.execute(
            "UPDATE outbound_watch SET last_chased_at = ? WHERE id = ?", (utc_now_iso(), item_id)
        )

    # ------------------------------------------------ auto-detect sweep
    async def auto_detect_sweep(self) -> int:
        """Best-effort: scan recent Sent items across every connected
        account, flag any with no reply since (auto_detected). Returns how
        many new ones were flagged. Never raises — a failed account is
        skipped, not fatal to the sweep."""
        if self._mail is None:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=AUTO_DETECT_LOOKBACK_DAYS)
        chase_cutoff = datetime.now(timezone.utc) - timedelta(days=DEFAULT_CHASE_DAYS)
        flagged = 0
        for client in getattr(self._mail, "_clients", []):
            try:
                sent = await client.recent_sent(AUTO_DETECT_LOOKBACK_DAYS)
            except Exception:
                logger.exception("Follow-up sweep: recent_sent failed for an account")
                continue
            for item in sent:
                try:
                    sent_at = datetime.fromisoformat(item["sent_at"])
                except (KeyError, ValueError):
                    continue
                if sent_at > chase_cutoff or sent_at < cutoff:
                    continue  # too fresh to chase yet, or outside the lookback window
                twin = await self._db.fetch_one(
                    "SELECT id FROM outbound_watch WHERE recipient = ? AND subject = ?",
                    (item["to_address"], item["subject"]),
                )
                if twin:
                    continue  # already tracked (flagged, resolved, or previously auto-detected)
                try:
                    replied = await client.has_reply_since(item["to_address"], item["sent_at"])
                except Exception:
                    logger.exception("Follow-up sweep: has_reply_since failed")
                    continue
                if replied:
                    continue
                await self._db.execute(
                    "INSERT INTO outbound_watch (subject, recipient, note, sent_at, source, status)"
                    " VALUES (?, ?, '', ?, 'auto_detected', 'open')",
                    (item["subject"], item["to_address"], item["sent_at"]),
                )
                flagged += 1
        return flagged
