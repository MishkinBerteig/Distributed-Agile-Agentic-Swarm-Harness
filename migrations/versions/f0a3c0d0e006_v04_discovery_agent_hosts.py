"""DAASH 0.4 schema, part 2: discovery, one-step approval, agent hosts (docs/DESIGN-0.4.md 5.1, 5.8, 5.9)

All remaining 0.4 schema is in this migration so the parallel 0.4 work does not create competing heads.

* connected_agents gains:
  * `status`: pending (waiting for the owner's approval), connected or offline;
  * `kind`: openclaw, hermes, a2a or other;
  * `pending_until`;
  * `runner_id` and `local_ref`, for agents reached through an agent host (protocol `runner`).
  `url` becomes optional, because agents on an agent host have none.
* agent_discoveries: what discovery found on the network (5.8).
* runner_enrollments: one-time codes that turn a machine into an agent host (5.9).
* An index for connected jobs pinned to one runner (5.9 "Work").

Revision ID: f0a3c0d0e006
Revises: f0a3c0d0e005
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f0a3c0d0e006"
down_revision: Union[str, None] = "f0a3c0d0e005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATEMENTS = [
    "ALTER TABLE connected_agents ALTER COLUMN url DROP NOT NULL",
    "ALTER TABLE connected_agents ADD COLUMN status text NOT NULL DEFAULT 'connected' "
    "CHECK (status IN ('pending', 'connected', 'offline'))",
    "ALTER TABLE connected_agents ADD COLUMN kind text NOT NULL DEFAULT 'other' "
    "CHECK (kind IN ('openclaw', 'hermes', 'a2a', 'other'))",
    "ALTER TABLE connected_agents ADD COLUMN pending_until timestamptz",
    "ALTER TABLE connected_agents ADD COLUMN runner_id text REFERENCES runners(id) ON DELETE CASCADE",
    "ALTER TABLE connected_agents ADD COLUMN local_ref text",
    "CREATE UNIQUE INDEX connected_agents_runner_ref ON connected_agents (runner_id, local_ref) "
    "WHERE runner_id IS NOT NULL",
    # queued connected jobs that only one runner (an agent host) may lease
    "CREATE INDEX ix_remote_jobs_pinned_queued ON remote_jobs ((payload->>'runner_id')) "
    "WHERE harness = 'connected' AND status = 'queued'",
    """CREATE TABLE agent_discoveries (
        id text PRIMARY KEY,
        host text NOT NULL,
        port integer NOT NULL,
        kind text NOT NULL,
        status text NOT NULL CHECK (status IN ('found', 'silent')),
        name text,
        url text,
        card jsonb,
        detail text,
        connected_agent_id text REFERENCES connected_agents(id) ON DELETE SET NULL,
        first_seen_at timestamptz NOT NULL DEFAULT now(),
        last_seen_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (host, port)
    )""",
    """CREATE TABLE runner_enrollments (
        id text PRIMARY KEY,
        code_hash text NOT NULL UNIQUE,
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL,
        used_at timestamptz,
        runner_id text REFERENCES runners(id) ON DELETE SET NULL
    )""",
]


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS runner_enrollments CASCADE")
    op.execute("DROP TABLE IF EXISTS agent_discoveries CASCADE")
    op.execute("DROP INDEX IF EXISTS ix_remote_jobs_pinned_queued")
    op.execute("DROP INDEX IF EXISTS connected_agents_runner_ref")
    for col in ("local_ref", "runner_id", "pending_until", "kind", "status"):
        op.execute(f"ALTER TABLE connected_agents DROP COLUMN IF EXISTS {col}")
    op.execute("UPDATE connected_agents SET url = '' WHERE url IS NULL")
    op.execute("ALTER TABLE connected_agents ALTER COLUMN url SET NOT NULL")
