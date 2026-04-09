"""Submission job queue API — priority queue, stats, exception worklist."""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import ExceptionType, JobStatus
from app.db import queue as job_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


# ── Request Models ──


class EnqueueRequest(BaseModel):
    case_ids: list[str]


class ResolveExceptionRequest(BaseModel):
    rep_id: str
    action: str  # "resolved", "skip", "override"
    note: str | None = None
    data: dict | None = None  # action-specific payload


# ── Dashboard Stats ──


@router.get("/stats")
async def queue_stats(db: AsyncSession = Depends(get_db)):
    """Dashboard overview: queue depths, exception counts, throughput."""
    stats = await job_queue.get_queue_stats(db)
    return stats


# ── Queue Views ──


@router.get("")
async def list_jobs(
    status: str | None = None,
    is_stat: bool | None = None,
    exception_type: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List submission jobs with filters."""
    status_enum = JobStatus(status) if status else None
    exc_enum = ExceptionType(exception_type) if exception_type else None

    jobs = await job_queue.list_jobs(
        db,
        status=status_enum,
        is_stat=is_stat,
        exception_type=exc_enum,
        limit=limit,
        offset=offset,
    )
    return await _serialize_jobs_with_exam_id(db, jobs)


@router.get("/queued")
async def queued_jobs(
    stat_only: bool = False,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List QUEUED jobs in priority order (what workers will pick up next)."""
    jobs = await job_queue.list_jobs(
        db,
        status=JobStatus.QUEUED,
        is_stat=True if stat_only else None,
        limit=limit,
    )
    return await _serialize_jobs_with_exam_id(db, jobs)


@router.get("/exceptions")
async def exception_worklist(
    exception_type: str | None = None,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Worklist: jobs needing human intervention, grouped by exception type."""
    if exception_type:
        exc_enum = ExceptionType(exception_type)
        jobs = await job_queue.list_exceptions(db, limit=limit, exception_type=exc_enum)
    else:
        jobs = await job_queue.list_exceptions(db, limit=limit)
    return await _serialize_jobs_with_exam_id(db, jobs)


# ── Enqueue ──


@router.post("/enqueue")
async def enqueue(req: EnqueueRequest, db: AsyncSession = Depends(get_db)):
    """Add cases to the submission queue."""
    jobs = await job_queue.enqueue_cases_bulk(db, req.case_ids)
    await db.commit()
    return {
        "enqueued": len(jobs),
        "jobs": await _serialize_jobs_with_exam_id(db, jobs),
    }


# ── Exception Resolution ──


@router.post("/{job_id}/resolve")
async def resolve_exception(
    job_id: str,
    req: ResolveExceptionRequest,
    db: AsyncSession = Depends(get_db),
):
    """Rep resolves an exception — triggers awakeable to resume workflow."""
    job = await db.get(job_queue.SubmissionJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != JobStatus.SUSPENDED:
        raise HTTPException(400, f"Job is {job.status.value}, not SUSPENDED")

    # Resolve the Restate awakeable if one exists
    if job.awakeable_id:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"http://localhost:8080/restate/awakeables/{job.awakeable_id}/resolve",
                    json={
                        "action": req.action,
                        "rep_id": req.rep_id,
                        "note": req.note,
                        "data": req.data,
                    },
                    timeout=5.0,
                )
            logger.info(f"Resolved awakeable {job.awakeable_id} for job {job_id}")
        except Exception as e:
            logger.error(f"Failed to resolve awakeable: {e}")
            raise HTTPException(500, f"Failed to resolve awakeable: {e}")

    # Update job status based on action
    if req.action == "skip":
        await job_queue.mark_completed(db, job_id)
    elif req.action == "resolved":
        job.status = JobStatus.QUEUED
        job.exception_type = None
        job.exception_detail = None
        job.claimed_by = None
        await db.flush()
    # "override" actions just resolved the awakeable — workflow continues

    await db.commit()
    return {"status": "resolved", "job_id": job_id}


# ── Worker Accounts ──


@router.get("/workers")
async def list_workers(db: AsyncSession = Depends(get_db)):
    """List all worker accounts with their current status."""
    accounts = await job_queue.list_worker_accounts(db)
    return [
        {
            "id": a.id,
            "username": a.username,
            "shift": a.shift.value,
            "container_id": a.container_id,
            "is_active": a.is_active,
            "is_logged_in": a.is_logged_in,
            "current_case_id": a.current_case_id,
            "cases_today": a.cases_today,
            "total_cases": a.total_cases,
            "last_login_at": a.last_login_at.isoformat() if a.last_login_at else None,
            "job_type": a.job_type or "FIRST_PASS",
        }
        for a in accounts
    ]


# ── Helpers ──


async def _serialize_jobs_with_exam_id(db, jobs) -> list[dict]:
    """Serialize jobs with exam_id from cases table."""
    from sqlalchemy import select
    from app.db.models import Case

    case_ids = [j.case_id for j in jobs]
    if case_ids:
        result = await db.execute(
            select(Case.id, Case.exam_id, Case.first_name, Case.last_name, Case.policy_num)
            .where(Case.id.in_(case_ids))
        )
        case_map = {r[0]: {"exam_id": r[1], "first_name": r[2], "last_name": r[3], "policy_num": r[4]} for r in result.all()}
    else:
        case_map = {}

    return [_serialize_job(j, case_map.get(j.case_id, {})) for j in jobs]


def _serialize_job(job: job_queue.SubmissionJob, case_info: dict = None) -> dict:
    info = case_info or {}
    return {
        "id": job.id,
        "case_id": job.case_id,
        "exam_id": info.get("exam_id"),
        "patient_name": f"{info.get('first_name', '')} {info.get('last_name', '')}".strip() or None,
        "policy_num": info.get("policy_num"),
        "priority": job.priority,
        "is_stat": job.is_stat,
        "status": job.status.value,
        "claimed_by": job.claimed_by,
        "exception_type": job.exception_type.value if job.exception_type else None,
        "exception_detail": job.exception_detail,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "last_error": job.last_error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
