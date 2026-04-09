"""Seed LLM model selection and prompt template settings

Revision ID: 004
Revises: 003
Create Date: 2026-03-23
"""
from typing import Sequence, Union

from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # LLM model/provider settings
    op.execute("""
        INSERT INTO system_settings (key, value, updated_by) VALUES
        ('llm_eval_provider', '"anthropic"', 'system'),
        ('llm_eval_model', '"claude-sonnet-4-6"', 'system'),
        ('llm_extract_provider', '"anthropic"', 'system'),
        ('llm_extract_model', '"claude-haiku-4-5-20251001"', 'system'),
        ('llm_embed_model', '"text-embedding-3-small"', 'system'),
        ('api_key_anthropic', '""', 'system'),
        ('api_key_google', '""', 'system'),
        ('api_key_openai', '""', 'system')
        ON CONFLICT (key) DO NOTHING
    """)

    # Prompt templates — stored as JSON strings (the template text itself)
    # We use 'null' as sentinel meaning "use hardcoded default"
    op.execute("""
        INSERT INTO system_settings (key, value, updated_by) VALUES
        ('prompt_eval_system', 'null', 'system'),
        ('prompt_eval_user', 'null', 'system'),
        ('prompt_extract_system', 'null', 'system'),
        ('prompt_extract_user', 'null', 'system')
        ON CONFLICT (key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("""
        DELETE FROM system_settings WHERE key IN (
            'llm_eval_provider', 'llm_eval_model',
            'llm_extract_provider', 'llm_extract_model',
            'llm_embed_model',
            'api_key_anthropic', 'api_key_google', 'api_key_openai',
            'prompt_eval_system', 'prompt_eval_user',
            'prompt_extract_system', 'prompt_extract_user'
        )
    """)
