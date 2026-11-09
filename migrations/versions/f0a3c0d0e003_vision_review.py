"""DAASH 0.3 vision review counter (docs/DESIGN-0.3.md section 10)

Revision ID: f0a3c0d0e003
Revises: f0a3c0d0e002
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f0a3c0d0e003"
down_revision: Union[str, None] = "f0a3c0d0e002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE swarms ADD COLUMN vision_reviews integer NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE swarms DROP COLUMN IF EXISTS vision_reviews")
