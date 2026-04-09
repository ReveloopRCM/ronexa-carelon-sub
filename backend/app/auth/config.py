"""Multi-LLM provider config + failover.

Provider routing:
  Primary:   Anthropic (Sonnet for eval, Haiku for extraction)
  Fallback:  Google Gemini (gemini-2.5-flash eval, gemini-2.0-flash extraction)

Failover triggers on anthropic.APIStatusError (5xx) or timeout.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"
    GOOGLE = "google"


@dataclass
class LLMConfig:
    """Configuration for an LLM provider."""
    provider: LLMProvider
    evaluation_model: str
    extraction_model: str
    api_key_env: str


# Provider configurations — order matters for failover
PROVIDER_CHAIN = [
    LLMConfig(
        provider=LLMProvider.ANTHROPIC,
        evaluation_model="claude-sonnet-4-6",
        extraction_model="claude-haiku-4-5-20251001",
        api_key_env="ANTHROPIC_API_KEY",
    ),
    LLMConfig(
        provider=LLMProvider.GOOGLE,
        evaluation_model="gemini-2.5-flash",
        extraction_model="gemini-2.0-flash",
        api_key_env="GOOGLE_API_KEY",
    ),
]


def get_active_provider(api_keys: dict[str, str]) -> LLMConfig | None:
    """Return the first provider with a valid API key."""
    for config in PROVIDER_CHAIN:
        key = api_keys.get(config.api_key_env, "")
        if key:
            return config
    return None
