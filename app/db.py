from __future__ import annotations

import asyncio
import asyncpg
from app.config import Settings


class Database:
    def __init__(self, settings: Settings):
        self._pool: asyncpg.Pool | None = None
        self._settings = settings

    async def connect(self) -> None:
        # Ensure pgvector extension exists before pool creation (it requires a dedicated connection)
        await self._ensure_vector_extension()
        self._pool = await asyncpg.create_pool(str(self._settings.DATABASE_URL))

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
