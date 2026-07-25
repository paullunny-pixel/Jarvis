"""The second brain's storage: memory chunks (semantic recall) and living
facts (updated in place, history kept). Design per docs/Jarvis-Data-Model.md.

Rooms: you · companies · health · finances · people · private
Types: STABLE · LIVING · PRIVATE

Isolation rule (hard, enforced here): chunks in the private room / PRIVATE
type are NEVER returned unless the caller explicitly asks for the private
scope — business retrieval simply cannot see them. Private content is
encrypted at rest via PrivateBox.
"""
from __future__ import annotations

import json
from typing import Any

from app.core.store import utc_now_iso
from app.db.base import Database
from app.memory.crypto import PrivateBox
from app.memory.embedder import Embedder, cosine

ROOMS = {"you", "companies", "health", "finances", "people", "private"}
TYPES = {"STABLE", "LIVING", "PRIVATE"}


def _is_private(room: str, type_: str) -> bool:
    return room == "private" or type_ == "PRIVATE"


class MemoryStore:
    def __init__(self, db: Database, embedder: Embedder, private_box: PrivateBox | None = None) -> None:
        self._db = db
        self._embedder = embedder
        self._box = private_box or PrivateBox("")

    # --- Writing ---

    async def add_chunk(
        self,
        content: str,
        *,
        room: str,
        type_: str = "STABLE",
        source: str = "conversation",
        tags: list[str] | None = None,
        document_id: int | None = None,
    ) -> int:
        room = room if room in ROOMS else "you"
        type_ = type_ if type_ in TYPES else "STABLE"
        private = _is_private(room, type_)
        [vector] = await self._embedder.embed([content], kind="document")
        stored_content = self._box.seal(content) if private else content
        return await self._db.insert_returning_id(
            "INSERT INTO memory_chunks (content, embedding, room, type, source, tags, document_id,"
            " is_private, created_at, superseded_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                stored_content,
                json.dumps(vector) if self._db.dialect == "sqlite" else _pgvector(vector),
                room,
                type_,
                source,
                json.dumps(tags or []),
                document_id or 0,
                1 if private else 0,
                utc_now_iso(),
            ),
        )

    async def supersede(self, old_id: int, new_content: str, **kwargs: Any) -> int:
        """Replace a fact: old row is kept but marked superseded (history),
        the new row becomes current."""
        old = await self._db.fetch_one(
            "SELECT room, type, tags FROM memory_chunks WHERE id = ?", (old_id,)
        )
        if old:
            kwargs.setdefault("room", old["room"])
            kwargs.setdefault("type_", old["type"])
            kwargs.setdefault("tags", json.loads(old["tags"]))
        new_id = await self.add_chunk(new_content, **kwargs)
        await self._db.execute(
            "UPDATE memory_chunks SET superseded_by = ? WHERE id = ?", (new_id, old_id)
        )
        return new_id

    # --- Retrieval ---

    async def search(
        self,
        query: str,
        *,
        k: int = 8,
        rooms: list[str] | None = None,
        include_private: bool = False,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Semantic search over current (non-superseded) chunks.

        THE WALL: private chunks are excluded unless include_private=True.
        Callers in business context must never pass include_private."""
        [query_vec] = await self._embedder.embed([query], kind="query")
        room_filter = [r for r in (rooms or []) if r in ROOMS]
        if not include_private and "private" in room_filter:
            room_filter.remove("private")

        if self._db.dialect == "postgres":
            sql = (
                "SELECT id, content, room, type, source, tags, document_id, is_private,"
                " 1 - (embedding <=> ?::vector) AS score"
                " FROM memory_chunks WHERE superseded_by = 0"
            )
            params: list[Any] = [_pgvector(query_vec)]
            if not include_private:
                sql += " AND is_private = 0"
            if room_filter:
                placeholders = ", ".join("?" for _ in room_filter)
                sql += f" AND room IN ({placeholders})"
                params.extend(room_filter)
            sql += " ORDER BY embedding <=> ?::vector LIMIT ?"
            params.extend([_pgvector(query_vec), k])
            rows = await self._db.fetch_all(sql, tuple(params))
        else:
            sql = (
                "SELECT id, content, embedding, room, type, source, tags, document_id, is_private"
                " FROM memory_chunks WHERE superseded_by = 0"
            )
            params = []
            if not include_private:
                sql += " AND is_private = 0"
            if room_filter:
                placeholders = ", ".join("?" for _ in room_filter)
                sql += f" AND room IN ({placeholders})"
                params.extend(room_filter)
            rows = await self._db.fetch_all(sql, tuple(params))
            for row in rows:
                row["score"] = cosine(query_vec, json.loads(row.pop("embedding")))
            rows.sort(key=lambda r: r["score"], reverse=True)
            rows = rows[:k]

        results = []
        for row in rows:
            if row["score"] < min_score:
                continue
            content = row["content"]
            if row["is_private"]:
                content = self._box.open(content)
            results.append({**row, "content": content, "tags": json.loads(row["tags"])})
        return results

    async def audit(self, room: str | None = None, include_private: bool = False) -> list[dict[str, Any]]:
        """Everything currently known (for 'what do you know about X?' flows)."""
        sql = "SELECT id, content, room, type, source, is_private FROM memory_chunks WHERE superseded_by = 0"
        params: list[Any] = []
        if not include_private:
            sql += " AND is_private = 0"
        if room in ROOMS:
            sql += " AND room = ?"
            params.append(room)
        rows = await self._db.fetch_all(sql + " ORDER BY id", tuple(params))
        for row in rows:
            if row["is_private"]:
                row["content"] = self._box.open(row["content"])
        return rows


class LivingFacts:
    """LIVING records: one current value per key, updated in place, history
    kept. Keys are dotted paths, e.g. 'health.weight_kg', 'villa.next_demand'."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def set(self, key: str, value: str, *, room: str = "you") -> None:
        existing = await self._db.fetch_one(
            "SELECT value FROM living_facts WHERE key = ?", (key,)
        )
        now = utc_now_iso()
        if existing is not None:
            await self._db.execute(
                "INSERT INTO living_facts_history (key, value, replaced_at) VALUES (?, ?, ?)",
                (key, existing["value"], now),
            )
            await self._db.execute(
                "UPDATE living_facts SET value = ?, room = ?, updated_at = ? WHERE key = ?",
                (value, room, now, key),
            )
        else:
            await self._db.execute(
                "INSERT INTO living_facts (key, value, room, updated_at) VALUES (?, ?, ?, ?)",
                (key, value, room, now),
            )

    async def get(self, key: str) -> str | None:
        row = await self._db.fetch_one("SELECT value FROM living_facts WHERE key = ?", (key,))
        return row["value"] if row else None

    async def all_current(self, *, exclude_private: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT key, value, room, updated_at FROM living_facts"
        if exclude_private:
            sql += " WHERE room != 'private'"
        return await self._db.fetch_all(sql + " ORDER BY key")

    async def history(self, key: str) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT value, replaced_at FROM living_facts_history WHERE key = ? ORDER BY replaced_at",
            (key,),
        )


def _pgvector(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
