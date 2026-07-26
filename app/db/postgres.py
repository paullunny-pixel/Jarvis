"""Postgres adapter — production on Render. Uses asyncpg; translates the app's
`?` placeholders to asyncpg's `$1..$n`."""
from __future__ import annotations

from typing import Any

from .schema import ddl_statements, migration_statements


def translate_placeholders(sql: str) -> str:
    """Convert `?` placeholders to `$1..$n` (ignores `?` inside string literals,
    which our SQL never uses anyway)."""
    parts = sql.split("?")
    out = parts[0]
    for i, part in enumerate(parts[1:], start=1):
        out += f"${i}" + part
    return out


class PostgresDatabase:
    dialect = "postgres"

    def __init__(self, database_url: str) -> None:
        # Render provides postgres:// URLs; asyncpg accepts them directly.
        self._url = database_url
        self._pool: Any = None

    async def init(self) -> None:
        import asyncpg  # imported lazily so dev/test environments don't need it

        self._pool = await asyncpg.create_pool(self._url, min_size=1, max_size=5)
        async with self._pool.acquire() as conn:
            for stmt in ddl_statements("postgres"):
                await conn.execute(stmt)
            for stmt in migration_statements("postgres"):
                await conn.execute(stmt)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def execute(self, sql: str, params: tuple = ()) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(translate_placeholders(sql), *params)

    async def insert_returning_id(self, sql: str, params: tuple = ()) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(translate_placeholders(sql) + " RETURNING id", *params)
            return int(row["id"])

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(translate_placeholders(sql), *params)
            return [dict(row) for row in rows]

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None
