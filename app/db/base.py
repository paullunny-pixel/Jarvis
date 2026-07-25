"""Database layer.

One tiny adapter interface, two implementations:
- SQLite  — local dev and the in-session test suite (zero setup).
- Postgres — production on Render (managed Postgres, pgvector from Milestone 2).

All app code writes SQL with `?` placeholders; the Postgres adapter translates
them to `$1..$n`. Timestamps are stored as ISO-8601 UTC strings for full
cross-engine compatibility.
"""
from __future__ import annotations

from typing import Any, Protocol


class Database(Protocol):
    dialect: str

    async def init(self) -> None: ...
    async def close(self) -> None: ...
    async def execute(self, sql: str, params: tuple = ()) -> None: ...
    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]: ...
    async def fetch_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None: ...
    async def insert_returning_id(self, sql: str, params: tuple = ()) -> int:
        """Run an INSERT and return the new row's id, race-free."""
        ...


def get_database(database_url: str, sqlite_path: str = "jarvis.db") -> Database:
    if database_url:
        from .postgres import PostgresDatabase

        return PostgresDatabase(database_url)
    from .sqlite import SqliteDatabase

    return SqliteDatabase(sqlite_path)
