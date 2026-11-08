"""DAASH 0.1 prototype schema (docs/DESIGN-0.1.md section 5)

Revision ID: e5a1d0c0f001
Revises: d3c8b4e2a1f6

Dev databases are disposable: the old tables are dropped and the full new
schema is created. Hand-written SQL; column names equal app/models.py fields.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e5a1d0c0f001"
down_revision: Union[str, None] = "d3c8b4e2a1f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_TABLES = ["agent_sessions", "memory_entries", "transcripts", "tasks", "teams", "swarms"]
NEW_TABLES = ["feedback", "team_proposals", "memories", "learnings", "agent_calls",
              "task_events", "verdicts", "acceptance_criteria", "tasks", "agents",
              "teams", "swarms", "global_config"]

STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS vector",
    """CREATE TABLE global_config (
        key text PRIMARY KEY,
        value jsonb NOT NULL
    )""",
    """CREATE TABLE swarms (
        id text PRIMARY KEY,
        name text NOT NULL,
        vision_statement text NOT NULL,
        definition_of_done text,
        lifecycle_state text NOT NULL DEFAULT 'CREATED',
        config jsonb NOT NULL DEFAULT '{}'::jsonb,
        paused_reason text,
        retrospective_done boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT now(),
        started_at timestamptz,
        CONSTRAINT ck_swarms_lifecycle_state CHECK (lifecycle_state IN
            ('CREATED','ACTIVE','PAUSED','USER_FEEDBACK','VERIFICATION','LEARNING','ARCHIVED','DELETED'))
    )""",
    "CREATE UNIQUE INDEX uq_one_live_swarm ON swarms ((1)) "
    "WHERE lifecycle_state NOT IN ('ARCHIVED','DELETED')",
    """CREATE TABLE teams (
        id text PRIMARY KEY,
        swarm_id text NOT NULL REFERENCES swarms(id) ON DELETE CASCADE,
        name text NOT NULL,
        mission_statement text NOT NULL DEFAULT '',
        status text NOT NULL DEFAULT 'active',
        creator_kind text NOT NULL DEFAULT 'user',
        creator_team_id text REFERENCES teams(id) ON DELETE SET NULL,
        definition_of_ready text,
        definition_of_done text,
        generation integer NOT NULL DEFAULT 0,
        next_focus text,
        last_assessed_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX ix_teams_swarm_id ON teams (swarm_id)",
    """CREATE TABLE agents (
        id text PRIMARY KEY,
        swarm_id text NOT NULL REFERENCES swarms(id) ON DELETE CASCADE,
        team_id text REFERENCES teams(id) ON DELETE CASCADE,
        role text NOT NULL,
        name text NOT NULL,
        harness text NOT NULL DEFAULT 'daash',
        provider text NOT NULL DEFAULT 'default',
        model text NOT NULL,
        state text NOT NULL DEFAULT 'idle',
        current_task_id text,
        tokens_used bigint NOT NULL DEFAULT 0,
        calls integer NOT NULL DEFAULT 0,
        errors integer NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL DEFAULT now(),
        last_active_at timestamptz
    )""",
    "CREATE INDEX ix_agents_swarm_id ON agents (swarm_id)",
    "CREATE INDEX ix_agents_team_id ON agents (team_id)",
    """CREATE TABLE tasks (
        id text PRIMARY KEY,
        swarm_id text NOT NULL REFERENCES swarms(id) ON DELETE CASCADE,
        team_id text NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        parent_id text REFERENCES tasks(id) ON DELETE CASCADE,
        name text NOT NULL,
        description text NOT NULL DEFAULT '',
        artifact text NOT NULL DEFAULT '',
        status text NOT NULL DEFAULT 'Pending',
        priority integer,
        complexity text,
        depends_on text[] NOT NULL DEFAULT '{}',
        assigned_agent_id text,
        attempts integer NOT NULL DEFAULT 0,
        created_by text NOT NULL DEFAULT 'user',
        rejection_reason text,
        feedback text,
        deliverable jsonb NOT NULL DEFAULT '{}'::jsonb,
        embedding vector(768),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        approved_at timestamptz,
        committed_at timestamptz,
        done_at timestamptz
    )""",
    "CREATE INDEX ix_tasks_swarm_status ON tasks (swarm_id, status)",
    "CREATE INDEX ix_tasks_team_id ON tasks (team_id)",
    "CREATE INDEX ix_tasks_parent_id ON tasks (parent_id)",
    """CREATE TABLE acceptance_criteria (
        id text PRIMARY KEY,
        task_id text NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        ordinal integer NOT NULL,
        text text NOT NULL,
        check_kind text NOT NULL DEFAULT 'rubric',
        check_path text,
        check_arg text,
        status text NOT NULL DEFAULT 'unknown',
        score double precision,
        evidence text,
        updated_at timestamptz,
        CONSTRAINT uq_criteria_task_ordinal UNIQUE (task_id, ordinal)
    )""",
    """CREATE TABLE verdicts (
        id text PRIMARY KEY,
        task_id text NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        attempt integer NOT NULL,
        judge_role text NOT NULL,
        agent_id text,
        passed boolean NOT NULL,
        score double precision NOT NULL,
        reasoning text NOT NULL DEFAULT '',
        details jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX ix_verdicts_task_id ON verdicts (task_id)",
    # task_id / team_id are deliberately not FKs: history survives task deletes.
    """CREATE TABLE task_events (
        id bigserial PRIMARY KEY,
        swarm_id text NOT NULL REFERENCES swarms(id) ON DELETE CASCADE,
        team_id text,
        task_id text,
        kind text NOT NULL,
        from_status text,
        to_status text,
        actor text NOT NULL DEFAULT 'engine',
        detail text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX ix_task_events_swarm_created ON task_events (swarm_id, created_at DESC)",
    "CREATE INDEX ix_task_events_task_id ON task_events (task_id)",
    """CREATE TABLE agent_calls (
        id text PRIMARY KEY,
        swarm_id text NOT NULL REFERENCES swarms(id) ON DELETE CASCADE,
        team_id text,
        agent_id text,
        task_id text,
        role text NOT NULL,
        model text NOT NULL,
        purpose text NOT NULL,
        prompt_tokens integer NOT NULL DEFAULT 0,
        completion_tokens integer NOT NULL DEFAULT 0,
        total_tokens integer NOT NULL DEFAULT 0,
        duration_ms integer NOT NULL DEFAULT 0,
        ok boolean NOT NULL DEFAULT true,
        error text,
        transcript jsonb NOT NULL DEFAULT '[]'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX ix_agent_calls_swarm_created ON agent_calls (swarm_id, created_at)",
    "CREATE INDEX ix_agent_calls_agent_id ON agent_calls (agent_id, created_at DESC)",
    """CREATE TABLE learnings (
        id text PRIMARY KEY,
        swarm_id text REFERENCES swarms(id) ON DELETE CASCADE,
        team_id text,
        task_id text,
        kind text NOT NULL DEFAULT 'pattern',
        content text NOT NULL,
        created_by text NOT NULL DEFAULT 'engine',
        embedding vector(768),
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX ix_learnings_swarm_id ON learnings (swarm_id)",
    """CREATE TABLE memories (
        id text PRIMARY KEY,
        agent_id text NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
        swarm_id text NOT NULL,
        team_id text,
        task_id text,
        content text NOT NULL,
        embedding vector(768),
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX ix_memories_agent_id ON memories (agent_id)",
    """CREATE TABLE team_proposals (
        id text PRIMARY KEY,
        swarm_id text NOT NULL REFERENCES swarms(id) ON DELETE CASCADE,
        team_id text NOT NULL,
        proposer_agent_id text NOT NULL,
        name text NOT NULL,
        mission_statement text NOT NULL,
        reason text NOT NULL DEFAULT '',
        status text NOT NULL DEFAULT 'pending',
        votes jsonb NOT NULL DEFAULT '{}'::jsonb,
        quorum integer NOT NULL DEFAULT 1,
        created_team_id text,
        created_at timestamptz NOT NULL DEFAULT now(),
        decided_at timestamptz
    )""",
    "CREATE INDEX ix_team_proposals_swarm_id ON team_proposals (swarm_id)",
    """CREATE TABLE feedback (
        id text PRIMARY KEY,
        swarm_id text NOT NULL REFERENCES swarms(id) ON DELETE CASCADE,
        text text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX ix_feedback_swarm_id ON feedback (swarm_id)",
]


def upgrade() -> None:
    for t in OLD_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for t in NEW_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
