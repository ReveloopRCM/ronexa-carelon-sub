"""Submission job queue — Postgres-backed priority queue.

Workers claim jobs via SELECT FOR UPDATE SKIP LOCKED.
This ensures exactly-once delivery without external message brokers.
"""
from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Sequence

from sqlalchemy import select, update, func, text, case as sql_case
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Case,
    CaseState,
    ExceptionType,
    JobStatus,
    JobType,
    SubmissionJob,
    WorkerAccount,
)

logger = logging.getLogger(__name__)


# ── Priority Scoring ──


def compute_priority(case: Case) -> int:
    """Score a case for queue ordering. Higher = more urgent."""
    if case.is_stat:
        return 1000

    today = date.today()

    # Check date of service proximity
    dos = None
    if case.scheduled_dt:
        dos = case.scheduled_dt.date() if isinstance(case.scheduled_dt, datetime) else case.scheduled_dt

    if dos:
        if dos <= today:
            return 500   # Same-day or past-due
        days_out = (dos - today).days
        if days_out == 1:
            return 200   # Next-day
        if days_out <= 7:
            return 100   # Within a week
    return 50             # Standard / future


async def enqueue_case(db: AsyncSession, case_id: str) -> SubmissionJob:
    """Create a submission job for a case. Idempotent — skips if job exists."""
    existing = await db.execute(
        select(SubmissionJob).where(SubmissionJob.case_id == case_id)
    )
    existing_job = existing.scalar_one_or_none()
    if existing_job:
        logger.info(f"Job already exists for case {case_id}, skipping")
        return existing_job

    case = await db.get(Case, case_id)
    if not case:
        raise ValueError(f"Case {case_id} not found")

    priority = compute_priority(case)

    job = SubmissionJob(
        case_id=case_id,
        priority=priority,
        is_stat=case.is_stat,
        status=JobStatus.QUEUED,
    )
    db.add(job)
    await db.flush()
    logger.info(f"Enqueued case {case_id} with priority {priority}")
    return job


async def enqueue_cases_bulk(
    db: AsyncSession, case_ids: list[str]
) -> list[SubmissionJob]:
    """Enqueue multiple cases. Skips already-enqueued."""
    # Find which already have jobs
    existing = await db.execute(
        select(SubmissionJob.case_id).where(SubmissionJob.case_id.in_(case_ids))
    )
    existing_ids = {r[0] for r in existing.all()}
    new_ids = [cid for cid in case_ids if cid not in existing_ids]

    if not new_ids:
        return []

    # Load cases for priority scoring
    cases_result = await db.execute(
        select(Case).where(Case.id.in_(new_ids))
    )
    cases = cases_result.scalars().all()

    jobs = []
    for case in cases:
        job = SubmissionJob(
            case_id=case.id,
            priority=compute_priority(case),
            is_stat=case.is_stat,
            status=JobStatus.QUEUED,
        )
        jobs.append(job)

    db.add_all(jobs)
    await db.flush()
    logger.info(f"Enqueued {len(jobs)} cases (skipped {len(existing_ids)} existing)")
    return jobs


# ── Claim / Release ──


