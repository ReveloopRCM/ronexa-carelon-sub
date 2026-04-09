from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://ronexa:dev_password@localhost:5432/ronexa"

    # Redis
    REDIS_URL: str = "redis://localhost:6379"

    # Restate
    RESTATE_URL: str = "http://localhost:8080"
    RESTATE_ADMIN_URL: str = "http://localhost:9070"

    # Portal credentials
    CARELON_USERNAME: str = ""
    CARELON_PASSWORD: str = ""
    CARELON_BASE_URL: str = "https://www.providerportal.com"
    CARELON_REDIRECT_URI: str = "https://www.providerportal.com/interaction-code/callback"
    CARELON_AUTHENTICATOR_ID: str = "aut5t66pqwcf91LGc4h7"

    # Okta
    OKTA_CLIENT_ID: str = "0oa97idukvkwwIVul4h7"
    OKTA_DOMAIN: str = "login.mbm.partners.carelon.com"

    # MFA — Graph API
    GRAPH_TENANT_ID: str = ""
    GRAPH_CLIENT_ID: str = ""
    GRAPH_CLIENT_SECRET: str = ""
    GRAPH_MAILBOX: str = ""
    MFA_EMAIL_SUBJECT: str = "One-time verification code"

    # LLM
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""

    # Local paths
    BROWSER_PROFILES_DIR: str = "./profiles"
    HEADLESS: str = "true"  # "false" to show browser (for RDP monitoring)
    PORTALS_DIR: str = "./portals"

    # Worker
    CENTER_NPI: str = ""
    PORTAL_ID: str = "carelon_provider_portal"

    # Environment
    ENVIRONMENT: str = "local"

    # Phase 1 flag
    REQUIRE_REP_REVIEW: bool = True

    # Envision — MongoDB (Cosmos DB) + Azure Blob Storage
    MONGO_URI: str = ""
    MONGO_DB: str = "workflowdb"
    MONGO_COLLECTION: str = "auth-submissions"
    AZURE_BLOB_CONNECTION_STRING: str = ""
    AZURE_BLOB_CONTAINER: str = "carelon-attachments"

    # Azure AI Document Intelligence (OCR for clinical PDFs)
    AZURE_DOC_INTELLIGENCE_ENDPOINT: str = ""
    AZURE_DOC_INTELLIGENCE_KEY: str = ""

    # RingCentral (fax for clinical documentation)
    RC_CLIENT_ID: str = ""
    RC_CLIENT_SECRET: str = ""
    RC_JWT_TOKEN: str = ""
    RC_SERVER_URL: str = "https://platform.ringcentral.com"

    # Authentication
    RONEXA_API_URL: str = "https://api-v2.ronexa.com"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD_HASH: str = ""  # bcrypt hash — generate with: python -c "import bcrypt; print(bcrypt.hashpw(b'yourpass', bcrypt.gensalt()).decode())"
    JWT_SECRET: str = ""  # Falls back to SETTINGS_ENCRYPTION_KEY if empty

    model_config = {"env_file": ".env", "extra": "ignore"}

    def model_post_init(self, __context) -> None:
        """Reload keys from .env when shell environment has empty overrides.

        pydantic-settings gives env vars priority over .env files, so
        ANTHROPIC_API_KEY='' in the shell beats the real key in .env.
        This re-reads .env for any key that ended up empty.
        """
        import os
        from dotenv import dotenv_values

        env_values = dotenv_values(".env")
        for field_name in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"]:
            if not getattr(self, field_name, "") and env_values.get(field_name):
                object.__setattr__(self, field_name, env_values[field_name])

    # --- LLM provider routing ---
    # Evaluator and extractor call these directly by provider name.
    # Failover logic lives in the evaluator, not here.

    def get_anthropic_client(self):
        import anthropic
        return anthropic.AsyncAnthropic(api_key=self.ANTHROPIC_API_KEY)

    def get_anthropic_model(self) -> str:
        return "claude-sonnet-4-6"

    def get_gemini_model(self) -> str:
        return "gemini-2.5-flash"

    def get_extraction_model(self) -> str:
        """Model for Haiku vision extraction (PDF → ClinicalContext)."""
        if self.ANTHROPIC_API_KEY:
            return "claude-haiku-4-5-20251001"
        if self.GOOGLE_API_KEY:
            return "gemini-2.5-flash"
        raise RuntimeError("No LLM API key configured for extraction")

    def get_extraction_client(self):
        """Client for Haiku vision extraction."""
        if self.ANTHROPIC_API_KEY:
            return self.get_anthropic_client()
        if self.GOOGLE_API_KEY:
            from google import genai
            return genai.Client(api_key=self.GOOGLE_API_KEY)
        raise RuntimeError("No LLM API key configured for extraction")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
