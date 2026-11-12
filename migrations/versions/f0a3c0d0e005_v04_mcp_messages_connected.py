"""DAASH 0.4 schema: MCP job tokens, team messages, connected agents (docs/DESIGN-0.4.md)

All 0.4 schema is in this one migration so later 0.4 work does not create competing heads.

* remote_jobs.mcp_token_hash (section 2.3): sha256 of the per-job MCP token, minted at lease.
* agent_messages (section 3): one row per recipient (a message to the whole team fans out), so
  read state stays per recipient. `send_id` groups the rows of one send.
* connected_agents (section 5.1): existing agents reached over A2A or the Hermes API server.

Revision ID: f0a3c0d0e005
Revises: f0a3c0d0e004
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f0a3c0d0e005"
down_revision: Union[str, None] = "f0a3c0d0e004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATEMENTS = [
    "ALTER TABLE remote_jobs ADD COLUMN mcp_token_hash text",
    "CREATE UNIQUE INDEX remote_jobs_mcp_token ON remote_jobs (mcp_token_hash) "
    "WHERE mcp_token_hash IS NOT NULL",
    # active connected jobs of one connected agent (MCP token resolution, section 2.3)
    "CREATE INDEX ix_remote_jobs_connected_active ON remote_jobs "
    "((payload->>'connected_agent_id')) "
    "WHERE harness = 'connected' AND status IN ('queued', 'leased')",
    """CREATE TABLE agent_messages (
        id text PRIMARY KEY,
        send_id text NOT NULL,
        swarm_id text NOT NULL REFERENCES swarms(id) ON DELETE CASCADE,
        team_id text,
        from_agent_id text NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
        to_agent_id text REFERENCES agents(id) ON DELETE CASCADE,
        audience text NOT NULL DEFAULT 'direct',
        task_id text,
        attempt integer,
        text text NOT NULL CHECK (char_length(text) <= 4000),
        created_at timestamptz NOT NULL DEFAULT now(),
        read_at timestamptz
    )""",
    "CREATE INDEX ix_agent_messages_unread ON agent_messages (to_agent_id, created_at) "
    "WHERE read_at IS NULL",
    "CREATE INDEX ix_agent_messages_swarm ON agent_messages (swarm_id, created_at)",
    "CREATE INDEX ix_agent_messages_sender ON agent_messages (from_agent_id, task_id, attempt)",
    """CREATE TABLE connected_agents (
        id text PRIMARY KEY,
        name text NOT NULL UNIQUE,
        protocol text NOT NULL,
        url text NOT NULL,
        endpoint text,
        skill text,
        token text,
        mcp_token_hash text UNIQUE,
        capabilities jsonb NOT NULL DEFAULT '["edit_files"]'::jsonb,
        max_jobs integer NOT NULL DEFAULT 1,
        enabled boolean NOT NULL DEFAULT true,
        card jsonb,
        last_ok_at timestamptz,
        last_error text,
        mcp_seen_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
]


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS connected_agents CASCADE")
    op.execute("DROP TABLE IF EXISTS agent_messages CASCADE")
    op.execute("DROP INDEX IF EXISTS ix_remote_jobs_connected_active")
    op.execute("DROP INDEX IF EXISTS remote_jobs_mcp_token")
    op.execute("ALTER TABLE remote_jobs DROP COLUMN IF EXISTS mcp_token_hash")
