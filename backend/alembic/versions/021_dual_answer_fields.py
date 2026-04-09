"""Add dual-answer fields to questions table.

Notes-path columns store the documentation-honest answer alongside
the approval-optimized answer, giving reps both perspectives.

Revision ID: 021
Revises: 020
Create Date: 2026-04-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("ai_notes_answer", sa.JSON(), nullable=True))
    op.add_column("questions", sa.Column("ai_notes_confidence", sa.Float(), nullable=True))
    op.add_column("questions", sa.Column("ai_notes_reasoning", sa.Text(), nullable=True))
    op.add_column("questions", sa.Column("ai_approval_gap", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("questions", "ai_approval_gap")
    op.drop_column("questions", "ai_notes_reasoning")
    op.drop_column("questions", "ai_notes_confidence")
    op.drop_column("questions", "ai_notes_answer")
