"""Integration-tier fixtures (docs/DESIGN-0.2.md A.2): real PostgreSQL (database `daash_test`)
and real Redis (DB 15); the API runs in-process over ASGI; the fake LLM is in-process.

Run with `docker compose --profile integration run --rm --build integration`.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.bus import DecisionBus
from app.config import Settings
from app.db import create_pool
from tests.fakes.fake_llm import FakeLLM
from tests.integration.helpers import FakeClock, Harness, make_llm

TEST_DB_URL = os.environ.get("DAASH_TEST_DATABASE_URL",
                             "postgresql://daash:daash@db:5432/daash_test")
TEST_REDIS_URL = os.environ.get("DAASH_TEST_REDIS_URL", "redis://redis:6379/15")
REPO_ROOT = Path(__file__).resolve().parents[2]


def with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/" + name))


def db_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


async def create_database_if_missing(url: str) -> None:
    admin = await asyncpg.connect(with_database(url, "postgres"))
    try:
        name = db_name(url)
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()


async def reset_schema(url: str) -> None:
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()


def run_alembic(db_url: str, target: str = "head") -> None:
    """`alembic upgrade <target>` against `db_url` in a subprocess (like the entrypoint)."""
    env = {**os.environ, "DAASH_ALEMBIC_DATABASE_URL": db_url}
    res = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), "upgrade", target],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"alembic upgrade {target} failed:\n{res.stdout}\n{res.stderr}")


@pytest.fixture(scope="session")
def database() -> str:
    """Create `daash_test` if missing, wipe its schema and migrate to head, once per session."""
    asyncio.run(create_database_if_missing(TEST_DB_URL))
    asyncio.run(reset_schema(TEST_DB_URL))
    run_alembic(TEST_DB_URL)
    return TEST_DB_URL


async def truncate_all(pool: asyncpg.Pool) -> None:
    rows = await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                            "AND tablename <> 'alembic_version'")
    if rows:
        await pool.execute("TRUNCATE " + ", ".join(f'"{r["tablename"]}"' for r in rows)
                           + " CASCADE")


@pytest_asyncio.fixture
async def pool(database):
    p = await create_pool(database)
    await truncate_all(p)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def bus():
    r = aioredis.from_url(TEST_REDIS_URL)
    await r.flushdb()
    b = DecisionBus(TEST_REDIS_URL, 3600, 200)
    yield b
    await b.close()
    await r.flushdb()
    await r.aclose()


@pytest.fixture
def fake() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(DATABASE_URL=TEST_DB_URL, REDIS_URL=TEST_REDIS_URL,
                    WORKSPACE_ROOT=str(tmp_path / "workspaces"), EMBEDDING_USE_MODEL=False,
                    LLM_ENDPOINT="http://fake-llm:9100", ENGINE_TICK_SECONDS=0.01)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest_asyncio.fixture
async def h(pool, bus, fake, settings, clock):
    """The harness: DB, bus, fake LLM and an in-process Engine wired to them."""
    harness = Harness(pool, bus, fake, settings, clock)
    yield harness
    await harness.close()


@pytest_asyncio.fixture
async def api(pool, bus, fake, settings, monkeypatch):
    """The real FastAPI app over ASGI (no lifespan), with its globals pointed at the test
    resources. The LLM is the fake."""
    from app import main

    monkeypatch.setattr(main, "pool", pool)
    monkeypatch.setattr(main, "bus", bus)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "llm", make_llm(fake, settings.LLM_ENDPOINT))
    monkeypatch.setattr(main, "embedder", main.build_embedder("none", False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                 base_url="http://test") as client:
        yield client
