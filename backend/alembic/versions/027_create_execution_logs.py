"""Create execution_logs table for billing-oriented pipeline event tracking.

Every pipeline event (ingestion, extraction, processing, submission, outcome)
is logged here. Table is NEVER flushed — it's the billing data source.

Revision ID: 027
Revises: 026
Create Date: 2026-04-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "execution_logs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("case_id", sa.String(), sa.ForeignKey("cases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("exam_id", sa.String(), nullable=True),
        sa.Column("patient_name", sa.String(), nullable=True),
        sa.Column("cpt_code", sa.String(), nullable=True),
        sa.Column("center_abbr", sa.String(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("document_type", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="COMPLETED"),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_exec_log_event_type", "execution_logs", ["event_type"])
    op.create_index("ix_exec_log_created", "execution_logs", ["created_at"])
    op.create_index("ix_exec_log_case", "execution_logs", ["case_id"])


def downgrade() -> None:
    op.drop_table("execution_logs")
