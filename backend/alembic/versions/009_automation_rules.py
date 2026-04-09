"""Add automation_rules table for full CPT+ICD automation toggles.

Revision ID: 009
Revises: 008
"""
revision = "009"
down_revision = "008"

from alembic import op
import sqlalchemy as sa


def upgrade():
    op.create_table(
        "automation_rules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("cpt_code", sa.String(), nullable=False),
        sa.Column("icd_code", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), default=False, nullable=False),
        sa.Column("enabled_by", sa.String(), nullable=True),
        sa.Column("enabled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_automation_rules_cpt_icd",
        "automation_rules",
        ["cpt_code", "icd_code"],
        unique=True,
    )


def downgrade():
    op.drop_index("ix_automation_rules_cpt_icd")
    op.drop_table("automation_rules")
