"""Desktop notifications (Mac app v2, §2): every proactive send that reaches
Telegram also lands here, so the Mac app — when it's the surface Paul is
actually looking at — can show a native banner and, for the ones judged
worth speaking aloud, a spoken announcement.

The announce/silent split rides the SAME distinction HeartbeatJobs already
makes: a voice send (`_send_voice`) means Jarvis decided this was worth
saying out loud on Telegram, so it's worth saying out loud on the desktop
too; a text send stays a silent banner. No new judgement call, no
duplicated logic — one signal, two surfaces.
"""
from __future__ import annotations

from app.core.store import utc_now_iso
from app.db.base import Database


class DesktopNotifications:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, text: str, *, kind: str = "text", announce: bool = False) -> None:
        await self._db.execute(
            "INSERT INTO desktop_notifications (text, kind, announce, created_at)"
            " VALUES (?, ?, ?, ?)",
            (text, kind, 1 if announce else 0, utc_now_iso()),
        )

    async def recent(self, limit: int = 20, *, include_dismissed: bool = False) -> list[dict]:
        where = "" if include_dismissed else "WHERE dismissed = 0"
        rows = await self._db.fetch_all(
            f"SELECT id, text, kind, announce, created_at, dismissed FROM desktop_notifications"
            f" {where} ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "id": r["id"],
                "text": r["text"],
                "kind": r["kind"],
                "announce": bool(r["announce"]),
                "created_at": r["created_at"],
                "dismissed": bool(r["dismissed"]),
            }
            for r in rows
        ]

    async def uncleared_count(self) -> int:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS n FROM desktop_notifications WHERE dismissed = 0"
        )
        return int(row["n"]) if row else 0

    async def dismiss(self, notification_id: int) -> bool:
        await self._db.execute(
            "UPDATE desktop_notifications SET dismissed = 1, dismissed_at = ? WHERE id = ?",
            (utc_now_iso(), notification_id),
        )
        row = await self._db.fetch_one(
            "SELECT id FROM desktop_notifications WHERE id = ? AND dismissed = 1",
            (notification_id,),
        )
        return row is not None
