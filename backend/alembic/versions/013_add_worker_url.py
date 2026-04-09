"""Add worker_url column to worker_accounts table.

Revision ID: 013
Revises: 012
"""
from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"


def upgrade():
    op.add_column("worker_accounts", sa.Column("worker_url", sa.String(), nullable=True))


def downgrade():
    op.drop_column("worker_accounts", "worker_url")
