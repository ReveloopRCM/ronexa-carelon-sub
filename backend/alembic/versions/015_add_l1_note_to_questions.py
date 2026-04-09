"""Add l1_note column to questions table.

Revision ID: 015
Revises: 014
"""
from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"


def upgrade():
    op.add_column("questions", sa.Column("l1_note", sa.String(), nullable=True))


def downgrade():
    op.drop_column("questions", "l1_note")
