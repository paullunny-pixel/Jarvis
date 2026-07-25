"""Repositories over the database: the message log and living settings."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.db.base import Database


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MessageLog:
    """Every message in and out, transcribed and searchable (Milestone 1 'done' bar)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def log(
        self,
        direction: str,
        transcript: str,
        *,
        chat_id: int = 0,
        kind: str = "text",
        voice_duration: int = 0,
        channel: str = "telegram",
        meta: dict[str, Any] | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO messages (ts, direction, channel, chat_id, kind, transcript, voice_duration, meta)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                utc_now_iso(),
                direction,
                channel,
                chat_id,
                kind,
                transcript,
                voice_duration,
                json.dumps(meta or {}),
            ),
        )

    async def recent(self, limit: int = 40) -> list[dict[str, Any]]:
        """Most recent messages, oldest first — ready to feed the brain."""
        rows = await self._db.fetch_all(
            "SELECT ts, direction, kind, transcript FROM messages"
            " WHERE transcript != '' ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return list(reversed(rows))

    async def as_claude_messages(self, limit: int = 40) -> list[dict[str, str]]:
        """Conversation history in Claude message format, merging consecutive
        same-role turns (the API requires alternating roles)."""
        merged: list[dict[str, str]] = []
        for row in await self.recent(limit):
            role = "user" if row["direction"] == "in" else "assistant"
            if merged and merged[-1]["role"] == role:
                merged[-1]["content"] += "\n" + row["transcript"]
            else:
                merged.append({"role": role, "content": row["transcript"]})
        # History must start with a user turn.
        while merged and merged[0]["role"] != "user":
            merged.pop(0)
        return merged


class SettingsStore:
    """Small living key-value store (owner chat lock, current timezone, ...)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, key: str, default: str = "") -> str:
        row = await self._db.fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set(self, key: str, value: str) -> None:
        if self._db.dialect == "postgres":
            await self._db.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                (key, value, utc_now_iso()),
            )
        else:
            await self._db.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, utc_now_iso()),
            )
