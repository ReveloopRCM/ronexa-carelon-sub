from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSON, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# --- Enums ---


class CaseState(str, enum.Enum):
    PENDING_NOTES = "PENDING_NOTES"
    PENDING_STAT = "PENDING_STAT"
    WAITING_CLINICALS = "WAITING_CLINICALS"
    HOLD = "HOLD"
    SUBMISSION_ERROR = "SUBMISSION_ERROR"  # Portal refused at submit step (duplicate modal, criteria, portal error)
    NOTES_UPLOADED = "NOTES_UPLOADED"
    PROCESSING = "PROCESSING"
    IN_REVIEW = "IN_REVIEW"        # Legacy — kept for backward compat
    L1_REVIEW = "L1_REVIEW"        # Waiting for Level 1 rep review
    L2_REVIEW = "L2_REVIEW"        # Waiting for Level 2 senior review
    APPROVED_FOR_SUBMIT = "APPROVED_FOR_SUBMIT"  # DEPRECATED — kept for DB enum compat only
    SUBMITTING = "SUBMITTING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    PENDED = "PENDED"
    PENDED_FAX_REVIEW = "PENDED_FAX_REVIEW"  # Pended, waiting for fax validation before sending
    NO_AUTH_REQUIRED = "NO_AUTH_REQUIRED"    # Portal says no pre-auth needed for this member/plan
    CLINICAL_REVIEW = "CLINICAL_REVIEW"      # Processed via signature replay, awaiting rep verification with clinicals
    ALREADY_WORKED = "ALREADY_WORKED"        # Worked in legacy system, flagged by rep to clear review queue
    ORDER_READY = "ORDER_READY"              # Order form extracted, ready for OrderWorkflow (no clinical notes)
    FAILED = "FAILED"


class ReviewState(str, enum.Enum):
    PENDING = "PENDING"
    AI_SUGGESTED = "AI_SUGGESTED"
    REP_APPROVED = "REP_APPROVED"
    REP_EDITED = "REP_EDITED"
    FLAGGED = "FLAGGED"


class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUSPENDED = "SUSPENDED"      # DEPRECATED — kept for DB enum compat
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobType(str, enum.Enum):
    FIRST_PASS = "FIRST_PASS"    # Job 1: portal navigation + question discovery (with clinical notes)
    SUBMIT = "SUBMIT"            # Job 2: submit with approved answers
    SIGNATURE_REPLAY = "SIGNATURE_REPLAY"  # Job 3: replay algorithm signature for awaiting-clinicals cases
    ORDER = "ORDER"              # Job 4: order-only first pass (no clinical notes, uses order form)


class ExceptionType(str, enum.Enum):
    STAT_PENDED = "STAT_PENDED"
    RPO_NOT_FOUND = "RPO_NOT_FOUND"
    MED_NECESSITY = "MED_NECESSITY"
    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    DUPLICATE_AUTH = "DUPLICATE_AUTH"
    ELIGIBILITY_EXPIRED = "ELIGIBILITY_EXPIRED"
    PHONE_MISSING = "PHONE_MISSING"
    REFERRING_NPI_MISSING = "REFERRING_NPI_MISSING"
    PORTAL_ERROR = "PORTAL_ERROR"
    NO_AUTH_REQUIRED = "NO_AUTH_REQUIRED"
    ALREADY_WORKED = "ALREADY_WORKED"
    FAX_FAILED = "FAX_FAILED"              # RC delivery failed or timed out


class AccountShift(str, enum.Enum):
    DAY = "DAY"
    NIGHT = "NIGHT"


