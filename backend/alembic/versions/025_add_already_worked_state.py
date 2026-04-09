"""Add ALREADY_WORKED to CaseState and ExceptionType enums.

Cases worked in the legacy system during transition. Reps flag these
to clear the review queue — not analytics-protected, can be flushed.

Revision ID: 025
Revises: 024
Create Date: 2026-04-07
"""
from typing import Sequence, Union

from alembic import op

revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE casestate ADD VALUE IF NOT EXISTS 'ALREADY_WORKED'")
    op.execute("ALTER TYPE exceptiontype ADD VALUE IF NOT EXISTS 'ALREADY_WORKED'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values
    pass
