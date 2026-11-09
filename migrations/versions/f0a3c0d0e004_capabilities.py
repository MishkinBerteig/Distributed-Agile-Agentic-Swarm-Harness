"""DAASH 0.3 agent capabilities (docs/DESIGN-0.3.md section 11)

Revision ID: f0a3c0d0e004
Revises: f0a3c0d0e003
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f0a3c0d0e004"
down_revision: Union[str, None] = "f0a3c0d0e003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE agents ADD COLUMN capabilities jsonb NOT NULL DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE tasks ADD COLUMN required_capabilities jsonb NOT NULL "
               "DEFAULT '[]'::jsonb")
    # existing agents get their harness default (docs/DESIGN-0.3.md section 11)
    op.execute("UPDATE agents SET capabilities = '[\"edit_files\"]'::jsonb "
               "WHERE harness = 'daash'")
    op.execute("UPDATE agents SET capabilities = '[\"edit_files\", \"run_code\"]'::jsonb "
               "WHERE harness <> 'daash'")


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS required_capabilities")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS capabilities")
