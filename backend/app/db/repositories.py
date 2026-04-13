from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditEvent,
    Batch,
    Case,
    CaseState,
    ClinicalNote,
    ExecutionLog,
    Question,
    ReviewState,
)


# --- Batch ---


async def create_batch(db: AsyncSession, **kwargs) -> Batch:
    batch = Batch(**kwargs)
    db.add(batch)
    await db.flush()
    return batch


async def get_batch(db: AsyncSession, batch_id: str) -> Batch | None:
    return await db.get(Batch, batch_id)


async def list_batches(db: AsyncSession) -> Sequence[Batch]:
    result = await db.execute(select(Batch).order_by(Batch.uploaded_at.desc()))
    return result.scalars().all()


# --- Case ---


async def create_cases_bulk(db: AsyncSession, cases: list[dict]) -> list[Case]:
    objs = [Case(**c) for c in cases]
    db.add_all(objs)
    await db.flush()
    return objs


async def get_case(db: AsyncSession, case_id: str) -> Case | None:
    return await db.get(Case, case_id)


async def get_case_by_exam_id(db: AsyncSession, exam_id: str) -> Case | None:
    result = await db.execute(select(Case).where(Case.exam_id == exam_id))
    return result.scalar_one_or_none()


async def list_cases(
    db: AsyncSession,
    state: CaseState | None = None,
    center_npi: str | None = None,
    batch_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> Sequence[Case]:
    q = select(Case)
    if state:
        q = q.where(Case.state == state)
    if center_npi:
        q = q.where(Case.center_npi == center_npi)
    if batch_id:
        q = q.where(Case.batch_id == batch_id)
    q = q.order_by(Case.sort_priority, Case.scheduled_dt.asc().nulls_last(), Case.ingested_at)
    q = q.limit(limit).offset(offset)
    result = await db.execute(q)
    return result.scalars().all()


async def update_case_state(
    db: AsyncSession, case_id: str, state: CaseState, **extra
) -> None:
    values = {"state": state, "updated_at": datetime.utcnow(), **extra}
    await db.execute(update(Case).where(Case.id == case_id).values(**values))


# --- Question ---


async def create_question(db: AsyncSession, **kwargs) -> Question:
    q = Question(**kwargs)
    db.add(q)
    await db.flush()
    return q


async def get_questions_for_case(
    db: AsyncSession, case_id: str
) -> Sequence[Question]:
    result = await db.execute(
        select(Question)
        .where(Question.case_id == case_id)
        .order_by(Question.sequence)
    )
    return result.scalars().all()


async def get_pending_review_question(
    db: AsyncSession, case_id: str
) -> Question | None:
    result = await db.execute(
        select(Question)
        .where(Question.case_id == case_id)
        .where(Question.review_state == ReviewState.AI_SUGGESTED)
        .order_by(Question.sequence)
        .limit(1)
    )
    return result.scalar_one_or_none()


# --- ClinicalNote ---


async def create_clinical_note(db: AsyncSession, **kwargs) -> ClinicalNote:
    note = ClinicalNote(**kwargs)
    db.add(note)
    await db.flush()
    return note


async def get_notes_for_case(
    db: AsyncSession, case_id: str
) -> Sequence[ClinicalNote]:
    result = await db.execute(
        select(ClinicalNote).where(ClinicalNote.case_id == case_id)
    )
    return result.scalars().all()


# --- AuditEvent ---


async def create_audit_event(db: AsyncSession, **kwargs) -> AuditEvent:
    event = AuditEvent(**kwargs)
    db.add(event)
    await db.flush()
    return event


async def get_audit_trail(
    db: AsyncSession, case_id: str
) -> Sequence[AuditEvent]:
    result = await db.execute(
        select(AuditEvent)
        .where(AuditEvent.case_id == case_id)
        .order_by(AuditEvent.timestamp)
    )
    return result.scalars().all()


# --- ExecutionLog (billing events) ---


async def create_execution_log(
    db: AsyncSession,
    event_type: str,
    status: str = "COMPLETED",
    case: Case | None = None,
    case_id: str | None = None,
    document_type: str | None = None,
    detail: dict | None = None,
) -> ExecutionLog:
    """Create a billing-oriented execution log entry.

    Denormalizes case fields at write time so logs survive flush.
    """
    log = ExecutionLog(
        case_id=case.id if case else case_id,
        exam_id=case.exam_id if case else None,
        patient_name=f"{case.last_name}, {case.first_name}" if case else None,
        cpt_code=case.cpt_code if case else None,
        center_abbr=case.center_abbr if case else None,
        event_type=event_type,
        document_type=document_type,
        status=status,
        detail=detail,
    )
    db.add(log)
    await db.flush()
    return log


# --- Queue (IN_REVIEW cases) ---


async def list_review_queue(
    db: AsyncSession,
    limit: int = 50,
    states: list[CaseState] | None = None,
) -> Sequence[Case]:
    if states is None:
        states = [CaseState.L1_REVIEW, CaseState.L2_REVIEW]
    result = await db.execute(
        select(Case)
        .where(Case.state.in_(states))
        .order_by(Case.sort_priority, Case.ingested_at)
        .limit(limit)
    )
    return result.scalars().all()
