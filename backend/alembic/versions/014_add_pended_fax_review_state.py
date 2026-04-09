"""Add PENDED_FAX_REVIEW to casestate enum.

Revision ID: 014
Revises: 013
"""
from alembic import op

revision = "014"
down_revision = "013"


def upgrade():
    op.execute("ALTER TYPE casestate ADD VALUE IF NOT EXISTS 'PENDED_FAX_REVIEW' AFTER 'PENDED'")


def downgrade():
    # PostgreSQL doesn't support removing enum values
    pass
