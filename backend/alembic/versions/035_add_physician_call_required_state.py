"""Add PHYSICIAN_CALL_REQUIRED to CaseState enum.

When the portal's Order Summary page (PrintActivity.aspx) renders the
ineligible label with text matching "treating physician about initiating"
or "Carelon Order number may be required", the imaging center cannot
submit — only the treating physician can initiate the Carelon Order
Request. These cases need a rep to *call* the physician's office.

Previously they landed in NO_AUTH_REQUIRED alongside genuine no-auth
cases and hid in the Completed tab. v155 routes them to this new
PHYSICIAN_CALL_REQUIRED state, which surfaces in a dedicated "Call"
tab in the Cases page (between Hold and Completed).

Revision ID: 035
Revises: 034
Create Date: 2026-06-02
"""
from typing import Sequence, Union

from alembic import op

revision: str = "035"
down_revision: Union[str, None] = "034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE casestate ADD VALUE IF NOT EXISTS 'PHYSICIAN_CALL_REQUIRED'"
    )


def downgrade() -> None:
    # PostgreSQL does not support removing enum values without table rewrite.
    pass
