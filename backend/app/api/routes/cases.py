"""Case CRUD + status endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import settings
from app.db.database import get_db
from app.db import repositories as repo
from app.db.models import CaseState, SubmissionJob
from app.workflow.restate_utils import purge_case_workflow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.get("")
async def list_cases(
    state: str | None = None,
    center_npi: str | None = None,
    batch_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List cases with filtering."""
    state_enum = CaseState(state) if state else None
    cases = await repo.list_cases(
        db,
        state=state_enum,
        center_npi=center_npi,
        batch_id=batch_id,
        limit=limit,
        offset=offset,
    )
    return [_serialize_case(c) for c in cases]


@router.get("/{case_id}")
async def get_case(case_id: str, db: AsyncSession = Depends(get_db)):
    """Case detail + questions + audit trail."""
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    questions = await repo.get_questions_for_case(db, case_id)
    audit = await repo.get_audit_trail(db, case_id)
    notes = await repo.get_notes_for_case(db, case_id)

    # Extract flow checks from audit trail for dedicated UI section
    flow_checks = []
    audit_events = []
    for e in audit:
        event = {
            "id": e.id,
            "actor": e.actor,
            "action": e.action,
            "data": e.data,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
        }
        audit_events.append(event)
        if e.action and e.action.startswith("flow_check:"):
            check_name = e.action.replace("flow_check:", "")
            flow_checks.append({
                "check": check_name,
                "data": e.data or {},
                "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            })

    return {
        **_serialize_case(case),
        "questions": [_serialize_question(q) for q in questions],
        "clinical_notes": [
            {
                "id": n.id,
                "filename": n.filename,
                "page_count": n.page_count,
                "document_type": n.document_type,
                "structured": n.structured or {},
                "uploaded_at": n.uploaded_at.isoformat() if n.uploaded_at else None,
            }
            for n in notes
        ],
        "flow_checks": flow_checks,
        "audit_trail": audit_events,
    }


@router.get("/{case_id}/clinical-pdf")
async def get_clinical_pdf(case_id: str, db: AsyncSession = Depends(get_db)):
    """Serve the clinical PDF from Azure Blob Storage.

    Looks up the case's clinical_blob_key or file_key, fetches from blob,
    returns raw PDF bytes for inline display.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if not case.clinical_blob_key and not case.file_key:
        raise HTTPException(status_code=404, detail="No clinical PDF associated with this case")

    from app.ingest.blob_fetcher import fetch_clinical_pdf

    case_dict = {
        "clinical_blob_key": case.clinical_blob_key,
        "file_key": case.file_key,
    }

    try:
        pdf_bytes = fetch_clinical_pdf(case_dict)
    except Exception as e:
        logger.error(f"Failed to fetch clinical PDF for case {case_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Blob fetch failed: {e}")

    if not pdf_bytes:
        raise HTTPException(status_code=404, detail="Clinical PDF not found in blob storage")

    filename = case.clinical_blob_key or case.file_key or "clinical.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=\"{filename}\""},
    )


@router.get("/{case_id}/auth-pdf")
async def get_auth_pdf(case_id: str, db: AsyncSession = Depends(get_db)):
    """Serve authorization PDF or NO_AUTH screenshot from Azure Blob Storage.

    Supports both PDF (from approved submissions) and PNG (NO_AUTH screenshots).
    Uses raw blob client to avoid fetch_blob()'s auto-.pdf suffix behavior.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if not case.auth_pdf_url:
        raise HTTPException(status_code=404, detail="No authorization PDF for this case")

    from app.ingest.blob_fetcher import _get_container

    blob_key = case.auth_pdf_url
    try:
        container = _get_container()
        blob_client = container.get_blob_client(blob_key)
        data = blob_client.download_blob().readall()
    except Exception as e:
        logger.error(f"Failed to fetch auth blob for case {case_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Blob fetch failed: {e}")

    # Detect content type from blob key extension
    if blob_key.lower().endswith(".png"):
        media_type = "image/png"
    else:
        media_type = "application/pdf"

    filename = blob_key.rsplit("/", 1)[-1] if "/" in blob_key else "authorization"

    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f"inline; filename=\"{filename}\""},
    )


