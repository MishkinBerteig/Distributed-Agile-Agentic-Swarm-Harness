"""DAASH 0.3 runners and remote jobs (docs/DESIGN-0.3.md section 3)

Revision ID: f0a3c0d0e002
Revises: e5a1d0c0f001
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f0a3c0d0e002"
down_revision: Union[str, None] = "e5a1d0c0f001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATEMENTS = [
    """CREATE TABLE runners (
        id text PRIMARY KEY,
        name text NOT NULL UNIQUE,
        token_hash text NOT NULL UNIQUE,
        enabled boolean NOT NULL DEFAULT true,
        host text,
        version text,
        harnesses jsonb NOT NULL DEFAULT '[]'::jsonb,
        max_jobs integer NOT NULL DEFAULT 1,
        last_seen_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE remote_jobs (
        id text PRIMARY KEY,
        swarm_id text NOT NULL REFERENCES swarms(id) ON DELETE CASCADE,
        team_id text NOT NULL,
        task_id text NOT NULL,
        agent_id text NOT NULL,
        attempt integer NOT NULL,
        harness text NOT NULL,
        model text NOT NULL,
        status text NOT NULL DEFAULT 'queued',
        runner_id text REFERENCES runners(id) ON DELETE SET NULL,
        payload jsonb NOT NULL,
        result jsonb,
        expiries integer NOT NULL DEFAULT 0,
        lease_expires_at timestamptz,
        heartbeat_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        leased_at timestamptz,
        finished_at timestamptz
    )""",
    "CREATE UNIQUE INDEX remote_jobs_one_active ON remote_jobs (task_id) "
    "WHERE status IN ('queued', 'leased')",
    "CREATE INDEX ix_remote_jobs_swarm_id ON remote_jobs (swarm_id)",
    "CREATE INDEX ix_remote_jobs_status ON remote_jobs (status, created_at)",
]


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS remote_jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS runners CASCADE")
