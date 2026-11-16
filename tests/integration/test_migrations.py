"""Alembic migrations: from an empty database and from the pre-0.1 head (docs/DESIGN-0.2.md A.2)."""
from __future__ import annotations

import os
import subprocess
import sys

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


async def columns(url: str, table: str) -> set[str]:
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch("SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = 'public' AND table_name = $1", table)
        return {r["column_name"] for r in rows}
    finally:
        await conn.close()


def alembic_downgrade(db_url: str, target: str) -> None:
    env = {**os.environ, "DAASH_ALEMBIC_DATABASE_URL": db_url}
    res = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), "downgrade",
         target], cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


async def test_0_4_schema_is_two_migrations_and_reversible(scratch_db):
    """0.4's schema (docs/DESIGN-0.4.md) is f0a3c0d0e005 (MCP, messages, connected agents) plus
    f0a3c0d0e006 (discovery, approval state, agent hosts), on top of the 0.3 head."""
    run_alembic(scratch_db, "f0a3c0d0e006")
    assert {"agent_messages", "connected_agents", "agent_discoveries",
            "runner_enrollments"} <= await tables(scratch_db)
    assert "mcp_token_hash" in await columns(scratch_db, "remote_jobs")
    assert await columns(scratch_db, "connected_agents") == {
        "id", "name", "protocol", "url", "endpoint", "skill", "token", "mcp_token_hash",
        "capabilities", "max_jobs", "enabled", "card", "last_ok_at", "last_error",
        "mcp_seen_at", "created_at", "status", "kind", "pending_until", "runner_id", "local_ref"}
    alembic_downgrade(scratch_db, "f0a3c0d0e005")
    assert not {"agent_discoveries", "runner_enrollments"} & await tables(scratch_db)
    assert "status" not in await columns(scratch_db, "connected_agents")
    alembic_downgrade(scratch_db, "f0a3c0d0e004")
    assert not {"agent_messages", "connected_agents"} & await tables(scratch_db)
    assert "mcp_token_hash" not in await columns(scratch_db, "remote_jobs")
    run_alembic(scratch_db)
    assert await version(scratch_db) == script_head()


async def column_type(url: str, table: str, column: str) -> str:
    conn = await asyncpg.connect(url)
    try:
        return await conn.fetchval(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = $1::regclass AND attname = $2", table, column)
    finally:
        await conn.close()


async def test_0_4_1_embeddings_schema_is_one_reversible_migration(scratch_db):
    """0.4.1 (docs/DESIGN-0.4.1.md section 4): untyped vectors with the model and the text, the old
    word-hash vectors cleared with their text kept, and embedding_status."""
    assert script_head() == "f0a3c0d0e007"
    run_alembic(scratch_db, "f0a3c0d0e006")
    old_vec = "[" + ",".join(["0.1"] * 768) + "]"
    conn = await asyncpg.connect(scratch_db)
    try:
        await conn.execute("INSERT INTO swarms (id, name, vision_statement) VALUES ('s', 'S', 'v')")
        await conn.execute("INSERT INTO teams (id, swarm_id, name) VALUES ('t', 's', 'T')")
        await conn.execute("INSERT INTO agents (id, swarm_id, team_id, role, name, model) "
                           "VALUES ('a', 's', 't', 'mission_delivery', 'D', 'fast')")
        await conn.execute("INSERT INTO tasks (id, swarm_id, team_id, name, description, embedding) "
                           "VALUES ('k', 's', 't', 'Write README', 'One example each', $1::vector)",
                           old_vec)
        await conn.execute("INSERT INTO tasks (id, swarm_id, team_id, name) VALUES ('k2', 's', 't', 'Bare')")
        await conn.execute("INSERT INTO learnings (id, swarm_id, kind, content, embedding) "
                           "VALUES ('l', 's', 'pattern', 'Plan fewer tasks', $1::vector)", old_vec)
        await conn.execute("INSERT INTO memories (id, agent_id, swarm_id, content, embedding) "
                           "VALUES ('m', 'a', 's', 'Bob likes pizza', $1::vector)", old_vec)
    finally:
        await conn.close()
    assert await column_type(scratch_db, "memories", "embedding") == "vector(768)"

    run_alembic(scratch_db)
    for table in ("tasks", "learnings", "memories"):
        assert {"embedding", "embedding_model", "embedding_text"} <= await columns(scratch_db, table)
        assert await column_type(scratch_db, table, "embedding") == "vector"
    conn = await asyncpg.connect(scratch_db)
    try:
        rows = {r["id"]: r for r in await conn.fetch(
            "SELECT id, embedding, embedding_model, embedding_text FROM tasks "
            "UNION ALL SELECT id, embedding, embedding_model, embedding_text FROM learnings "
            "UNION ALL SELECT id, embedding, embedding_model, embedding_text FROM memories")}
        assert {k: r["embedding_text"] for k, r in rows.items()} == {
            "k": "Write README\nOne example each", "k2": "Bare", "l": "Plan fewer tasks",
            "m": "Bob likes pizza"}
        assert all(r["embedding"] is None and r["embedding_model"] is None for r in rows.values())
        status = await conn.fetchrow("SELECT * FROM embedding_status")
        assert status["id"] == 1 and status["state"] == "starting"
        # any model's vectors fit now
        await conn.execute("UPDATE memories SET embedding = '[1,2,3]', embedding_model = 'x/y'")
    finally:
        await conn.close()

    alembic_downgrade(scratch_db, "f0a3c0d0e006")
    assert "embedding_status" not in await tables(scratch_db)
    assert await column_type(scratch_db, "memories", "embedding") == "vector(768)"
    assert "embedding_model" not in await columns(scratch_db, "memories")
    run_alembic(scratch_db)
    assert await version(scratch_db) == "f0a3c0d0e007"
