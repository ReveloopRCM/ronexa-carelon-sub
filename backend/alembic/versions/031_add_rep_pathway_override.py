"""Add rep_pathway_override_id to cases.

When a rep changes the clinical scenario (pathway) in review, this column
holds the rep's chosen pathway_id as a one-way instruction to the next
rerun: "deterministically force the portal pathway to this id, run a
clean-slate first pass." The compiler clears it transactionally with
`_save_pathway_to_case` after the override has been honored.

This is the single durable source of truth for the rep's pathway intent.
`pathway_id` (existing column) is overwritten by every successful pathway
selection so it cannot double as "what the rep wants."

Revision ID: 031
Revises: 030
Create Date: 2026-04-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("rep_pathway_override_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cases", "rep_pathway_override_id")
