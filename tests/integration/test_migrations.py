"""Alembic migrations: from an empty database and from the pre-0.1 head (docs/DESIGN-0.2.md A.2)."""
from __future__ import annotations

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.integration.conftest import (REPO_ROOT, TEST_DB_URL, create_database_if_missing,
                                        db_name, reset_schema, run_alembic, with_database)

PRE_01_HEAD = "d3c8b4e2a1f6"
OLD_TABLES = {"agent_sessions", "memory_entries", "transcripts"}
NEW_TABLES = {"global_config", "swarms", "teams", "agents", "tasks", "acceptance_criteria",
              "verdicts", "task_events", "agent_calls", "learnings", "memories",
              "team_proposals", "feedback", "alembic_version"}


def script_head() -> str:
    return ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini"))).get_current_head()


@pytest.fixture
async def scratch_db():
    """A throw-away database so these tests never disturb the session database."""
    url = with_database(TEST_DB_URL, db_name(TEST_DB_URL) + "_mig")
    await create_database_if_missing(url)
    await reset_schema(url)
    yield url


async def tables(url: str) -> set[str]:
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {r["tablename"] for r in rows}
    finally:
        await conn.close()


async def version(url: str) -> str:
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()


async def test_session_database_is_at_head(database):
    assert await version(database) == script_head()
    assert NEW_TABLES <= await tables(database)


async def test_migrate_from_empty_database_to_head(scratch_db):
    run_alembic(scratch_db)
    assert await version(scratch_db) == script_head()
    got = await tables(scratch_db)
    assert NEW_TABLES <= got and not (OLD_TABLES & got)
    run_alembic(scratch_db)                                   # idempotent at head
    assert await version(scratch_db) == script_head()


async def test_migrate_from_pre_0_1_head_replaces_the_old_schema(scratch_db):
    run_alembic(scratch_db, PRE_01_HEAD)
    assert await version(scratch_db) == PRE_01_HEAD
    old = await tables(scratch_db)
    assert OLD_TABLES <= old
    conn = await asyncpg.connect(scratch_db)
    try:
        await conn.execute("INSERT INTO swarms (id, name, vision_statement) "
                           "VALUES ('legacy', 'Legacy', 'old vision')")
    finally:
        await conn.close()

    run_alembic(scratch_db)
    assert await version(scratch_db) == script_head()
    got = await tables(scratch_db)
    assert NEW_TABLES <= got and not (OLD_TABLES & got)

    conn = await asyncpg.connect(scratch_db)
    try:
        # dev databases are disposable: the legacy rows are gone, the new schema is usable
        assert await conn.fetchval("SELECT count(*) FROM swarms") == 0
        await conn.execute("INSERT INTO swarms (id, name, vision_statement) VALUES "
                           "('s1', 'One', 'v')")
        await conn.execute("INSERT INTO teams (id, swarm_id, name) VALUES ('t1', 's1', 'T')")
    finally:
        await conn.close()


async def test_schema_constraints_hold(pool):
    """The invariants the code relies on: one live swarm, cascades, unique ordinals, enums."""
    await pool.execute("INSERT INTO swarms (id, name, vision_statement) VALUES ('s1','A','v')")
    with pytest.raises(asyncpg.UniqueViolationError):          # uq_one_live_swarm
        await pool.execute("INSERT INTO swarms (id, name, vision_statement) VALUES ('s2','B','v')")
    with pytest.raises(asyncpg.CheckViolationError):           # lifecycle vocabulary
        await pool.execute("UPDATE swarms SET lifecycle_state = 'BOGUS' WHERE id = 's1'")
    await pool.execute("UPDATE swarms SET lifecycle_state = 'ARCHIVED' WHERE id = 's1'")
    await pool.execute("INSERT INTO swarms (id, name, vision_statement) VALUES ('s2','B','v')")

    await pool.execute("INSERT INTO teams (id, swarm_id, name) VALUES ('t1','s2','T')")
    await pool.execute("INSERT INTO tasks (id, swarm_id, team_id, name) VALUES ('k1','s2','t1','K')")
    await pool.execute("INSERT INTO acceptance_criteria (id, task_id, ordinal, text) "
                       "VALUES ('c1','k1',1,'x')")
    with pytest.raises(asyncpg.UniqueViolationError):          # (task_id, ordinal)
        await pool.execute("INSERT INTO acceptance_criteria (id, task_id, ordinal, text) "
                           "VALUES ('c2','k1',1,'y')")
    await pool.execute("DELETE FROM swarms WHERE id = 's2'")   # cascades all the way down
    for table in ("teams", "tasks", "acceptance_criteria"):
        assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0