# --- Models ---


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    uploaded_by: Mapped[str] = mapped_column(String, nullable=False)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    unique_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    stat_count: Mapped[int] = mapped_column(Integer, default=0)
    hold_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String, default="excel")

    cases: Mapped[list[Case]] = relationship("Case", back_populates="batch")


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        Index("ix_cases_state", "state"),
        Index("ix_cases_center_npi", "center_npi"),
        Index("ix_cases_carrier_id", "carrier_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    batch_id: Mapped[str | None] = mapped_column(String, ForeignKey("batches.id"), nullable=True)

    # Dedup key
    exam_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)

    # Member search
    first_name: Mapped[str] = mapped_column(String, nullable=False)
    last_name: Mapped[str] = mapped_column(String, nullable=False)
    dob: Mapped[str] = mapped_column(String, nullable=False)
    policy_num: Mapped[str] = mapped_column(String, nullable=False)
    patient_zip: Mapped[str | None] = mapped_column(String, nullable=True)

    # Session pool key
    center_npi: Mapped[str] = mapped_column(String, nullable=False)
    center_abbr: Mapped[str | None] = mapped_column(String, nullable=True)

    # Portal submission fields
    cpt_code: Mapped[str] = mapped_column(String, nullable=False)
    # Laterality + anatomy parsed from Mongo `CPTDesc` at ingest (e.g. "MRI Knee
    # WO - RIGHT" → body_side_desc="Right", body_part_desc="Knee"). Both nullable
    # because many CPTs (brain, abdomen, chest) have no laterality. Used by the
    # compiler at `AddExam` and rendered on the Worklist / case detail page so
    # bilateral exams (same CPT, different side) are distinguishable at a glance.
    body_side_desc: Mapped[str | None] = mapped_column(String, nullable=True)
    body_part_desc: Mapped[str | None] = mapped_column(String, nullable=True)
    icd1: Mapped[str | None] = mapped_column(String, nullable=True)
    icd2: Mapped[str | None] = mapped_column(String, nullable=True)
    icd3: Mapped[str | None] = mapped_column(String, nullable=True)
    icd4: Mapped[str | None] = mapped_column(String, nullable=True)
    icd5: Mapped[str | None] = mapped_column(String, nullable=True)
    referring_npi: Mapped[str | None] = mapped_column(String, nullable=True)
    carrier_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Clinical attachment
    file_key: Mapped[str | None] = mapped_column(String, nullable=True)
    attachment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    clinical_blob_key: Mapped[str | None] = mapped_column(String, nullable=True)

    # Referring provider (from API)
    referring_fax: Mapped[str | None] = mapped_column(String, nullable=True)
    patient_phone: Mapped[str | None] = mapped_column(String, nullable=True)

    # External reference (Mongo _id / API correlation)
    external_reference_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Priority / routing
    is_stat: Mapped[bool] = mapped_column(Boolean, default=False)
    scheduled_dt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # RIS back-reference
    auth_exam_id: Mapped[str | None] = mapped_column(String, nullable=True)
    order_request_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # State + classification
    state: Mapped[CaseState] = mapped_column(
        Enum(CaseState, name="casestate", create_constraint=True),
        nullable=False,
        default=CaseState.PENDING_NOTES,
    )
    sort_priority: Mapped[int] = mapped_column(Integer, default=3)
    hold_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    # Portal session context (populated at processing time)
    portal_provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    portal_client_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Processing results — populated by _mark_case_complete after portal submission
    portal_case_id: Mapped[str | None] = mapped_column(String, nullable=True)
    auth_number: Mapped[str | None] = mapped_column(String, nullable=True)
    determination_status: Mapped[str | None] = mapped_column(String, nullable=True)
    valid_from: Mapped[str | None] = mapped_column(String, nullable=True)
    valid_through: Mapped[str | None] = mapped_column(String, nullable=True)
    denial_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pend_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_pdf_url: Mapped[str | None] = mapped_column(String, nullable=True)

    # Fax delivery tracking (pended cases only). Populated by validate_fax
    # endpoint at send time, updated by the RingCentral status poller.
    # See backend/app/ingest/poll_scheduler.py:_fax_poll_loop.
    fax_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fax_status: Mapped[str | None] = mapped_column(String, nullable=True)           # Queued | Sent | SendingFailed | Timeout
    fax_delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fax_confirmation_pdf_key: Mapped[str | None] = mapped_column(String, nullable=True)  # Azure Blob key for receipt PDF

    # Pathway / clinical scenario — from GetPathwayOptions + SetPathway
    pathway_name: Mapped[str | None] = mapped_column(String, nullable=True)
    pathway_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pathway_options: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{id, text}, ...]

    # Carelon's "Anticipated Determination Date" — distinct from valid_from
    # (which is "Scheduled Date of Service"). This is the date the rep cares
    # about: when Carelon expects to render an auth decision. Captured from
    # the post-submit confirmation page. Populated for PENDED / In-Progress
    # outcomes; NULL for APPROVED (where the auth is already decided).
    determination_date: Mapped[str | None] = mapped_column(String, nullable=True)

    # Rep override — when the rep changes the clinical scenario in review,
    # the next rerun must deterministically force the portal pathway to this
    # id (no LLM selection). Cleared by the compiler exactly when the
    # override is honored. This is the single durable source of truth for
    # the rep's intent — `pathway_id` above gets overwritten by every
    # successful pathway selection, so it can't double as "what the rep
    # wants." See `_save_pathway_to_case(clear_override=True)` for clearing.
    rep_pathway_override_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Approval tracking — from clinical algorithm (RecommendationType) + IsBypassAndGoldCardState
    gold_card_level: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0=none, 2=full bypass
    auto_approved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)   # True when algorithm RecommendationType=3 (Approve)
    approval_type: Mapped[str | None] = mapped_column(String, nullable=True)     # gold_card|algorithm|manual
    algorithm_recommendation: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Raw RecommendationType (3=Approve, 6=InProgress)
    signature_replay: Mapped[bool] = mapped_column(Boolean, default=False)  # True when processed via algorithm signature replay (no clinicals)
    signature_id: Mapped[str | None] = mapped_column(String, nullable=True)  # FK to algorithm_signatures.id if replayed

    # JSONB — everything else from Excel row
    raw_data: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Timestamps
    ingested_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    batch: Mapped[Batch] = relationship("Batch", back_populates="cases")
    questions: Mapped[list[Question]] = relationship("Question", back_populates="case")
    clinical_notes: Mapped[list[ClinicalNote]] = relationship(
        "ClinicalNote", back_populates="case"
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        "AuditEvent", back_populates="case"
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(String, ForeignKey("cases.id"), nullable=False)

    portal_question_id: Mapped[str] = mapped_column(String, nullable=False)
    group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    question_type: Mapped[int] = mapped_column(Integer, nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    options_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # AI decision
    ai_answer: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_gap: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Dual-answer: notes-honest path
    ai_notes_answer: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ai_notes_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_notes_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_approval_gap: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Review
    review_state: Mapped[ReviewState] = mapped_column(
        Enum(ReviewState, name="reviewstate", create_constraint=True),
        nullable=False,
        default=ReviewState.PENDING,
    )
    review_level: Mapped[int] = mapped_column(Integer, default=1)  # 1=L1, 2=L2
    rep_answer: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # L1 review tracking
    l1_reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    l1_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    l1_note: Mapped[str | None] = mapped_column(String, nullable=True)

    # L2 review tracking
    l2_reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    l2_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Legacy (kept for backward compat)
    reviewed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Restate awakeable
    awakeable_id: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    case: Mapped[Case] = relationship("Case", back_populates="questions")


class ClinicalNote(Base):
    __tablename__ = "clinical_notes"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(String, ForeignKey("cases.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    document_type: Mapped[str] = mapped_column(String, default="UNKNOWN")
    document_quality: Mapped[str] = mapped_column(String, default="CLEAN")
    extraction_method: Mapped[str] = mapped_column(String, default="haiku_vision")
    structured: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    case: Mapped[Case] = relationship("Case", back_populates="clinical_notes")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(String, ForeignKey("cases.id"), nullable=False)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    case: Mapped[Case] = relationship("Case", back_populates="audit_events")


class SubmissionJob(Base):
    """Priority queue for portal submissions.

    Workers claim jobs via SELECT FOR UPDATE SKIP LOCKED.
    One job = one case = one portal session.
    """
    __tablename__ = "submission_jobs"
    __table_args__ = (
        Index("ix_jobs_queue", "status", "priority", "created_at"),
        Index("ix_jobs_worker", "claimed_by"),
        Index("ix_jobs_case", "case_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(
        String, ForeignKey("cases.id"), nullable=False, unique=True
    )

    # Priority scoring (higher = more urgent)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    is_stat: Mapped[bool] = mapped_column(Boolean, default=False)

    # Job type — FIRST_PASS (question discovery) or SUBMIT (portal submission)
    job_type: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="FIRST_PASS",
    )

    # Job lifecycle
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="jobstatus", create_constraint=True),
        nullable=False,
        default=JobStatus.QUEUED,
    )
    claimed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Restate correlation
    restate_invocation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    awakeable_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Exception handling
    exception_type: Mapped[ExceptionType | None] = mapped_column(
        Enum(ExceptionType, name="exceptiontype", create_constraint=True),
        nullable=True,
    )
    exception_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Retry tracking
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    # Per-phase max attempts. v138 bumped 2 → 3 after empirical evidence that
    # Carelon's portal flakes (timeouts on slow page renders, transient
    # postback hiccups) cluster around the 1-2 attempt range; 3 gives real
    # buffer with the v138 timeout fix in place. The runaway-loop bound from
    # v136 still applies (claim_next_job WHERE attempt < max_attempts).
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Earliest UTC time at which claim_next_job will pick this job up again.
    # Set on transient retries when we want to give the portal time to recover
    # (typically Carelon-side flakes on the post-eligibility transition where
    # attempts back-to-back all hit the same portal-state window). NULL means
    # immediately claimable. v148: 5-minute cooldown for known portal-flake
    # patterns ("provider search page did not load", "could not select
    # provider", "facility continue failed").
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Timing
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    case: Mapped[Case] = relationship("Case")


class WorkerAccount(Base):
    """Carelon login accounts managed by the system.

    Each account has a day/night shift and is assigned to a container.
    """
    __tablename__ = "worker_accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    password: Mapped[str] = mapped_column(String, nullable=False, default="")

    # Shift assignment
    shift: Mapped[AccountShift] = mapped_column(
        Enum(AccountShift, name="accountshift", create_constraint=True),
        nullable=False,
    )
    container_id: Mapped[str] = mapped_column(String, nullable=False)

    # Worker HTTP API URL (e.g., http://10.0.0.5:9081)
    worker_url: Mapped[str | None] = mapped_column(String, nullable=True)

    # MFA mailbox
    mailbox_address: Mapped[str] = mapped_column(String, nullable=False)

    # State
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    is_logged_in: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_logout_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_case_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Worker loop control
    job_type: Mapped[str] = mapped_column(String, nullable=False, default="FIRST_PASS")
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    wake_awakeable_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Stats
    cases_today: Mapped[int] = mapped_column(Integer, default=0)
    total_cases: Mapped[int] = mapped_column(Integer, default=0)

    # Browser profile path (persistent volume)
    profile_path: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class BypassRule(Base):
    """Auto-bypass rules for CPT+ICD combinations with proven answer patterns."""
    __tablename__ = "bypass_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    cpt_code: Mapped[str] = mapped_column(String, nullable=False)
    icd_code: Mapped[str] = mapped_column(String, nullable=False)
    min_confidence: Mapped[float] = mapped_column(Float, default=85.0)
    bypass_l1: Mapped[bool] = mapped_column(Boolean, default=True)
    bypass_l2: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_count: Mapped[int] = mapped_column(Integer, default=0)
    last_approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_bypass_rules_cpt_icd", "cpt_code", "icd_code", unique=True),
    )


class AutomationRule(Base):
    """Full automation rules — CPT+ICD combos that skip review entirely.

    When enabled, cases matching this CPT+ICD go straight from LLM answers
    to portal submission without L1/L2 review. Toggle is manual, informed
    by submission analytics (success rates over time).
    """
    __tablename__ = "automation_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    cpt_code: Mapped[str] = mapped_column(String, nullable=False)
    icd_code: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled_by: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_automation_rules_cpt_icd", "cpt_code", "icd_code", unique=True),
    )


