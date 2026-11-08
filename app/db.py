from __future__ import annotations

import json

import asyncpg
from app.config import Settings


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Make json/jsonb round-trip as Python objects on every pooled connection."""
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(
            typ, encoder=lambda v: json.dumps(v, default=str), decoder=json.loads,
            schema="pg_catalog",
        )


async def create_pool(dsn: str, **kwargs) -> asyncpg.Pool:
    """Create an asyncpg pool whose connections decode jsonb into dict/list."""
    return await asyncpg.create_pool(dsn, init=_init_connection, **kwargs)


class Database:
    def __init__(self, settings: Settings):
        self._pool: asyncpg.Pool | None = None
        self._settings = settings

    async def connect(self) -> None:
        # Ensure pgvector extension exists before pool creation (it requires a dedicated connection)
        await self._ensure_vector_extension()
        self._pool = await create_pool(str(self._settings.DATABASE_URL))

    async def disconnect(self) -> None:
        if self._pool:
            await self._pool.close()

    @property
    def pool(self) -> asyncpg.Pool:
        assert self._pool is not None
        return self._pool

    async def _ensure_vector_extension(self) -> None:
        """Create pgvector extension if it doesn't exist.

        Uses a standalone connection rather than the pool because
        CREATE EXTENSION cannot run inside a transaction, and Alembic
        manages the rest of the schema via migrations.
        """
        conn = await asyncpg.connect(str(self._settings.DATABASE_URL))
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        finally:
            await conn.close()
