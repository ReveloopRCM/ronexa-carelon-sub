"""Add pathway_name and pathway_id to cases table.

Revision ID: 016
Revises: 015
Create Date: 2026-04-01
"""
from typing import Sequence, Union

from alembic import op

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS pathway_name VARCHAR")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS pathway_id VARCHAR")


def downgrade() -> None:
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS pathway_name")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS pathway_id")
