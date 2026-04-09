"""Add algorithm_recommendation column to cases table.

Stores the raw RecommendationType value from the portal's clinical
algorithm (GPAWV response). 3=Approve, 6=InProgress.

Revision ID: 023
Revises: 022
Create Date: 2026-04-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS algorithm_recommendation INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS algorithm_recommendation")
