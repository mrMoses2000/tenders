from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg


async def _init_connection(connection: asyncpg.Connection) -> None:
    for type_name in ("json", "jsonb"):
        await connection.set_type_codec(
            type_name,
            schema="pg_catalog",
            encoder=lambda value: json.dumps(value, ensure_ascii=False),
            decoder=json.loads,
            format="text",
        )


async def create_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
    command_timeout: float = 60,
) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=command_timeout,
        init=_init_connection,
    )


@asynccontextmanager
async def transaction(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    async with pool.acquire() as connection, connection.transaction():
        yield connection


async def run_migrations(pool: asyncpg.Pool, migrations_dir: Path) -> list[str]:
    """Apply immutable numbered migrations under a process-safe advisory lock."""
    files = sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))
    applied: list[str] = []
    async with pool.acquire() as connection:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await connection.execute("SELECT pg_advisory_lock($1)", 718_639_041)
        try:
            for path in files:
                source = path.read_text(encoding="utf-8")
                digest = hashlib.sha256(source.encode()).hexdigest()
                existing = await connection.fetchrow(
                    "SELECT sha256 FROM schema_migrations WHERE name=$1", path.name
                )
                if existing:
                    if existing["sha256"] != digest:
                        raise RuntimeError(f"Applied migration changed on disk: {path.name}")
                    continue
                async with connection.transaction():
                    await connection.execute(source)
                    await connection.execute(
                        "INSERT INTO schema_migrations(name,sha256) VALUES ($1,$2)",
                        path.name,
                        digest,
                    )
                applied.append(path.name)
        finally:
            await connection.execute("SELECT pg_advisory_unlock($1)", 718_639_041)
    return applied


def row_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
