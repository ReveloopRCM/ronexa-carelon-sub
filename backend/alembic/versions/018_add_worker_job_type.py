"""Add job_type column to worker_accounts table.

Each worker has a role: FIRST_PASS (question discovery) or SUBMIT (portal submission).
WorkerLoop reads this at startup to know which queue to claim from.

Revision ID: 018
Revises: 017
Create Date: 2026-04-01
"""
from typing import Sequence, Union

from alembic import op

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE worker_accounts ADD COLUMN IF NOT EXISTS job_type VARCHAR NOT NULL DEFAULT 'FIRST_PASS'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE worker_accounts DROP COLUMN IF EXISTS job_type")
