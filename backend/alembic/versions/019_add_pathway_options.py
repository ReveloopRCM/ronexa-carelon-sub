"""Add pathway_options JSON column to cases table.

Stores the list of pathway options from GetPathwayOptions API
so reps can see and change the AI's pathway selection during review.

Revision ID: 019
Revises: 018
Create Date: 2026-04-02
"""
from typing import Sequence, Union

from alembic import op

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE cases ADD COLUMN IF NOT EXISTS pathway_options JSONB"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS pathway_options")
