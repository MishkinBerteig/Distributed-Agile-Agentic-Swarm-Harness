from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import AsyncGenerator

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.bus import DecisionBus
from app.config import Settings
from app.db import Database
from app.repo import TaskRepository

TEST_DB_URL = "postgresql://daash:daash@localhost/daash_test"
TEST_REDIS_URL = os.environ.get("DAASH_TEST_REDIS_URL", "redis://localhost:6379/15")
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def migrated_schema() -> None:
    """Build the test schema with `alembic upgrade head` so tests exercise the migration."""
    subprocess.run(
        ["psql", TEST_DB_URL, "-q", "-c", "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"],
        check=True,
    )
    env = {**os.environ, "DAASH_ALEMBIC_DATABASE_URL": "postgresql+psycopg://daash:daash@localhost/daash_test"}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, check=True, env=env, capture_output=True,
    )


@pytest_asyncio.fixture
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    p = await asyncpg.create_pool(TEST_DB_URL)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def task_repo(pool: asyncpg.Pool) -> AsyncGenerator[TaskRepository, None]:
    # Clean data between repo tests; schema comes from the session migration.
    await pool.execute("TRUNCATE transcripts, memory_entries, tasks, teams CASCADE")
    repo = TaskRepository(pool)
    yield repo


@pytest_asyncio.fixture
async def test_db(pool: asyncpg.Pool) -> AsyncGenerator[Database, None]:
    settings = Settings(DATABASE_URL=TEST_DB_URL)
    db = Database(settings)
    # Monkey-patch the pool onto the DB instance
    db._pool = pool
    yield db


@pytest_asyncio.fixture
async def bus() -> AsyncGenerator[DecisionBus, None]:
    """Decision bus on a dedicated Redis DB (15), flushed before/after use."""
    import redis.asyncio as aioredis

    admin = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
    await admin.flushdb()
    b = DecisionBus(TEST_REDIS_URL, ttl_seconds=60)
    yield b
    await admin.flushdb()
    await admin.aclose()
    await b.close()


@pytest_asyncio.fixture
async def client(test_db: Database, bus: DecisionBus) -> AsyncGenerator[AsyncClient, None]:
    import app.main as main_module
    main_module.db = test_db
    original_bus = main_module.bus
    main_module.bus = bus

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    main_module.bus = original_bus
