"""PortalDNA Pydantic models — declarative portal descriptor schema.

PortalDNA is the JSON descriptor that defines a portal's state machine.
The compiler reads it and drives the submission. No portal-specific logic
lives in the engine.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class MFAConfig(BaseModel):
    type: str = "graph_api"
    mailbox_env: str = "GRAPH_MAILBOX"
    poll_interval_ms: int = 3000
    max_attempts: int = 20
    subject_pattern: str = ""
    otp_regex: str = r"\d{6}"


class PostLoginStep(BaseModel):
    action: Literal["click", "wait", "navigate", "api_call"]
    selector: str | None = None
    url: str | None = None
    endpoint: str | None = None
    payload: dict[str, Any] | None = None
    wait_ms: int | None = None
    description: str = ""


class AuthProfile(BaseModel):
    type: Literal["okta_idx", "direct", "saml"] = "okta_idx"
    okta_domain_env: str = "OKTA_DOMAIN"
    client_id_env: str = "OKTA_CLIENT_ID"
    username_env: str = "CARELON_USERNAME"
    password_env: str = "CARELON_PASSWORD"
    mfa: MFAConfig = Field(default_factory=MFAConfig)
    post_login_steps: list[PostLoginStep] = Field(default_factory=list)


class PhaseStep(BaseModel):
    """A single step within a navigation phase."""
    type: Literal["api_call", "dom_action", "extract", "condition", "wait"]
    endpoint: str | None = None
    method: str = "POST"
    payload_template: dict[str, Any] | None = None
    selector: str | None = None
    action: str | None = None  # click, type, select
    value_template: str | None = None
    extract_to: str | None = None  # variable name to store result
    extract_path: str | None = None  # JSONPath into response
    condition: str | None = None  # expression to evaluate
    on_empty: str | None = None  # HANDLE seam action
    wait_ms: int | None = None
    description: str = ""


class NavigationPhase(BaseModel):
    """A phase in the portal submission flow."""
    id: str
    type: Literal["API_SEQUENCE", "WEBFORM", "RECURSIVE_STATE_MACHINE"]
    description: str = ""
    steps: list[PhaseStep] = Field(default_factory=list)
    # For RECURSIVE_STATE_MACHINE (CLINICAL_TREE)
    loop_endpoint: str | None = None
    delete_endpoint: str | None = None
    done_endpoint: str | None = None
    # Output capture
    output_fields: dict[str, str] = Field(default_factory=dict)


class StateMachine(BaseModel):
    phases: list[str]  # Ordered phase IDs


class RAGConfig(BaseModel):
    enabled: bool = True
    top_k: int = 5
    min_similarity: float = 0.75
    pre_filter_fields: list[str] = Field(default_factory=lambda: ["cpt_code", "carrier_id"])


class DecideSeam(BaseModel):
    """Configuration for the DECIDE intelligence seam."""
    model_env: str = "evaluation"  # maps to settings.get_evaluation_model()
    max_tokens: int = 500
    temperature: float = 0.1
    rag: RAGConfig = Field(default_factory=RAGConfig)


class IntelligenceSeams(BaseModel):
    decide: DecideSeam = Field(default_factory=DecideSeam)


class BotDetectionProfile(BaseModel):
    name: str = "akamai"
    api_calls_exempt: bool = True  # In-browser fetch() bypasses WAF
    page_loads_monitored: bool = True
    retry_on_challenge: bool = True
    max_challenge_retries: int = 3


class PortalDNA(BaseModel):
    """Root descriptor for a portal's automation configuration."""
    portal_id: str
    portal_name: str
    portal_url: str
    version: str = "1.0.0"

    auth: AuthProfile = Field(default_factory=AuthProfile)
    bot_detection: BotDetectionProfile = Field(default_factory=BotDetectionProfile)
    intelligence: IntelligenceSeams = Field(default_factory=IntelligenceSeams)
    state_machine: StateMachine
    navigation_phases: list[NavigationPhase]

    @classmethod
    def load(cls, path: str | Path) -> PortalDNA:
        """Load PortalDNA from a JSON file."""
        with open(path) as f:
            data = json.load(f)
        return cls.model_validate(data)

    def get_phase(self, phase_id: str) -> NavigationPhase:
        """Get a navigation phase by ID."""
        for phase in self.navigation_phases:
            if phase.id == phase_id:
                return phase
        raise ValueError(f"Phase '{phase_id}' not found in {self.portal_id}")