async def claim_next_job(
    db: AsyncSession,
    worker_id: str,
    stat_only: bool = False,
    job_type: str | None = None,
) -> SubmissionJob | None:
    """Claim the highest-priority QUEUED job. Returns None if queue is empty.

    Uses SELECT FOR UPDATE SKIP LOCKED — concurrent workers never block each
    other and never claim the same job.

    Args:
        job_type: "FIRST_PASS" — claims NOTES_UPLOADED cases for portal first pass.
                  "SUBMIT"     — claims SUBMITTING cases for portal submission.
                  None         — defaults to FIRST_PASS behavior (backward compat).
    """

    q = (
        select(SubmissionJob)
        .join(Case, SubmissionJob.case_id == Case.id)
        .where(SubmissionJob.status == JobStatus.QUEUED)
    )

    # Filter by job_type + appropriate case state.
    # PROCESSING is also accepted: a QUEUED job + PROCESSING case means
    # the previous workflow attempt was abandoned (crash/restart). Safe to reclaim.
    if job_type == "SUBMIT":
        q = q.where(
            SubmissionJob.job_type == "SUBMIT",
            Case.state.in_((CaseState.SUBMITTING, CaseState.PROCESSING)),
        )
    elif job_type == "ORDER":
        q = q.where(
            SubmissionJob.job_type == "ORDER",
            Case.state.in_((CaseState.ORDER_READY, CaseState.PROCESSING)),
        )
    elif job_type == "SIGNATURE_REPLAY":
        q = q.where(
            SubmissionJob.job_type == "SIGNATURE_REPLAY",
            Case.state == CaseState.PENDING_NOTES,
        )
    elif job_type == "FIRST_PASS":
        q = q.where(
            SubmissionJob.job_type == "FIRST_PASS",
            Case.state.in_((CaseState.NOTES_UPLOADED, CaseState.PROCESSING)),
        )
    else:
        # Default: first pass only (backward compat)
        q = q.where(Case.state.in_((CaseState.NOTES_UPLOADED, CaseState.PROCESSING)))

    if stat_only:
        q = q.where(SubmissionJob.is_stat == True)

    q = (
        q.order_by(
            SubmissionJob.priority.desc(),
            SubmissionJob.created_at.asc(),
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )

    result = await db.execute(q)
    job = result.scalar_one_or_none()

    if not job:
        return None

    job.status = JobStatus.CLAIMED
    job.claimed_by = worker_id
    job.claimed_at = datetime.utcnow()
    job.attempt += 1
    await db.flush()

    logger.info(
        f"Worker {worker_id} claimed job {job.id} "
        f"(case={job.case_id}, priority={job.priority}, attempt={job.attempt})"
    )
    return job


async def mark_running(db: AsyncSession, job_id: str) -> None:
    """Mark a claimed job as actively running in the portal."""
    await db.execute(
        update(SubmissionJob)
        .where(SubmissionJob.id == job_id)
        .values(status=JobStatus.RUNNING, started_at=datetime.utcnow())
    )


async def mark_suspended(
    db: AsyncSession, job_id: str, awakeable_id: str
) -> None:
    """Mark job as suspended (awaiting rep review)."""
    await db.execute(
        update(SubmissionJob)
        .where(SubmissionJob.id == job_id)
        .values(status=JobStatus.SUSPENDED, awakeable_id=awakeable_id)
    )


async def mark_completed(db: AsyncSession, job_id: str) -> None:
    """Mark job as successfully completed."""
    await db.execute(
        update(SubmissionJob)
        .where(SubmissionJob.id == job_id)
        .values(status=JobStatus.COMPLETED, completed_at=datetime.utcnow())
    )


async def mark_failed(
    db: AsyncSession, job_id: str, error: str,
    exception_type: ExceptionType | None = None,
    exception_detail: str | None = None,
) -> None:
    """Mark job as failed. If under max_attempts, re-queue for retry."""
    job = await db.get(SubmissionJob, job_id)
    if not job:
        return

    if exception_type:
        job.exception_type = exception_type
        job.exception_detail = exception_detail

    if job.attempt < job.max_attempts and not exception_type:
        # Retryable failure — re-queue
        job.status = JobStatus.QUEUED
        job.claimed_by = None
        job.claimed_at = None
        job.last_error = error
        logger.info(f"Job {job_id} re-queued (attempt {job.attempt}/{job.max_attempts})")
    else:
        # Exhausted retries or exception needs human intervention
        job.status = JobStatus.FAILED
        job.last_error = error
        job.completed_at = datetime.utcnow()
        logger.warning(f"Job {job_id} FAILED: {error}")

    await db.flush()


async def mark_exception(
    db: AsyncSession,
    job_id: str,
    exception_type: ExceptionType,
    detail: str,
    awakeable_id: str | None = None,
) -> None:
    """Flag a job with an exception that needs human intervention."""
    values = {
        "status": JobStatus.SUSPENDED,
        "exception_type": exception_type,
        "exception_detail": detail,
    }
    if awakeable_id:
        values["awakeable_id"] = awakeable_id

    await db.execute(
        update(SubmissionJob).where(SubmissionJob.id == job_id).values(**values)
    )
    logger.info(f"Job {job_id} exception: {exception_type.value} — {detail}")


async def release_job(db: AsyncSession, job_id: str) -> None:
    """Release a claimed job back to QUEUED (e.g. worker shutdown)."""
    await db.execute(
        update(SubmissionJob)
        .where(SubmissionJob.id == job_id)
        .values(
            status=JobStatus.QUEUED,
            claimed_by=None,
            claimed_at=None,
        )
    )


# ── Queries ──


async def get_queue_stats(db: AsyncSession) -> dict:
    """Dashboard stats: counts by status, STAT queue depth, etc."""
    result = await db.execute(
        select(
            SubmissionJob.status,
            func.count(SubmissionJob.id),
        ).group_by(SubmissionJob.status)
    )
    by_status = {row[0].value: row[1] for row in result.all()}

    # STAT queue depth
    stat_result = await db.execute(
        select(func.count(SubmissionJob.id)).where(
            SubmissionJob.status == JobStatus.QUEUED,
            SubmissionJob.is_stat == True,
        )
    )
    stat_queued = stat_result.scalar() or 0

    # Exception breakdown
    exc_result = await db.execute(
        select(
            SubmissionJob.exception_type,
            func.count(SubmissionJob.id),
        ).where(
            SubmissionJob.exception_type.isnot(None),
            SubmissionJob.status.in_([JobStatus.SUSPENDED, JobStatus.FAILED]),
        ).group_by(SubmissionJob.exception_type)
    )
    exceptions = {row[0].value: row[1] for row in exc_result.all()}

    # Today's completed count
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    completed_result = await db.execute(
        select(func.count(SubmissionJob.id)).where(
            SubmissionJob.status == JobStatus.COMPLETED,
            SubmissionJob.completed_at >= today_start,
        )
    )
    completed_today = completed_result.scalar() or 0

    # Case state counts
    from app.db.models import Case, CaseState
    awaiting_result = await db.execute(
        select(func.count(Case.id)).where(
            Case.state.in_([CaseState.PENDING_NOTES, CaseState.WAITING_CLINICALS])
        )
    )
    awaiting_clinicals = awaiting_result.scalar() or 0

    ready_result = await db.execute(
        select(func.count(Case.id)).where(
            Case.state == CaseState.NOTES_UPLOADED
        )
    )
    ready_for_processing = ready_result.scalar() or 0

    return {
        "by_status": by_status,
        "stat_queued": stat_queued,
        "standard_queued": by_status.get("QUEUED", 0) - stat_queued,
        "exceptions": exceptions,
        "completed_today": completed_today,
        "awaiting_clinicals": awaiting_clinicals,
        "ready_for_processing": ready_for_processing,
    }


async def list_jobs(
    db: AsyncSession,
    status: JobStatus | None = None,
    is_stat: bool | None = None,
    exception_type: ExceptionType | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[SubmissionJob]:
    """List jobs with optional filters."""
    q = select(SubmissionJob)
    if status:
        q = q.where(SubmissionJob.status == status)
    if is_stat is not None:
        q = q.where(SubmissionJob.is_stat == is_stat)
    if exception_type:
        q = q.where(SubmissionJob.exception_type == exception_type)
    q = q.order_by(
        SubmissionJob.priority.desc(),
        SubmissionJob.created_at.asc(),
    ).limit(limit).offset(offset)
    result = await db.execute(q)
    return result.scalars().all()


async def list_exceptions(
    db: AsyncSession,
    limit: int = 50,
    exception_type: ExceptionType | None = None,
) -> Sequence[SubmissionJob]:
    """List jobs with exceptions (the Worklist). Optionally filter by type."""
    q = (
        select(SubmissionJob)
        .where(
            SubmissionJob.exception_type.isnot(None),
            SubmissionJob.status.in_([JobStatus.SUSPENDED, JobStatus.FAILED]),
        )
    )
    if exception_type:
        q = q.where(SubmissionJob.exception_type == exception_type)
    q = q.order_by(
        SubmissionJob.priority.desc(),
        SubmissionJob.created_at.asc(),
    ).limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


# ── Worker Accounts ──


async def get_active_account(
    db: AsyncSession, container_id: str
) -> WorkerAccount | None:
    """Get the currently active account for a container."""
    result = await db.execute(
        select(WorkerAccount).where(
            WorkerAccount.container_id == container_id,
            WorkerAccount.is_active == True,
        )
    )
    return result.scalar_one_or_none()


async def list_worker_accounts(db: AsyncSession) -> Sequence[WorkerAccount]:
    """List all worker accounts with their status."""
    result = await db.execute(
        select(WorkerAccount).order_by(WorkerAccount.container_id, WorkerAccount.shift)
    )
    return result.scalars().all()
