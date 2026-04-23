"""Add fax-tracking columns to cases table.

Enables the RingCentral fax-status poller to record the final delivery
state (Sent/Failed/Timeout), the delivered timestamp, and a blob key
for the generated Fax Confirmation PDF.

Revision ID: 028
Revises: 027
Create Date: 2026-04-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "028"
down_revision: Union[str, None] = "027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("fax_message_id", sa.String(), nullable=True),
    )
    op.add_column(
        "cases",
        sa.Column("fax_status", sa.String(), nullable=True),
    )
    op.add_column(
        "cases",
        sa.Column("fax_delivered_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "cases",
        sa.Column("fax_confirmation_pdf_key", sa.String(), nullable=True),
    )
    # Partial index to speed up the poller query (find still-Queued faxes)
    op.create_index(
        "ix_cases_fax_polling",
        "cases",
        ["fax_message_id", "fax_status"],
        postgresql_where=sa.text("fax_message_id IS NOT NULL AND (fax_status IS NULL OR fax_status = 'Queued')"),
    )


def downgrade() -> None:
    op.drop_index("ix_cases_fax_polling", table_name="cases")
    op.drop_column("cases", "fax_confirmation_pdf_key")
    op.drop_column("cases", "fax_delivered_at")
    op.drop_column("cases", "fax_status")
    op.drop_column("cases", "fax_message_id")
