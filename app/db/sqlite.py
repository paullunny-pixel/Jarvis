"""SQLite adapter — local dev + tests. Sync sqlite3 driven through a thread
executor behind an asyncio lock (plenty for a single-user assistant)."""
from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from .schema import ddl_statements, migration_statements


class SqliteDatabase:
    dialect = "sqlite"

    def __init__(self, path: str = "jarvis.db") -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            for stmt in ddl_statements("sqlite"):
                conn.execute(stmt)
            for stmt in migration_statements("sqlite"):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
            conn.commit()
            return conn

        self._conn = await asyncio.to_thread(_open)

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    async def execute(self, sql: str, params: tuple = ()) -> None:
        async with self._lock:
            def _run() -> None:
                assert self._conn is not None
                self._conn.execute(sql, params)
                self._conn.commit()

            await asyncio.to_thread(_run)

    async def insert_returning_id(self, sql: str, params: tuple = ()) -> int:
        async with self._lock:
            def _run() -> int:
                assert self._conn is not None
                cursor = self._conn.execute(sql, params)
                self._conn.commit()
                return int(cursor.lastrowid or 0)

            return await asyncio.to_thread(_run)

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        async with self._lock:
            def _run() -> list[dict[str, Any]]:
                assert self._conn is not None
                cursor = self._conn.execute(sql, params)
                return [dict(row) for row in cursor.fetchall()]

            return await asyncio.to_thread(_run)

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None