@router.post("/{case_id}/notes")
async def upload_notes(
    case_id: str,
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
):
    """Upload a clinical note PDF → extract structured data via vision LLM.

    Flow:
      1. Read PDF bytes from multipart upload
      2. extract_page_images() → PNG pages
      3. extract_clinical_context() → ClinicalContext dict (Haiku vision)
      4. Save ClinicalNote to DB with structured extraction
      5. Update case state to NOTES_UPLOADED

    Returns the structured extraction so the frontend can display it.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    pdf_bytes = await file.read()
    if len(pdf_bytes) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    logger.info(f"Uploading clinical note for case {case_id}: {file.filename} ({len(pdf_bytes)} bytes)")

    # Step 1: Extract page images from PDF
    from app.ingest.pdf_parser import extract_page_images

    pages = extract_page_images(pdf_bytes)
    if not pages:
        raise HTTPException(status_code=422, detail="No readable pages found in PDF")

    logger.info(f"Extracted {len(pages)} page(s) from {file.filename}")

    # Step 2: Run vision extraction (Haiku → structured clinical data)
    from app.intelligence.extractor import extract_clinical_context

    case_context = {
        "cpt_code": case.cpt_code,
        "icd1": case.icd1,
    }

    try:
        structured = await extract_clinical_context(pdf_bytes, case_context)
    except Exception as e:
        logger.error(f"Vision extraction failed for case {case_id}: {e}")
        # Still save the note — extraction can be retried
        structured = {"error": str(e), "_meta": {"page_count": len(pages)}}

    # Step 3: Save ClinicalNote to DB
    doc_type = structured.get("document_type", "UNKNOWN")
    doc_quality = structured.get("document_quality", "CLEAN")

    note = await repo.create_clinical_note(
        db,
        case_id=case_id,
        filename=file.filename,
        page_count=len(pages),
        document_type=doc_type,
        document_quality=doc_quality,
        extraction_method="azure_document_intelligence",
        structured=structured,
    )

    # Step 4: Update case state
    if case.state in (CaseState.PENDING_NOTES, CaseState.PENDING_STAT):
        await repo.update_case_state(db, case_id, CaseState.NOTES_UPLOADED)

    await repo.create_audit_event(
        db,
        case_id=case_id,
        actor="system",
        action="notes_uploaded",
        data={
            "filename": file.filename,
            "page_count": len(pages),
            "note_id": note.id,
            "has_extraction": "error" not in structured,
        },
    )

    await db.commit()

    logger.info(f"Clinical note saved: {note.id} ({len(pages)} pages, type={doc_type})")

    # Step 5: Auto-resolve clinical awakeable if workflow is suspended
    workflow_resumed = False
    # awakeable_id lives on the SubmissionJob (questions table), not Case
    job_awakeable = None
    if case.state == CaseState.WAITING_CLINICALS:
        job_result = await db.execute(
            select(SubmissionJob).where(SubmissionJob.case_id == case_id).order_by(SubmissionJob.created_at.desc())
        )
        job = job_result.scalar_one_or_none()
        if job:
            job_awakeable = job.awakeable_id
    if case.state == CaseState.WAITING_CLINICALS and job_awakeable:
        logger.info(
            f"Case {case_id}: auto-resolving clinical awakeable {job_awakeable}"
        )
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{settings.RESTATE_URL}/restate/awakeables/{job_awakeable}/resolve",
                    json={"action": "clinicals_uploaded", "note_count": 1},
                    headers={"Content-Type": "application/json"},
                )
            workflow_resumed = True
        except Exception as e:
            logger.error(f"Failed to auto-resolve clinical awakeable: {e}")

    return {
        "note_id": note.id,
        "filename": file.filename,
        "page_count": len(pages),
        "document_type": doc_type,
        "structured": structured,
        "case_state": case.state.value,
        "workflow_resumed": workflow_resumed,
    }


@router.post("/{case_id}/process")
async def process_case(
    case_id: str,
    dry_run: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Start Restate workflow for a case.

    Args:
        dry_run: If true, stop after facility_search — don't submit.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if case.state not in (CaseState.PENDING_NOTES, CaseState.PENDING_STAT, CaseState.NOTES_UPLOADED):
        raise HTTPException(
            status_code=400,
            detail=f"Case in state {case.state.value} cannot be processed",
        )

    # Build case data dict for CaseWorkflow
    # Load clinical context if available
    notes = await repo.get_notes_for_case(db, case_id)
    clinical_context = None
    if notes:
        clinical_context = notes[0].structured if notes[0].structured else None

    case_event = {
        "worker_id": "worker-a",  # Virtual Object key; URL resolved via WorkerAccount lookup
        "case_data": {
            "id": case.id,
            "exam_id": case.exam_id,
            "first_name": case.first_name,
            "last_name": case.last_name,
            "dob": case.dob,
            "policy_num": case.policy_num,
            "patient_zip": case.patient_zip,
            "center_npi": case.center_npi,
            "center_abbr": case.center_abbr,
            "cpt_code": case.cpt_code,
            "icd1": case.icd1,
            "icd2": case.icd2,
            "icd3": case.icd3,
            "icd4": case.icd4,
            "icd5": case.icd5,
            "referring_npi": case.referring_npi,
            "referring_fax": case.referring_fax,
            "patient_phone": case.patient_phone,
            "carrier_id": case.carrier_id,
            "is_stat": case.is_stat,
            "scheduled_dt": str(case.scheduled_dt) if case.scheduled_dt else None,
            "raw_data": case.raw_data or {},
        },
        "clinical_context": clinical_context,
    }

    # Dispatch via CaseWorkflow (fire-and-forget)
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.RESTATE_URL}/CaseWorkflow/{case_id}/run/send",
            json=case_event,
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
        if resp.status_code not in (200, 202):
            logger.error(f"Failed to start workflow: {resp.status_code} {resp.text}")
            raise HTTPException(status_code=502, detail=f"Failed to start workflow: {resp.text}")

    await repo.update_case_state(db, case_id, CaseState.PROCESSING)
    await db.commit()

    return {"case_id": case_id, "state": "PROCESSING"}


@router.post("/{case_id}/requeue")
async def requeue_case(case_id: str, db: AsyncSession = Depends(get_db)):
    """Move a HOLD or FAILED case back to the processing queue.

    Resets state to NOTES_UPLOADED, clears hold_reason, resets job to QUEUED.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if case.state not in (CaseState.HOLD, CaseState.FAILED):
        raise HTTPException(
            status_code=400,
            detail=f"Case is {case.state.value} — only HOLD or FAILED cases can be requeued",
        )

    # Purge stale Restate workflow invocation so re-dispatch isn't silently dropped
    await purge_case_workflow(case_id)

    # Reset case state
    case.state = CaseState.NOTES_UPLOADED
    case.hold_reason = None

    # Reset the submission job
    from app.db.models import SubmissionJob, JobStatus
    job_result = await db.execute(
        select(SubmissionJob).where(SubmissionJob.case_id == case_id)
    )
    job = job_result.scalar_one_or_none()
    if job:
        job.status = JobStatus.QUEUED
        job.claimed_by = None
        job.exception_type = None
        job.exception_detail = None
        job.attempt = 0

    await repo.create_audit_event(
        db, case_id=case_id, actor="ui",
        action="requeued",
        data={"from_state": "HOLD", "reason": "Manual requeue from case detail"},
    )

    await db.commit()
    return {"case_id": case_id, "state": "NOTES_UPLOADED", "message": "Case requeued"}


