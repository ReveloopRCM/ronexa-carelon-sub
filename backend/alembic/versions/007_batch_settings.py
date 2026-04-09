"""Add batch processing settings + APPROVED_FOR_SUBMIT state

Revision ID: 007
Revises: 006
Create Date: 2026-03-25
"""
from typing import Sequence, Union

from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add APPROVED_FOR_SUBMIT to casestate enum
    op.execute("""
        DO $$ BEGIN
            ALTER TYPE casestate ADD VALUE IF NOT EXISTS 'APPROVED_FOR_SUBMIT' AFTER 'L2_REVIEW';
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)

    # Seed batch processing settings
    op.execute("""
        INSERT INTO system_settings (key, value, updated_by) VALUES
        ('auto_process_enabled', 'false', 'system'),
        ('max_cases_per_batch', '50', 'system'),
        ('finalize_batch_size', '10', 'system')
        ON CONFLICT (key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM system_settings WHERE key IN ('auto_process_enabled', 'max_cases_per_batch', 'finalize_batch_size')")
