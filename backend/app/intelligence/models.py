"""Intelligence layer data models — PortalObservation, TypedDecision, ClinicalContext."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PortalObservation:
    """A question observed from the portal API response (READ seam output)."""
    question_id: str
    group_id: int
    question_text: str
    question_type: int  # 2=date, 3=single-select, 4=multi-select
    options: list[dict]  # [{id, text}]
    cpt_code: str
    sequence: int = 0
    icd1: str | None = None
    carrier_id: str | None = None


@dataclass
class TypedDecision:
    """Sonnet's evaluation of a portal question (DECIDE seam output)."""
    question_id: str
    question_type: int
    group_id: int
    sequence: int = 0
    answer_value: str | list[str] = ""  # UUID(s) for select, date string for date
    measure_unit_id: str | None = None
    measure_unit_values: list[str] | None = None
    confidence: float = 0.0  # 0-100
    evidence: str | None = None  # quote from clinical notes
    gap: str | None = None  # missing documentation description
    reasoning: str | None = None  # one sentence
    # Pathway + chain-of-thought (v2 prompts)
    pathway_rationale: str | None = None
    chain_coherence: str | None = None
    # Dual-answer: notes-honest path
    notes_answer_value: str | list[str] | None = None
    notes_confidence: float | None = None  # legacy — v2 schema omits this
    notes_reasoning: str | None = None  # legacy — v2 schema omits this
    approval_gap: str | None = None

    def to_portal_answer(self) -> dict:
        """Convert to portal answer dict for GetPathwayAssetsWithValidation.

        Portal expects:
          QuestionId, QuestionType, GroupId, Sequence, Values (list)
        """
        # Values must always be a list
        if isinstance(self.answer_value, list):
            values = self.answer_value
        else:
            values = [self.answer_value] if self.answer_value else []

        answer = {
            "QuestionId": self.question_id,
            "QuestionType": self.question_type,
            "GroupId": self.group_id,
            "Sequence": self.sequence,
            "Values": values,
        }
        if self.measure_unit_id:
            answer["MeasureUnitId"] = self.measure_unit_id
        if self.measure_unit_values:
            answer["MeasureUnitValues"] = self.measure_unit_values
        return answer


@dataclass
class ClinicalContext:
    """Structured extraction from clinical notes (Haiku vision output)."""
    patient_name: str | None = None
    dob: str | None = None
    provider_name: str | None = None
    provider_npi: str | None = None
    date_of_service: str | None = None
    chief_complaint: str | None = None
    diagnoses: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    procedures_ordered: list[str] = field(default_factory=list)
    medications: list[str] = field(default_factory=list)
    relevant_history: str | None = None
    clinical_indication: str | None = None
    body_part: str | None = None
    laterality: str | None = None
    prior_treatments: list[str] = field(default_factory=list)
    prior_imaging: list[str] = field(default_factory=list)
    symptom_duration: str | None = None
    raw_text_excerpts: list[str] = field(default_factory=list)
    document_type: str = "UNKNOWN"
    document_quality: str = "CLEAN"
    page_count: int = 0
