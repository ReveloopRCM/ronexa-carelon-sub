"""Add missing case columns (clinical_blob_key, referring_fax, etc.)

Revision ID: 005
Revises: 004
Create Date: 2026-03-24
"""
from typing import Sequence, Union

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS clinical_blob_key VARCHAR")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS referring_fax VARCHAR")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS external_reference_id VARCHAR")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS is_stat BOOLEAN DEFAULT false")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS scheduled_dt TIMESTAMP")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS auth_exam_id VARCHAR")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS order_request_id VARCHAR")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS sort_priority INTEGER DEFAULT 3")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS hold_reason VARCHAR")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS raw_data JSONB DEFAULT '{}'")


def downgrade() -> None:
    for col in ["clinical_blob_key", "referring_fax", "external_reference_id",
                "is_stat", "scheduled_dt", "auth_exam_id", "order_request_id",
                "sort_priority", "hold_reason", "raw_data"]:
        op.execute(f"ALTER TABLE cases DROP COLUMN IF EXISTS {col}")
