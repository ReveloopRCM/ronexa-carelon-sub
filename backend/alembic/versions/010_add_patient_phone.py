"""Add patient_phone column to cases table.

Revision ID: 010
Revises: 009
"""
from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"


def upgrade() -> None:
    op.add_column("cases", sa.Column("patient_phone", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("cases", "patient_phone")
