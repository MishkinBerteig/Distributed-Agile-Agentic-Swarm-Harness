"""swarm lifecycle state + single-live-swarm invariant

Revision ID: b7f3c1a9d2e4
Revises: 9ce40a1bd3c6
Create Date: 2026-10-01

A swarm may only ever be instantiated one at a time. This migration adds the
lifecycle state machine column and a database-level guarantee that at most one
swarm is "live" (not ARCHIVED) at any moment. DELETED swarms are hard-deleted,
so they never persist; ARCHIVED is the only terminal row retained for history.

The singleton guard is a partial unique index on a constant expression: the
index key is identical (`1`) for every live row, so Postgres permits at most one.
Archived rows fall outside the WHERE predicate and are excluded from the index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7f3c1a9d2e4'
down_revision: Union[str, Sequence[str], None] = '9ce40a1bd3c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Lifecycle states mirror the DAASH swarm state diagram. DELETED is included
# as a transient state set during the hard-delete transaction.
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

# Terminal states that release the single-swarm slot. DELETED is included for
# forward-compatibility even though such rows are hard-deleted (never present
# long enough to conflict).
TERMINAL_STATES = ('ARCHIVED', 'DELETED')


def upgrade() -> None:
    # 1. Add the lifecycle column, defaulting existing/blank rows to CREATED so the
    #    NOT NULL add is valid on a non-empty table. Dev DBs are reset freely, so no
    #    smart backfill is attempted here.
    op.add_column(
        'teams',
        sa.Column('lifecycle_state', sa.String(length=32), nullable=False,
                  server_default='CREATED'),
    )

    # 2. Constrain the column to the known lifecycle vocabulary.
    state_list = ", ".join(f"'{s}'" for s in LIFECYCLE_STATES)
    op.create_check_constraint(
        'teams_lifecycle_state_check',
        'teams',
        f'lifecycle_state IN ({state_list})',
    )

    # 3. Enforce "at most one live swarm" at the database level. The constant index
    #    key means all live rows collide; archived (terminal) rows are excluded by
    #    the WHERE predicate and thus never conflict.
    terminal_list = ", ".join(f"'{s}'" for s in TERMINAL_STATES)
    op.execute(
        f"CREATE UNIQUE INDEX uq_one_live_swarm ON teams ((1)) "
        f"WHERE lifecycle_state NOT IN ({terminal_list})"
    )


def downgrade() -> None:
    op.drop_index('uq_one_live_swarm', table_name='teams')
    op.drop_constraint('teams_lifecycle_state_check', 'teams', type_='check')
    op.drop_column('teams', 'lifecycle_state')