@router.patch("/{case_id}/cure")
async def cure_and_requeue(
    case_id: str,
    updates: dict,
    db: AsyncSession = Depends(get_db),
):
    """Fix missing/bad data on a HOLD case and requeue for processing.

    Accepts a dict of field updates (e.g., {"referring_npi": "1234567890"}).
    Only allows updating known curable fields.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if case.state != CaseState.HOLD:
        raise HTTPException(
            status_code=400,
            detail=f"Case is {case.state.value} — only HOLD cases can be cured",
        )

    # Allowed curable fields
    curable_fields = {
        "first_name", "last_name", "dob", "policy_num", "patient_zip",
        "center_npi", "center_abbr", "cpt_code",
        "icd1", "icd2", "icd3", "icd4", "icd5",
        "referring_npi", "referring_fax", "patient_phone", "carrier_id",
    }

    applied = {}
    for field, value in updates.items():
        if field not in curable_fields:
            continue
        old_value = getattr(case, field, None)
        setattr(case, field, value)
        applied[field] = {"old": old_value, "new": value}

    if not applied:
        raise HTTPException(
            status_code=400,
            detail=f"No valid fields to update. Allowed: {sorted(curable_fields)}",
        )

    # Purge stale Restate workflow invocation so re-dispatch isn't silently dropped
    await purge_case_workflow(case_id)

    # Reset state and job
    old_reason = case.hold_reason
    case.state = CaseState.NOTES_UPLOADED
    case.hold_reason = None

    from app.db.models import SubmissionJob, JobStatus
    job_result = await db.execute(
        select(SubmissionJob).where(SubmissionJob.case_id == case_id)
    )
    job = job_result.scalar_one_or_none()
    if job:
        job.status = JobStatus.QUEUED
        job.claimed_by = None
        job.exception_type = None
        job.exception_detail = None
        job.attempt = 0  # Reset attempts — this is a data fix, not a retry

    await repo.create_audit_event(
        db, case_id=case_id, actor="ui",
        action="cured_and_requeued",
        data={
            "from_state": "HOLD",
            "hold_reason": old_reason,
            "fields_updated": applied,
        },
    )

    await db.commit()
    return {
        "case_id": case_id,
        "state": "NOTES_UPLOADED",
        "fields_updated": applied,
        "message": "Case cured and requeued",
    }


@router.post("/{case_id}/resolve-clinicals")
async def resolve_clinicals(case_id: str, db: AsyncSession = Depends(get_db)):
    """Resolve the clinical awakeable — resumes workflow after clinicals arrive.

    Called automatically after note upload, or manually via 'Clinicals Ready' button.
    Checks that notes actually exist before resolving.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if case.state != CaseState.WAITING_CLINICALS:
        raise HTTPException(
            status_code=400,
            detail=f"Case is {case.state.value}, not WAITING_CLINICALS",
        )

    # awakeable_id lives on the SubmissionJob, not Case
    job_result = await db.execute(
        select(SubmissionJob).where(SubmissionJob.case_id == case_id).order_by(SubmissionJob.created_at.desc())
    )
    job = job_result.scalar_one_or_none()
    awakeable_id = job.awakeable_id if job else None

    if not awakeable_id:
        raise HTTPException(
            status_code=400,
            detail="No awakeable ID — workflow may not be suspended",
        )

    # Verify notes actually exist
    notes = await repo.get_notes_for_case(db, case_id)
    if not notes:
        raise HTTPException(
            status_code=400,
            detail="No clinical notes found — upload clinicals first",
        )
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{settings.RESTATE_URL}/restate/awakeables/{awakeable_id}/resolve",
                json={"action": "clinicals_uploaded", "note_count": len(notes)},
                headers={"Content-Type": "application/json"},
            )
    except Exception as e:
        logger.error(f"Failed to resolve clinical awakeable {awakeable_id}: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to resume workflow: {e}")

    await repo.create_audit_event(
        db,
        case_id=case_id,
        actor="system",
        action="clinicals_resolved",
        data={"awakeable_id": awakeable_id, "note_count": len(notes)},
    )
    await db.commit()

    return {"status": "resolved", "case_id": case_id, "note_count": len(notes)}


