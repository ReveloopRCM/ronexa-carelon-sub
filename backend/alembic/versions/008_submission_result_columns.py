"""Add submission result columns to cases table

Revision ID: 008
Revises: 007
Create Date: 2026-03-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add columns for portal submission extraction results
    op.add_column("cases", sa.Column("determination_status", sa.String(), nullable=True))
    op.add_column("cases", sa.Column("valid_from", sa.String(), nullable=True))
    op.add_column("cases", sa.Column("valid_through", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("cases", "valid_through")
    op.drop_column("cases", "valid_from")
    op.drop_column("cases", "determination_status")
