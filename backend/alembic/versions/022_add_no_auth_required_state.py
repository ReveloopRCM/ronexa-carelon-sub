"""Add NO_AUTH_REQUIRED to CaseState and ExceptionType enums.

Portal sometimes indicates that DI pre-authorization is not required
for a member's plan. This terminal state tracks those cases.

Revision ID: 022
Revises: 021
Create Date: 2026-04-04
"""
from typing import Sequence, Union

from alembic import op

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE casestate ADD VALUE IF NOT EXISTS 'NO_AUTH_REQUIRED'")
    op.execute("ALTER TYPE exceptiontype ADD VALUE IF NOT EXISTS 'NO_AUTH_REQUIRED'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values
    pass
