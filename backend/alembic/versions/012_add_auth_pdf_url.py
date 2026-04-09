"""Add auth_pdf_url column to cases table.

Revision ID: 012
Revises: 011
"""
from alembic import op
import sqlalchemy as sa

revision = "012"
down_revision = "011"


def upgrade():
    op.add_column("cases", sa.Column("auth_pdf_url", sa.String(), nullable=True))


def downgrade():
    op.drop_column("cases", "auth_pdf_url")
