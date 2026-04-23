"""Add SUBMISSION_ERROR to CaseState enum.

Cases where Carelon's portal refused at the submit step (duplicate-order
modal, medical-necessity criteria page, portal 5xx, session expired, etc.)
now land in a dedicated SUBMISSION_ERROR state instead of the generic HOLD.
The subcategory is stored on SubmissionJob.exception_type (DUPLICATE_AUTH,
MED_NECESSITY, PORTAL_ERROR — all already defined).

Revision ID: 029
Revises: 028
Create Date: 2026-04-23
"""
from typing import Sequence, Union

from alembic import op

revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE casestate ADD VALUE IF NOT EXISTS 'SUBMISSION_ERROR'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values
    pass