class SystemSetting(Base):
    """Runtime configuration stored in Postgres, editable from the UI."""
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str] = mapped_column(String, default="system")


class OutcomePattern(Base):
    __tablename__ = "outcome_patterns"
    __table_args__ = (
        Index("ix_outcome_cpt", "cpt_code"),
        Index("ix_outcome_carrier", "carrier_id"),
        Index("ix_outcome_portal", "portal_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(String, nullable=False)
    portal_id: Mapped[str] = mapped_column(String, nullable=False)
    cpt_code: Mapped[str] = mapped_column(String, nullable=False)
    icd1: Mapped[str | None] = mapped_column(String, nullable=True)
    carrier_id: Mapped[str | None] = mapped_column(String, nullable=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[int] = mapped_column(Integer, nullable=False)
    options_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    question_embedding = mapped_column(Vector(1536), nullable=True)
    answer_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    answer_text: Mapped[str] = mapped_column(String, nullable=False)
    evidence_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    was_rep_override: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    denial_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    pathway_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pathway_name: Mapped[str | None] = mapped_column(String, nullable=True)
    is_gold_card: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ExecutionLog(Base):
    """Billing-oriented pipeline event tracker.

    Every pipeline event is a potential billable line item. Fields are
    denormalized so logs remain readable even after the case is flushed.
    This table is NEVER deleted — not even by force flush.
    """
    __tablename__ = "execution_logs"
    __table_args__ = (
        Index("ix_exec_log_event_type", "event_type"),
        Index("ix_exec_log_created", "created_at"),
        Index("ix_exec_log_case", "case_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    case_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("cases.id", ondelete="SET NULL"), nullable=True
    )

    # Denormalized — survives case flush
    exam_id: Mapped[str | None] = mapped_column(String, nullable=True)
    patient_name: Mapped[str | None] = mapped_column(String, nullable=True)
    cpt_code: Mapped[str | None] = mapped_column(String, nullable=True)
    center_abbr: Mapped[str | None] = mapped_column(String, nullable=True)

    # Event classification
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    # RECEIVED, EXTRACTION_CLINICAL, EXTRACTION_ORDER, FIRST_PASS,
    # ORDER_PASS, APPROVED, PENDED, DENIED, SUBMITTED, NO_AUTH

    document_type: Mapped[str | None] = mapped_column(String, nullable=True)
    # CLINICAL, ORDER_FORM

    status: Mapped[str] = mapped_column(String, nullable=False, default="COMPLETED")
    # COMPLETED, FAILED

    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Flexible: page_count, error, auth_number, etc.

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AlgorithmSignature(Base):
    """Stored Q&A sequence from an algorithm-approved case (RecommendationType=3).

    Keyed by (cpt_code, icd1, pathway_id) — one approved signature per combo.
    When a new case with the same combo is awaiting clinicals, we replay this
    signature through the portal to get algorithm approval, then park it in
    CLINICAL_REVIEW for rep verification once clinicals arrive.
    """
    __tablename__ = "algorithm_signatures"
    __table_args__ = (
        Index("ix_algo_sig_cpt_icd", "cpt_code", "icd1"),
        Index("ix_algo_sig_lookup", "cpt_code", "icd1", "pathway_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    cpt_code: Mapped[str] = mapped_column(String, nullable=False)
    icd1: Mapped[str | None] = mapped_column(String, nullable=True)
    carrier_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pathway_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pathway_name: Mapped[str | None] = mapped_column(String, nullable=True)

    # The full ordered Q&A sequence that got RecommendationType=3
    # [{question_id, question_text, question_type, options, answer_value, answer_text, group_id, sequence}, ...]
    qa_sequence: Mapped[list] = mapped_column(JSON, nullable=False)

    # Metadata
    source_case_id: Mapped[str] = mapped_column(String, nullable=False)
    algorithm_recommendation: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
