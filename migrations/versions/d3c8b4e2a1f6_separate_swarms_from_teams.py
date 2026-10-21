"""Separate swarms from teams; add agent_sessions

Revision ID: d3c8b4e2a1f6
Revises: c4a7e2f8d1b5
Create Date: 2026-10-09

The Slice 3 "single live swarm" hack made the ``swarms`` REST surface an alias
for the ``teams`` table: one swarm, one team, lifecycle on the team row. That
cannot hold once a swarm contains *multiple* teams (slice 5.1.3) and real
agent sessions must be tracked per team member (5.1.0).

Per the README: a swarm *is* the container of one or more teams; the Vision is
swarm-level (injected into every session); the Mission is team-level; teams
have no lifecycle of their own — the swarm does.

This migration:
  1. Creates a real ``swarms`` table (id, name, vision, lifecycle, created_at).
  2. Adds ``teams.swarm_id`` and backfills a 1:1 ``swarms`` row per legacy team
     (dev databases are disposable; production is pre-release, so a simple
     1:1 backfill preserves everything).
  3. Relaxes ``teams.vision_statement`` / ``mission_statement`` /
     ``lifecycle_state`` to nullable (vision now lives on the swarm; teams
     carry no lifecycle). The Slice 3 single-live-swarm unique index and the
     teams lifecycle CHECK move to the new swarms table.
  4. Creates ``agent_sessions``: one row per live harness session the
     coordinator launches through a host runner (5.1.0). Rows are the durable
     source of truth for what is running; the runner is a cache.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3c8b4e2a1f6'
down_revision: Union[str, Sequence[str], None] = 'c4a7e2f8d1b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Lifecycle vocabulary moves wholesale from teams to swarms.
LIFECYCLE_STATES = (
    'CREATED',
    'ACTIVE',
    'PAUSED',
    'USER_FEEDBACK',
    'VERIFICATION',
    'LEARNING',
    'ARCHIVED',
    'DELETED',
)

TERMINAL_STATES = ('ARCHIVED', 'DELETED')


def upgrade() -> None:
    # 1. The real swarm table.
    op.create_table(
        'swarms',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('vision_statement', sa.Text(), nullable=False),
        sa.Column('lifecycle_state', sa.String(length=32), nullable=False,
                  server_default='CREATED'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    state_list = ", ".join(f"'{s}'" for s in LIFECYCLE_STATES)
    op.create_check_constraint(
        'swarms_lifecycle_state_check', 'swarms', f'lifecycle_state IN ({state_list})'
    )

    # 2. teams.swarm_id — nullable: a team is first-class (it can exist on its
    #    own, as tests and tooling insert them); a swarm *groups* teams. The FK
    #    is only enforced when a team IS assigned to a swarm.
    op.add_column('teams', sa.Column('swarm_id', sa.String(), nullable=True))

    # 3. Backfill 1:1: every legacy team becomes a one-team swarm, keeping the
    #    old single-swarm rows fully intact (including their lifecycle state).
    op.execute(
        "INSERT INTO swarms (id, name, vision_statement, lifecycle_state, created_at) "
        "SELECT id || '-swarm', name, COALESCE(vision_statement, ''), "
        "COALESCE(lifecycle_state, 'CREATED'), created_at FROM teams"
    )
    op.execute(
        "UPDATE teams t SET swarm_id = s.id FROM swarms s WHERE s.id = t.id || '-swarm'"
    )
    # Legacy vision now lives on the swarm; keep the team column as a mirror so
    # existing prompt paths keep working until they are re-pointed.
    op.execute(
        "UPDATE teams t SET vision_statement = s.vision_statement "
        "FROM swarms s WHERE s.id = t.swarm_id"
    )
    # Teams may exist without a swarm in tests/raw inserts; keep a FK for
    # integrity where one IS set.
    op.create_foreign_key('teams_swarm_fk', 'teams', 'swarms',
                          ['swarm_id'], ['id'])

    # 4. Teams lose NOT NULL guarantees that no longer apply.
    op.alter_column('teams', 'vision_statement', nullable=True)
    op.alter_column('teams', 'mission_statement', nullable=True)
    op.alter_column('teams', 'lifecycle_state', nullable=True)

    # 5. Retire the Slice 3 single-swarm invariant from teams — the swarm table
    #    carries it instead.
    op.drop_index('uq_one_live_swarm', table_name='teams')
    op.drop_constraint('teams_lifecycle_state_check', 'teams', type_='check')
    terminal_list = ", ".join(f"'{s}'" for s in TERMINAL_STATES)
    op.execute(
        f"CREATE UNIQUE INDEX uq_one_live_swarm ON swarms ((1)) "
        f"WHERE lifecycle_state NOT IN ({terminal_list})"
    )

    # 6. Agent sessions (slice 5.1.0): durable record of one harness session
    #    per team role, as launched through a host runner.
    op.create_table(
        'agent_sessions',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('swarm_id', sa.String(), nullable=False),
        sa.Column('team_id', sa.String(), nullable=False),
        sa.Column('role', sa.String(length=60), nullable=False),
        sa.Column('harness', sa.String(length=60), nullable=False,
                  server_default='openclaude'),
        sa.Column('model', sa.String(length=255), nullable=True),
        sa.Column('runner_url', sa.String(length=255), nullable=False,
                  server_default=''),
        sa.Column('session_state', sa.String(length=20), nullable=False,
                  server_default='pending'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('harness_session_id', sa.String(length=255), nullable=True),
        sa.Column('turns', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('launched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('agent_sessions_swarm_idx', 'agent_sessions', ['swarm_id'])
    op.create_index('agent_sessions_team_idx', 'agent_sessions', ['team_id'])


def downgrade() -> None:
    op.drop_index('agent_sessions_team_idx', table_name='agent_sessions')
    op.drop_index('agent_sessions_swarm_idx', table_name='agent_sessions')
    op.drop_table('agent_sessions')

    op.drop_index('uq_one_live_swarm', table_name='swarms')
    op.drop_constraint('swarms_lifecycle_state_check', 'swarms', type_='check')
    op.drop_constraint('teams_swarm_fk', 'teams', type_='foreignkey')

    # Restore teams to the Slice 3 shape, then drop the swarm table.
    op.execute("UPDATE teams SET swarm_id = ''")
    op.drop_column('teams', 'swarm_id')
    op.alter_column('teams', 'vision_statement', nullable=False,
                    server_default='')
    op.alter_column('teams', 'mission_statement', nullable=False,
                    server_default='')
    op.alter_column('teams', 'lifecycle_state', nullable=False,
                    server_default='CREATED')
    state_list = ", ".join(f"'{s}'" for s in LIFECYCLE_STATES)
    op.create_check_constraint(
        'teams_lifecycle_state_check', 'teams', f'lifecycle_state IN ({state_list})'
    )
    terminal_list = ", ".join(f"'{s}'" for s in TERMINAL_STATES)
    op.execute(
        f"CREATE UNIQUE INDEX uq_one_live_swarm ON teams ((1)) "
        f"WHERE lifecycle_state NOT IN ({terminal_list})"
    )
    op.drop_table('swarms')
