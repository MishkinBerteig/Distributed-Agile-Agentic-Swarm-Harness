from __future__ import annotations

from typing import AsyncGenerator

import asyncpg
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.db import Database
from app.repo import TaskRepository

TEST_DB_URL = "postgresql://daash:daash@localhost/daash_test"


@pytest_asyncio.fixture
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    p = await asyncpg.create_pool(TEST_DB_URL)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def task_repo(pool: asyncpg.Pool) -> AsyncGenerator[TaskRepository, None]:
    # Clean slate and ensure schema matches the latest models (name instead of title)
    await pool.execute("DROP TABLE IF EXISTS transcripts CASCADE")
    await pool.execute("DROP TABLE IF EXISTS memory_entries CASCADE")
    await pool.execute("DROP TABLE IF EXISTS tasks CASCADE")
    await pool.execute("DROP TABLE IF EXISTS teams CASCADE")
    await pool.execute("""
        CREATE TABLE teams (
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            vision_statement TEXT NOT NULL,
            mission_statement TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL REFERENCES teams(id),
            parent_id TEXT REFERENCES tasks(id),
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            acceptance_criteria TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Pending',
            rejection_reason TEXT,
            keywords TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
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
async def client(test_db: Database) -> AsyncGenerator[AsyncClient, None]:
    import app.main as main_module
    main_module.db = test_db

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
