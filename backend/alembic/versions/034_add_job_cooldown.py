"""Add cooldown_until to submission_jobs.

Workers retry transient portal errors via mark_case_hold's auto_requeue
path — but the retries fire back-to-back (claim_next_job picks up the
re-QUEUED row within ~10 seconds). When Carelon hits a 1-2 minute
post-eligibility transition flake, three immediate retries all land
inside the flake window and the case exhausts to permanent HOLD even
though the portal would have recovered shortly after.

cooldown_until lets us delay the next claim by a few minutes for the
specific transient patterns we've observed: "Provider search page did
not load", "Could not select provider", "Facility continue failed".
NULL = immediately claimable (existing behavior). claim_next_job's WHERE
clause adds `(cooldown_until IS NULL OR cooldown_until <= NOW())`.

Revision ID: 034
Revises: 033
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "034"
down_revision: Union[str, None] = "033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "submission_jobs",
        sa.Column("cooldown_until", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("submission_jobs", "cooldown_until")
