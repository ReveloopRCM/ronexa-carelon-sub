"""Availity integration routes.

Phase 1 scope:
- Proxy the Availity eligibility-jobs API (list, get, delete).
- Bulk-send MEMBER_NOT_FOUND cases to Availity (single job per click).
- Settings CRUD + test-connection (credentials + auto-send toggle).

No DB tables for Availity jobs — Availity is the source of truth.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import Case, CaseState, SubmissionJob, ExceptionType, SystemSetting

from app.services import availity as availity_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/availity", tags=["availity"])


# ── Settings helpers ──


AVAILITY_KEYS = [
    "availity_base_url",
    "availity_username",
    "availity_password",
    "availity_auto_send_enabled",
    "availity_auto_send_interval_minutes",
]
AVAILITY_ENCRYPTED_KEYS = {"availity_password"}

AVAILITY_WORKLIST_FILTERS = [
    ExceptionType.STAT_PENDED,
    ExceptionType.RPO_NOT_FOUND,
    ExceptionType.MED_NECESSITY,
    ExceptionType.MEMBER_NOT_FOUND,
    ExceptionType.DUPLICATE_AUTH,
    ExceptionType.PORTAL_ERROR,
    ExceptionType.PHONE_MISSING,
    ExceptionType.REFERRING_NPI_MISSING,
    ExceptionType.ELIGIBILITY_EXPIRED,
    ExceptionType.NO_AUTH_REQUIRED,
    ExceptionType.ALREADY_WORKED,
]


def _worklist_label(exception_type: ExceptionType) -> str:
    return exception_type.value.replace("_", " ").title()


async def _get_setting(db: AsyncSession, key: str, default=None):
    setting = await db.get(SystemSetting, key)
    if not setting:
        return default
    return setting.value


async def _set_setting(db: AsyncSession, key: str, value) -> None:
    from datetime import datetime
    setting = await db.get(SystemSetting, key)
    if setting:
        setting.value = value
        setting.updated_at = datetime.utcnow()
    else:
        db.add(SystemSetting(key=key, value=value))
    await db.flush()


# ── Settings endpoints ──


@router.get("/settings")
async def get_availity_settings(db: AsyncSession = Depends(get_db)):
    """Return current Availity settings (password masked)."""
    result = {}
    for key in AVAILITY_KEYS:
        val = await _get_setting(db, key, None)
        if val is None:
            if key == "availity_base_url":
                val = "https://api-v2.ronexa.com"
            elif key == "availity_auto_send_enabled":
                val = False
            elif key == "availity_auto_send_interval_minutes":
                val = 30
            else:
                val = ""
        # Mask encrypted secrets
        if key in AVAILITY_ENCRYPTED_KEYS and val and isinstance(val, str) and len(val) > 8:
            result[key] = val[:4] + "•" * 16 + val[-4:]
        else:
            result[key] = val
    return result


@router.put("/settings")
async def update_availity_settings(body: dict, db: AsyncSession = Depends(get_db)):
    """Update Availity settings. Encrypts password. Enforces min interval."""
    from app.intelligence.llm_config import encrypt_value

    updated = []
    for key in AVAILITY_KEYS:
        if key not in body:
            continue
        value = body[key]
        # Don't overwrite with masked placeholder
        if key in AVAILITY_ENCRYPTED_KEYS and "•" in str(value or ""):
            continue
        # Enforce minimum interval
        if key == "availity_auto_send_interval_minutes":
            try:
                value = max(30, int(value))
            except (TypeError, ValueError):
                value = 30
        # Encrypt secrets (unless already encrypted, prefix check)
        if key in AVAILITY_ENCRYPTED_KEYS and value and not str(value).startswith("gAAAA"):
            value = encrypt_value(str(value))
        await _set_setting(db, key, value)
        updated.append(key)

    await db.commit()
    # Invalidate cached session so new credentials take effect immediately
    availity_svc._session_cookie = None
    availity_svc._session_expires_at = 0.0
    return {"updated": updated}


@router.post("/test-connection")
async def test_availity_connection():
    """Attempt to authenticate against Availity."""
    return await availity_svc.test_connection()


# ── Availity worklist filter endpoints ──


@router.get("/worklist/filters")
async def get_availity_worklist_filters(db: AsyncSession = Depends(get_db)):
    """Return all supported Availity worklist filters, including zero-count filters."""
    count_result = await db.execute(
        select(
            SubmissionJob.exception_type,
            func.count(SubmissionJob.id),
        )
        .where(SubmissionJob.exception_type.is_not(None))
        .group_by(SubmissionJob.exception_type)
    )

    counts = {
        exception_type.value if hasattr(exception_type, "value") else str(exception_type): count
        for exception_type, count in count_result.all()
    }

    return [
        {
            "key": exception_type.value,
            "label": _worklist_label(exception_type),
            "count": counts.get(exception_type.value, 0),
        }
        for exception_type in AVAILITY_WORKLIST_FILTERS
    ]


@router.get("/worklist")
async def get_availity_worklist(
    exception_type: ExceptionType | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Return Availity worklist rows filtered by exception_type."""
    offset = (page - 1) * page_size

    conditions = []
    if exception_type is not None:
        conditions.append(SubmissionJob.exception_type == exception_type)
    else:
        conditions.append(SubmissionJob.exception_type.is_not(None))

    total_result = await db.execute(
        select(func.count(SubmissionJob.id))
        .join(Case, SubmissionJob.case_id == Case.id)
        .where(and_(*conditions))
    )
    total = total_result.scalar_one()

    result = await db.execute(
        select(Case, SubmissionJob)
        .join(SubmissionJob, SubmissionJob.case_id == Case.id)
        .where(and_(*conditions))
        .order_by(SubmissionJob.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )

    rows = []
    for case, job in result.all():
        rows.append({
            "job_id": job.id,
            "case_id": case.id,
            "exam_id": case.exam_id,
            "first_name": case.first_name,
            "last_name": case.last_name,
            "dob": case.dob,
            "policy_num": case.policy_num,
            "patient_phone": case.patient_phone,
            "referring_npi": case.referring_npi,
            "valid_from": case.valid_from,
            "valid_through": case.valid_through,
            "status": job.status.value if hasattr(job.status, "value") else job.status,
            "exception_type": job.exception_type.value if job.exception_type else None,
            "exception_detail": job.exception_detail,
            "created_at": job.created_at.isoformat() if job.created_at else None,
        })

    return {
        "rows": rows,
        "count": len(rows),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ── Job proxy endpoints ──


@router.get("/jobs")
async def list_jobs(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    """Proxy GET /api/eligibility-jobs."""
    try:
        return await availity_svc.list_jobs(status=status, page=page, page_size=page_size)
    except Exception as e:
        logger.warning(f"Availity list_jobs failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Proxy GET /api/eligibility-jobs/{job_id}."""
    try:
        job = await availity_svc.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return job
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Availity get_job failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """Proxy DELETE /api/eligibility-jobs/{job_id}."""
    try:
        ok = await availity_svc.delete_job(job_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return {"status": "deleted", "job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Availity delete_job failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


# ── Bulk send for MEMBER_NOT_FOUND ──


async def _collect_member_not_found_cases(db: AsyncSession) -> list[Case]:
    """Return all cases currently HOLD with exception_type=MEMBER_NOT_FOUND."""
    result = await db.execute(
        select(Case)
        .join(SubmissionJob, SubmissionJob.case_id == Case.id)
        .where(
            and_(
                Case.state == CaseState.HOLD,
                SubmissionJob.exception_type == ExceptionType.MEMBER_NOT_FOUND,
            )
        )
        .order_by(Case.updated_at.desc())
    )
    return list(result.scalars().all())


@router.get("/member-not-found-count")
async def member_not_found_count(db: AsyncSession = Depends(get_db)):
    """Count of MEMBER_NOT_FOUND cases eligible for the next bulk send."""
    cases = await _collect_member_not_found_cases(db)
    total = len(cases)
    complete = 0
    for c in cases:
        payload = availity_svc.case_to_availity_payload(c)
        if availity_svc.payload_is_complete(payload):
            complete += 1
    return {
        "total": total,
        "ready_to_send": complete,
        "skipped_incomplete": total - complete,
    }


@router.post("/send-member-not-found")
async def send_member_not_found(db: AsyncSession = Depends(get_db)):
    """Collect MEMBER_NOT_FOUND cases, map to Availity payloads, submit one job.

    Returns {job_id, case_count, skipped_incomplete, sample_skipped}.
    """
    cases = await _collect_member_not_found_cases(db)
    if not cases:
        raise HTTPException(status_code=400, detail="No MEMBER_NOT_FOUND cases to send")

    payloads: list[dict] = []
    skipped: list[dict[str, Any]] = []
    for c in cases:
        payload = availity_svc.case_to_availity_payload(c)
        if availity_svc.payload_is_complete(payload):
            payloads.append(payload)
        else:
            missing = [
                k for k in ("organisation", "payer", "center_npi", "policy_number",
                            "last_name", "first_name", "date_of_birth", "state")
                if not str(payload.get(k, "")).strip()
            ]
            skipped.append({
                "exam_id": c.exam_id,
                "first_name": c.first_name,
                "last_name": c.last_name,
                "missing_fields": missing,
            })

    if not payloads:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "All MEMBER_NOT_FOUND cases have missing required fields for Availity",
                "skipped_count": len(skipped),
                "sample_skipped": skipped[:5],
            },
        )

    try:
        response = await availity_svc.submit_job(payloads)
    except Exception as e:
        logger.error(f"Availity submit_job failed: {e}")
        raise HTTPException(status_code=502, detail=f"Availity submit failed: {e}")

    return {
        "job_id": response.get("job_id"),
        "status": response.get("status"),
        "case_count": response.get("case_count", len(payloads)),
        "skipped_incomplete": len(skipped),
        "sample_skipped": skipped[:5],
    }