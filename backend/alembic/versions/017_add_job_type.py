"""Add job_type column to submission_jobs table.

Two-job architecture: FIRST_PASS (question discovery) and SUBMIT (portal submission).

Revision ID: 017
Revises: 016
Create Date: 2026-04-01
"""
from typing import Sequence, Union

from alembic import op

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the enum type first
    op.execute("DO $$ BEGIN CREATE TYPE jobtype AS ENUM ('FIRST_PASS', 'SUBMIT'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    # Add column with default
    op.execute("ALTER TABLE submission_jobs ADD COLUMN IF NOT EXISTS job_type VARCHAR NOT NULL DEFAULT 'FIRST_PASS'")


def downgrade() -> None:
    op.execute("ALTER TABLE submission_jobs DROP COLUMN IF EXISTS job_type")
    op.execute("DROP TYPE IF EXISTS jobtype")