def _serialize_case(case) -> dict:
    return {
        "id": case.id,
        "batch_id": case.batch_id,
        "exam_id": case.exam_id,
        "first_name": case.first_name,
        "last_name": case.last_name,
        "dob": case.dob,
        "policy_num": case.policy_num,
        "center_npi": case.center_npi,
        "center_abbr": case.center_abbr,
        "cpt_code": case.cpt_code,
        "icd1": case.icd1,
        "carrier_id": case.carrier_id,
        "clinical_blob_key": case.clinical_blob_key,
        "file_key": case.file_key,
        "referring_npi": case.referring_npi,
        "state": case.state.value,
        "sort_priority": case.sort_priority,
        "is_stat": case.is_stat,
        "auth_number": case.auth_number,
        "portal_case_id": case.portal_case_id,
        "denial_reason": case.denial_reason,
        "pend_reason": case.pend_reason,
        "hold_reason": case.hold_reason,
        "valid_from": case.valid_from,
        "valid_through": case.valid_through,
        "determination_status": case.determination_status,
        "ingested_at": case.ingested_at.isoformat() if case.ingested_at else None,
        "updated_at": case.updated_at.isoformat() if case.updated_at else None,
        "submitted_at": case.submitted_at.isoformat() if case.submitted_at else None,
        "flags": (case.raw_data or {}).get("_flags", []),
    }


def _serialize_question(q) -> dict:
    return {
        "id": q.id,
        "portal_question_id": q.portal_question_id,
        "group_id": q.group_id,
        "sequence": q.sequence,
        "question_type": q.question_type,
        "question_text": q.question_text,
        "options_json": q.options_json,
        "ai_answer": q.ai_answer,
        "ai_confidence": q.ai_confidence,
        "ai_evidence": q.ai_evidence,
        "ai_reasoning": q.ai_reasoning,
        "ai_gap": q.ai_gap,
        "review_state": q.review_state.value,
        "rep_answer": q.rep_answer,
        "reviewed_by": q.reviewed_by,
        "reviewed_at": q.reviewed_at.isoformat() if q.reviewed_at else None,
        "created_at": q.created_at.isoformat() if q.created_at else None,
    }
