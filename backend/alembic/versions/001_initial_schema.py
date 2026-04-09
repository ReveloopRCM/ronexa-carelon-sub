"""Initial schema with all models and pgvector

Revision ID: 001
Revises:
Create Date: 2026-03-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Define enums as PostgreSQL ENUM with create_type=False
# We create them manually first via raw SQL
casestate_enum = postgresql.ENUM(
    "PENDING_NOTES", "PENDING_STAT", "HOLD", "NOTES_UPLOADED",
    "PROCESSING", "IN_REVIEW", "SUBMITTING", "APPROVED",
    "DENIED", "PENDED", "FAILED",
    name="casestate", create_type=False,
)
reviewstate_enum = postgresql.ENUM(
    "PENDING", "AI_SUGGESTED", "REP_APPROVED", "REP_EDITED", "FLAGGED",
    name="reviewstate", create_type=False,
)


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Create enums manually
    casestate_enum.create(op.get_bind(), checkfirst=True)
    reviewstate_enum.create(op.get_bind(), checkfirst=True)

    # Batches
    op.create_table(
        "batches",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("uploaded_by", sa.String(), nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("duplicate_rows", sa.Integer(), nullable=False),
        sa.Column("unique_cases", sa.Integer(), nullable=False),
        sa.Column("stat_count", sa.Integer(), default=0),
        sa.Column("hold_count", sa.Integer(), default=0),
        sa.Column("source", sa.String(), default="excel"),
    )

    # Cases
    op.create_table(
        "cases",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("batch_id", sa.String(), sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("exam_id", sa.String(), unique=True, nullable=False),
        sa.Column("first_name", sa.String(), nullable=False),
        sa.Column("last_name", sa.String(), nullable=False),
        sa.Column("dob", sa.String(), nullable=False),
        sa.Column("policy_num", sa.String(), nullable=False),
        sa.Column("patient_zip", sa.String(), nullable=True),
        sa.Column("center_npi", sa.String(), nullable=False),
        sa.Column("center_abbr", sa.String(), nullable=True),
        sa.Column("cpt_code", sa.String(), nullable=False),
        sa.Column("icd1", sa.String(), nullable=True),
        sa.Column("icd2", sa.String(), nullable=True),
        sa.Column("icd3", sa.String(), nullable=True),
        sa.Column("icd4", sa.String(), nullable=True),
        sa.Column("icd5", sa.String(), nullable=True),
        sa.Column("referring_npi", sa.String(), nullable=True),
        sa.Column("carrier_id", sa.String(), nullable=True),
        sa.Column("file_key", sa.String(), nullable=True),
        sa.Column("attachment_id", sa.String(), nullable=True),
        sa.Column("is_stat", sa.Boolean(), default=False),
        sa.Column("scheduled_dt", sa.DateTime(), nullable=True),
        sa.Column("auth_exam_id", sa.String(), nullable=True),
        sa.Column("order_request_id", sa.String(), nullable=True),
        sa.Column("state", casestate_enum, nullable=False),
        sa.Column("sort_priority", sa.Integer(), default=3),
        sa.Column("hold_reason", sa.String(), nullable=True),
        sa.Column("portal_provider_id", sa.String(), nullable=True),
        sa.Column("portal_client_id", sa.String(), nullable=True),
        sa.Column("portal_case_id", sa.String(), nullable=True),
        sa.Column("auth_number", sa.String(), nullable=True),
        sa.Column("denial_reason", sa.Text(), nullable=True),
        sa.Column("pend_reason", sa.Text(), nullable=True),
        sa.Column("raw_data", postgresql.JSONB(), server_default="{}"),
        sa.Column("ingested_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_cases_exam_id", "cases", ["exam_id"], unique=True)
    op.create_index("ix_cases_state", "cases", ["state"])
    op.create_index("ix_cases_center_npi", "cases", ["center_npi"])
    op.create_index("ix_cases_carrier_id", "cases", ["carrier_id"])

    # Questions
    op.create_table(
        "questions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("case_id", sa.String(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("portal_question_id", sa.String(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("question_type", sa.Integer(), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=True),
        sa.Column("ai_answer", sa.JSON(), nullable=True),
        sa.Column("ai_confidence", sa.Float(), nullable=True),
        sa.Column("ai_evidence", sa.Text(), nullable=True),
        sa.Column("ai_reasoning", sa.Text(), nullable=True),
        sa.Column("ai_gap", sa.Text(), nullable=True),
        sa.Column("review_state", reviewstate_enum, nullable=False),
        sa.Column("rep_answer", sa.JSON(), nullable=True),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("awakeable_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # Clinical notes
    op.create_table(
        "clinical_notes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("case_id", sa.String(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("document_type", sa.String(), default="UNKNOWN"),
        sa.Column("document_quality", sa.String(), default="CLEAN"),
        sa.Column("extraction_method", sa.String(), default="haiku_vision"),
        sa.Column("structured", sa.JSON(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # Audit events
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("case_id", sa.String(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), server_default=sa.func.now()),
    )

    # Outcome patterns (RAG)
    op.create_table(
        "outcome_patterns",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("case_id", sa.String(), nullable=False),
        sa.Column("portal_id", sa.String(), nullable=False),
        sa.Column("cpt_code", sa.String(), nullable=False),
        sa.Column("icd1", sa.String(), nullable=True),
        sa.Column("carrier_id", sa.String(), nullable=True),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("question_type", sa.Integer(), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=True),
        sa.Column("question_embedding", Vector(1536), nullable=True),
        sa.Column("answer_value", sa.JSON(), nullable=True),
        sa.Column("answer_text", sa.String(), nullable=False),
        sa.Column("evidence_text", sa.Text(), nullable=True),
        sa.Column("was_rep_override", sa.Boolean(), default=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("denial_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_outcome_cpt", "outcome_patterns", ["cpt_code"])
    op.create_index("ix_outcome_carrier", "outcome_patterns", ["carrier_id"])
    op.create_index("ix_outcome_portal", "outcome_patterns", ["portal_id"])


def downgrade() -> None:
    op.drop_table("outcome_patterns")
    op.drop_table("audit_events")
    op.drop_table("clinical_notes")
    op.drop_table("questions")
    op.drop_table("cases")
    op.drop_table("batches")
    reviewstate_enum.drop(op.get_bind(), checkfirst=True)
    casestate_enum.drop(op.get_bind(), checkfirst=True)
    op.execute("DROP EXTENSION IF EXISTS vector")
