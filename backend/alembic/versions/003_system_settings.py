"""Add system_settings table with default polling/flush config

Revision ID: 003
Revises: 002
Create Date: 2026-03-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(), server_default="system"),
    )

    # Seed defaults
    op.execute("""
        INSERT INTO system_settings (key, value, updated_by) VALUES
        ('polling_enabled', 'false', 'system'),
        ('polling_interval_minutes', '15', 'system'),
        ('polling_extract', 'true', 'system'),
        ('polling_limit', '500', 'system'),
        ('flush_enabled', 'false', 'system'),
        ('flush_time', '"04:00"', 'system'),
        ('last_sync_at', 'null', 'system'),
        ('last_sync_result', 'null', 'system'),
        ('last_flush_at', 'null', 'system'),
        ('last_flush_result', 'null', 'system'),
        ('sync_history', '[]', 'system')
    """)


def downgrade() -> None:
    op.drop_table("system_settings")
