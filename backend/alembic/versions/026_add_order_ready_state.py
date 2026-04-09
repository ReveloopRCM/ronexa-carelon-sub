"""Add ORDER_READY to CaseState enum.

New pipeline: cases without clinical notes but with order forms (file_key)
go through OrderWorkflow. ORDER_READY is the "ready to process" state
for order-only cases, analogous to NOTES_UPLOADED for clinical cases.

Revision ID: 026
Revises: 025
Create Date: 2026-04-08
"""
from typing import Sequence, Union

from alembic import op

revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE casestate ADD VALUE IF NOT EXISTS 'ORDER_READY'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values
    pass
