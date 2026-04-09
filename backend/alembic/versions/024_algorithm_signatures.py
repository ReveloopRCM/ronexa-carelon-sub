"""Add algorithm_signatures table, CLINICAL_REVIEW state, signature_replay fields.

Revision ID: 024
Revises: 023
Create Date: 2026-04-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- New enum value: CLINICAL_REVIEW --
    op.execute("ALTER TYPE casestate ADD VALUE IF NOT EXISTS 'CLINICAL_REVIEW'")

    # -- New table: algorithm_signatures --
    op.execute("""
        CREATE TABLE IF NOT EXISTS algorithm_signatures (
            id VARCHAR NOT NULL PRIMARY KEY,
            cpt_code VARCHAR NOT NULL,
            icd1 VARCHAR,
            carrier_id VARCHAR,
            pathway_id VARCHAR,
            pathway_name VARCHAR,
            qa_sequence JSON NOT NULL,
            source_case_id VARCHAR NOT NULL,
            algorithm_recommendation INTEGER NOT NULL DEFAULT 3,
            times_used INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_algo_sig_cpt_icd ON algorithm_signatures(cpt_code, icd1)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_algo_sig_lookup ON algorithm_signatures(cpt_code, icd1, pathway_id)")

    # -- New columns on cases --
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS signature_replay BOOLEAN DEFAULT FALSE")
    op.execute("ALTER TABLE cases ADD COLUMN IF NOT EXISTS signature_id VARCHAR")


def downgrade() -> None:
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS signature_id")
    op.execute("ALTER TABLE cases DROP COLUMN IF EXISTS signature_replay")
    op.execute("DROP TABLE IF EXISTS algorithm_signatures")
    # Note: Cannot remove enum values in PostgreSQL without recreating the type
