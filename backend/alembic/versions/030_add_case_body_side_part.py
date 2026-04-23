"""Add body_side_desc + body_part_desc to cases.

Bilateral exams (CPT 73721 etc.) share one CPT across left/right/bilateral
submissions. We parse the side + anatomy from Mongo's `CPTDesc` at ingest
(e.g. "MRI Knee WO - RIGHT" → body_side_desc="Right", body_part_desc="Knee")
and persist them so reps can distinguish the two cases on the Worklist and
the compiler can hand the portal the correct `BodySideCode`/`BodyPartCode`
at `AddExam` time.

Revision ID: 030
Revises: 029
Create Date: 2026-04-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("body_side_desc", sa.String(), nullable=True))
    op.add_column("cases", sa.Column("body_part_desc", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("cases", "body_part_desc")
    op.drop_column("cases", "body_side_desc")
