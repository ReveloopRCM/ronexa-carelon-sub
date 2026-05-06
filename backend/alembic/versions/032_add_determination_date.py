"""Add determination_date to cases.

Carelon's confirmation page shows two distinct dates: "Scheduled Date of
Service" (the appointment date — already captured as valid_from) and
"Anticipated Determination Date" (when Carelon expects to render an auth
decision — what the rep actually cares about). Reps were comparing the
determination date in Carelon's UI against our valid_from and seeing a
mismatch (off by ~1 day) because they're conceptually different fields.

This column captures the determination date specifically. It's populated
for PENDED / In-Progress outcomes (where the decision is pending) and
left NULL for APPROVED outcomes (where the decision is already final).

Revision ID: 032
Revises: 031
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "032"
down_revision: Union[str, None] = "031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("determination_date", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cases", "determination_date")
