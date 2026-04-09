"""Add password column to worker_accounts table.

Revision ID: 011
Revises: 010
"""
from alembic import op
import sqlalchemy as sa

revision = "011"
down_revision = "010"


def upgrade() -> None:
    op.add_column("worker_accounts", sa.Column("password", sa.String(), nullable=False, server_default=""))
