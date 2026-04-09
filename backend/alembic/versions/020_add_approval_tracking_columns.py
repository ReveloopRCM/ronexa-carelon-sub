"""Add approval tracking + pathway intelligence columns.

Case: gold_card_level, auto_approved, approval_type
OutcomePattern: pathway_id, pathway_name, is_gold_card

Revision ID: 020
Revises: 019
Create Date: 2026-04-03
"""
from typing import Sequence, Union

from alembic import op

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Case table — approval tracking
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS gold_card_level INTEGER")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS auto_approved BOOLEAN")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS approval_type VARCHAR")

    # OutcomePattern table — pathway intelligence
    op.execute("ALTER TABLE outcome_patterns ADD COLUMN IF NOT EXISTS pathway_id VARCHAR")
    op.execute("ALTER TABLE outcome_patterns ADD COLUMN IF NOT EXISTS pathway_name VARCHAR")
    op.execute("ALTER TABLE outcome_patterns ADD COLUMN IF NOT EXISTS is_gold_card BOOLEAN DEFAULT FALSE")


def downgrade() -> None:
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS gold_card_level")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS auto_approved")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS approval_type")
    op.execute("ALTER TABLE outcome_patterns DROP COLUMN IF EXISTS pathway_id")
    op.execute("ALTER TABLE outcome_patterns DROP COLUMN IF EXISTS pathway_name")
    op.execute("ALTER TABLE outcome_patterns DROP COLUMN IF EXISTS is_gold_card")
