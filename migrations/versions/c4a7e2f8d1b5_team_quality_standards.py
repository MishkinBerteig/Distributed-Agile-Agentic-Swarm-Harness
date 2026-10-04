"""team quality standards (Definition of Ready / Done)

Revision ID: c4a7e2f8d1b5
Revises: b7f3c1a9d2e4
Create Date: 2026-10-03

Slice 5.0 — Quality Judgement generates team DoR/DoD. Each team stores the
Definition of Ready and Definition of Done it generated (via the harness
adapter) as free text on its row. Nullable with no server default: a team
that has not run quality generation yet simply has no standards, which is
distinct from an empty-string standard. The values are regenerable at any
time, so no backfill is attempted for existing rows.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a7e2f8d1b5'
down_revision: Union[str, Sequence[str], None] = 'b7f3c1a9d2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'teams',
        sa.Column('definition_of_ready', sa.Text(), nullable=True),
    )
    op.add_column(
        'teams',
        sa.Column('definition_of_done', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('teams', 'definition_of_done')
    op.drop_column('teams', 'definition_of_ready')
