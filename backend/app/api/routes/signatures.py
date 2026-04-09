"""Algorithm Signature management endpoints.

Provides CRUD for algorithm signatures + batch scan trigger
to find PENDING_NOTES cases that match existing signatures.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import (
    AlgorithmSignature,
    Case,
    CaseState,
    SubmissionJob,
    JobStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/signatures", tags=["signatures"])


@router.get("")
async def list_signatures(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """List all algorithm signatures with usage stats."""
    result = await db.execute(
        select(AlgorithmSignature)
        .order_by(AlgorithmSignature.times_used.desc(), AlgorithmSignature.created_at.desc())
        .limit(limit)
    )
    sigs = result.scalars().all()

    return [
        {
            "id": s.id,
            "cpt_code": s.cpt_code,
            "icd1": s.icd1,
            "carrier_id": s.carrier_id,
            "pathway_id": s.pathway_id,
            "pathway_name": s.pathway_name,
            "source_case_id": s.source_case_id,
            "algorithm_recommendation": s.algorithm_recommendation,
            "qa_count": len(s.qa_sequence) if s.qa_sequence else 0,
            "times_used": s.times_used or 0,
            "success_count": s.success_count or 0,
            "success_rate": (
                round((s.success_count or 0) / (s.times_used or 1) * 100, 1)
                if (s.times_used or 0) > 0
                else None
            ),
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in sigs
    ]


@router.get("/stats")
async def signature_stats(db: AsyncSession = Depends(get_db)):
    """Coverage statistics: how many PENDING_NOTES cases have matching signatures."""
    # Count total signatures
    sig_count = await db.execute(select(func.count(AlgorithmSignature.id)))
    total_sigs = sig_count.scalar() or 0

    # Count distinct CPT+ICD combos with signatures
    combo_count = await db.execute(
        select(func.count()).select_from(
            select(AlgorithmSignature.cpt_code, AlgorithmSignature.icd1)
            .distinct()
            .subquery()
        )
    )
    total_combos = combo_count.scalar() or 0

    # Count PENDING_NOTES cases
    pending_count = await db.execute(
        select(func.count(Case.id)).where(Case.state == CaseState.PENDING_NOTES)
    )
    total_pending = pending_count.scalar() or 0

    # Count PENDING_NOTES cases that have matching signatures
    if total_sigs > 0:
        sig_combos = await db.execute(
            select(AlgorithmSignature.cpt_code, AlgorithmSignature.icd1).distinct()
        )
        sig_pairs = {(r[0], r[1]) for r in sig_combos.all()}

        matching_count = await db.execute(
            select(func.count(Case.id)).where(
                Case.state == CaseState.PENDING_NOTES,
                func.concat(Case.cpt_code, '|', Case.icd1).in_(
                    [f"{cpt}|{icd}" for cpt, icd in sig_pairs]
                ),
            )
        )
        matching_pending = matching_count.scalar() or 0
    else:
        matching_pending = 0

    # Count CLINICAL_REVIEW cases
    clinical_review_count = await db.execute(
        select(func.count(Case.id)).where(Case.state == CaseState.CLINICAL_REVIEW)
    )
    in_clinical_review = clinical_review_count.scalar() or 0

    return {
        "total_signatures": total_sigs,
        "unique_cpt_icd_combos": total_combos,
        "pending_notes_total": total_pending,
        "pending_notes_with_signature": matching_pending,
        "coverage_pct": round(matching_pending / max(total_pending, 1) * 100, 1),
        "in_clinical_review": in_clinical_review,
    }


@router.get("/{cpt}/{icd}")
async def get_signature(
    cpt: str,
    icd: str,
    db: AsyncSession = Depends(get_db),
):
    """Get signature for a specific CPT+ICD combo."""
    result = await db.execute(
        select(AlgorithmSignature).where(
            AlgorithmSignature.cpt_code == cpt,
            AlgorithmSignature.icd1 == icd,
        )
    )
    sigs = result.scalars().all()
    if not sigs:
        raise HTTPException(status_code=404, detail="No signature found")

    return [
        {
            "id": s.id,
            "cpt_code": s.cpt_code,
            "icd1": s.icd1,
            "pathway_id": s.pathway_id,
            "pathway_name": s.pathway_name,
            "source_case_id": s.source_case_id,
            "qa_sequence": s.qa_sequence,
            "times_used": s.times_used or 0,
            "success_count": s.success_count or 0,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in sigs
    ]


@router.post("/scan")
async def scan_pending_cases(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """Scan PENDING_NOTES cases and create SIGNATURE_REPLAY jobs for those with matching signatures.

    Returns the number of jobs created.
    """
    # Get all signature CPT+ICD combos
    sig_result = await db.execute(
        select(AlgorithmSignature.cpt_code, AlgorithmSignature.icd1).distinct()
    )
    sig_pairs = {(r[0], r[1]) for r in sig_result.all()}

    if not sig_pairs:
        return {"jobs_created": 0, "message": "No algorithm signatures available"}

    # Find PENDING_NOTES cases matching signature combos
    # Exclude cases that already have a job (avoid double-processing)
    existing_jobs = await db.execute(
        select(SubmissionJob.case_id).where(
            SubmissionJob.status.in_([JobStatus.QUEUED, JobStatus.CLAIMED, JobStatus.RUNNING]),
        )
    )
    existing_case_ids = {r[0] for r in existing_jobs.all()}

    # Also exclude cases already processed via signature replay
    cases_result = await db.execute(
        select(Case).where(
            Case.state == CaseState.PENDING_NOTES,
            Case.signature_replay == False,
        ).limit(limit * 3)  # Over-fetch since we filter by combo
    )
    candidates = cases_result.scalars().all()

    jobs_created = 0
    for case in candidates:
        if case.id in existing_case_ids:
            continue
        if (case.cpt_code, case.icd1) not in sig_pairs:
            continue
        if jobs_created >= limit:
            break

        job = SubmissionJob(
            case_id=case.id,
            job_type="SIGNATURE_REPLAY",
            priority=10,  # Low priority — primary work takes precedence
            is_stat=False,
            status=JobStatus.QUEUED,
        )
        db.add(job)
        jobs_created += 1

    await db.commit()

    logger.info(f"Signature scan: created {jobs_created} SIGNATURE_REPLAY jobs from {len(candidates)} candidates")
    return {
        "jobs_created": jobs_created,
        "total_signatures": len(sig_pairs),
        "candidates_scanned": len(candidates),
    }
