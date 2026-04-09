"""Add submission_jobs and worker_accounts tables

Revision ID: 002
Revises: 001
Create Date: 2026-03-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Enums
jobstatus_enum = postgresql.ENUM(
    "QUEUED", "CLAIMED", "RUNNING", "SUSPENDED",
    "COMPLETED", "FAILED", "CANCELLED",
    name="jobstatus", create_type=False,
)
exceptiontype_enum = postgresql.ENUM(
    "STAT_PENDED", "RPO_NOT_FOUND", "MED_NECESSITY",
    "MEMBER_NOT_FOUND", "DUPLICATE_AUTH", "PORTAL_ERROR",
    name="exceptiontype", create_type=False,
)
accountshift_enum = postgresql.ENUM(
    "DAY", "NIGHT",
    name="accountshift", create_type=False,
)


def upgrade() -> None:
    # Create enums
    op.execute("CREATE TYPE jobstatus AS ENUM ('QUEUED','CLAIMED','RUNNING','SUSPENDED','COMPLETED','FAILED','CANCELLED')")
    op.execute("CREATE TYPE exceptiontype AS ENUM ('STAT_PENDED','RPO_NOT_FOUND','MED_NECESSITY','MEMBER_NOT_FOUND','DUPLICATE_AUTH','PORTAL_ERROR')")
    op.execute("CREATE TYPE accountshift AS ENUM ('DAY','NIGHT')")

    # Add WAITING_CLINICALS to casestate if missing (was added after initial migration)
    op.execute("""
        DO $$ BEGIN
            ALTER TYPE casestate ADD VALUE IF NOT EXISTS 'WAITING_CLINICALS' AFTER 'HOLD';
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)

    # submission_jobs
    op.create_table(
        "submission_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("case_id", sa.String(), sa.ForeignKey("cases.id"), nullable=False, unique=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("is_stat", sa.Boolean(), server_default="false"),
        sa.Column("status", jobstatus_enum, nullable=False, server_default="QUEUED"),
        sa.Column("claimed_by", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("restate_invocation_id", sa.String(), nullable=True),
        sa.Column("awakeable_id", sa.String(), nullable=True),
        sa.Column("exception_type", exceptiontype_enum, nullable=True),
        sa.Column("exception_detail", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default="0"),
        sa.Column("max_attempts", sa.Integer(), server_default="3"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_jobs_queue", "submission_jobs", ["status", "priority", "created_at"])
    op.create_index("ix_jobs_worker", "submission_jobs", ["claimed_by"])
    op.create_index("ix_jobs_case", "submission_jobs", ["case_id"])

    # worker_accounts
    op.create_table(
        "worker_accounts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("username", sa.String(), unique=True, nullable=False),
        sa.Column("shift", accountshift_enum, nullable=False),
        sa.Column("container_id", sa.String(), nullable=False),
        sa.Column("mailbox_address", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="false"),
        sa.Column("is_logged_in", sa.Boolean(), server_default="false"),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column("last_logout_at", sa.DateTime(), nullable=True),
        sa.Column("current_case_id", sa.String(), nullable=True),
        sa.Column("cases_today", sa.Integer(), server_default="0"),
        sa.Column("total_cases", sa.Integer(), server_default="0"),
        sa.Column("profile_path", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("worker_accounts")
    op.drop_table("submission_jobs")
    op.execute("DROP TYPE IF EXISTS accountshift")
    op.execute("DROP TYPE IF EXISTS exceptiontype")
    op.execute("DROP TYPE IF EXISTS jobstatus")
