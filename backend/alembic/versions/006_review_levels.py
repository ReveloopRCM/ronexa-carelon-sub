"""Add two-level review system + bypass rules

Revision ID: 006
Revises: 005
Create Date: 2026-03-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add L1_REVIEW, L2_REVIEW to casestate enum
    op.execute("""
        DO $$ BEGIN
            ALTER TYPE casestate ADD VALUE IF NOT EXISTS 'L1_REVIEW' AFTER 'IN_REVIEW';
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            ALTER TYPE casestate ADD VALUE IF NOT EXISTS 'L2_REVIEW' AFTER 'L1_REVIEW';
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
    """)

    # Add review level fields to questions table
    op.add_column("questions", sa.Column("review_level", sa.Integer(), server_default="1"))
    op.add_column("questions", sa.Column("l1_reviewed_by", sa.String(), nullable=True))
    op.add_column("questions", sa.Column("l1_reviewed_at", sa.DateTime(), nullable=True))
    op.add_column("questions", sa.Column("l2_reviewed_by", sa.String(), nullable=True))
    op.add_column("questions", sa.Column("l2_reviewed_at", sa.DateTime(), nullable=True))

    # Create bypass_rules table
    op.create_table(
        "bypass_rules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("cpt_code", sa.String(), nullable=False),
        sa.Column("icd_code", sa.String(), nullable=False),
        sa.Column("min_confidence", sa.Float(), server_default="85.0"),
        sa.Column("bypass_l1", sa.Boolean(), server_default="true"),
        sa.Column("bypass_l2", sa.Boolean(), server_default="false"),
        sa.Column("approved_count", sa.Integer(), server_default="0"),
        sa.Column("last_approved_at", sa.DateTime(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="false"),
        sa.Column("created_by", sa.String(), server_default="system"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_bypass_rules_cpt_icd", "bypass_rules",
        ["cpt_code", "icd_code"], unique=True,
    )

    # Add review settings
    op.execute("""
        INSERT INTO system_settings (key, value, updated_by) VALUES
        ('l1_review_enabled', 'true', 'system'),
        ('l2_review_enabled', 'true', 'system'),
        ('bypass_enabled', 'false', 'system')
        ON CONFLICT (key) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_index("ix_bypass_rules_cpt_icd")
    op.drop_table("bypass_rules")
    op.drop_column("questions", "l2_reviewed_at")
    op.drop_column("questions", "l2_reviewed_by")
    op.drop_column("questions", "l1_reviewed_at")
    op.drop_column("questions", "l1_reviewed_by")
    op.drop_column("questions", "review_level")
