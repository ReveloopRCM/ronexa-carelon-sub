"""Rep review queue endpoints — batch review with backtrack.

Two-job architecture: no awakeables.
  - resolve-l2: enqueues SUBMIT job → SubmitWorkflow via Restate ingress
  - rerun: resets job to FIRST_PASS → WorkerLoop picks it up
  - validate-fax: sends fax inline (no awakeable)
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.core.settings import settings
from app.db.database import get_db
from app.db import repositories as repo
from app.db.models import AuditEvent, Case, CaseState, ReviewState, SubmissionJob
from app.workflow.restate_utils import purge_case_workflow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/queue", tags=["queue"])


# --- Request Models ---


class AnswerOverride(BaseModel):
    """A single answer from the rep — may be AI-approved or edited."""
    group_id: int
    answer_value: str | list[str]


class BatchResolveRequest(BaseModel):
    """Batch review submission: rep approves/edits all questions at once."""
    rep_id: str
    answers: list[AnswerOverride]
    note: str | None = None


class FaxValidateRequest(BaseModel):
    """Fax validation: rep approves or rejects faxing clinical notes."""
    action: str  # "approved" or "rejected"
    rep_id: str
    reason: str | None = None


class FlagRequest(BaseModel):
    rep_id: str
    reason: str = ""


class PathwayChangeRequest(BaseModel):
    pathway_id: str
    pathway_name: str
    rep_id: str


# --- Endpoints ---


@router.get("")
async def list_queue(
    level: int | str | None = None,
    source: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """Cases awaiting review.

    Params:
        level: 1=L1, 2=L2, clinical, awaiting_clinicals, or None for all
        source: "order" to filter to order-only cases (OrderWorkflow processed)
        limit: max results
    """
    from sqlalchemy import or_

    # Map level to state filter
    if level == 1 or level == "1":
        states = [CaseState.L1_REVIEW]
    elif level == 2 or level == "2":
        states = [CaseState.L2_REVIEW]
    elif level == "clinical":
        states = [CaseState.CLINICAL_REVIEW]
    elif level == "awaiting_clinicals":
        states = [CaseState.WAITING_CLINICALS]
    else:
        states = [
            CaseState.L1_REVIEW,
            CaseState.L2_REVIEW,
            CaseState.PENDED_FAX_REVIEW,
            CaseState.CLINICAL_REVIEW,
        ]

    if source == "order":
        # Order-only cases: filter by job_type=ORDER or order_only_first_pass flag
        q = (
            select(Case)
            .outerjoin(SubmissionJob, SubmissionJob.case_id == Case.id)
            .where(
                Case.state.in_(states),
                or_(
                    SubmissionJob.job_type == "ORDER",
                    Case.raw_data["order_only_first_pass"].as_boolean() == True,
                ),
            )
            .order_by(Case.sort_priority, Case.ingested_at)
            .limit(limit)
        )
        result = await db.execute(q)
        cases = result.scalars().unique().all()
    elif source == "clinical":
        # Clinical cases only — exclude order-only
        q = (
            select(Case)
            .outerjoin(SubmissionJob, SubmissionJob.case_id == Case.id)
            .where(
                Case.state.in_(states),
                or_(
                    SubmissionJob.job_type.is_(None),
                    SubmissionJob.job_type != "ORDER",
                ),
                or_(
                    Case.raw_data["order_only_first_pass"].as_boolean() != True,
                    ~Case.raw_data.has_key("order_only_first_pass"),
                ),
            )
            .order_by(Case.sort_priority, Case.ingested_at)
            .limit(limit)
        )
        result = await db.execute(q)
        cases = result.scalars().unique().all()
    else:
        cases = await repo.list_review_queue(db, limit=limit, states=states)

    # Batch-fetch rerun counts from SubmissionJob
    case_ids = [c.id for c in cases]
    rerun_map: dict[str, int] = {}
    if case_ids:
        job_rows = await db.execute(
            select(SubmissionJob.case_id, SubmissionJob.attempt)
            .where(SubmissionJob.case_id.in_(case_ids))
        )
        rerun_map = {row.case_id: row.attempt for row in job_rows}

    return [
        {
            "id": c.id,
            "exam_id": c.exam_id,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "cpt_code": c.cpt_code,
            "center_abbr": c.center_abbr,
            "state": c.state.value,
            "is_stat": c.is_stat,
            "ingested_at": c.ingested_at.isoformat() if c.ingested_at else None,
            "rerun_count": rerun_map.get(c.id, 0),
            "auto_approved": getattr(c, "auto_approved", None),
            "approval_type": getattr(c, "approval_type", None),
            "algorithm_recommendation": getattr(c, "algorithm_recommendation", None),
            "signature_replay": getattr(c, "signature_replay", False),
        }
        for c in cases
    ]


@router.get("/{case_id}")
async def get_queue_item(case_id: str, db: AsyncSession = Depends(get_db)):
    """All pending questions + AI answers for batch review."""
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    all_questions = await repo.get_questions_for_case(db, case_id)
    notes = await repo.get_notes_for_case(db, case_id)

    # Fetch rerun count from SubmissionJob
    job_row = await db.execute(
        select(SubmissionJob.attempt).where(SubmissionJob.case_id == case_id)
    )
    rerun_count = (job_row.scalar() or 0)

    # Fetch latest L2 rerun note from audit events
    rerun_note = None
    if rerun_count > 0:
        ae_row = await db.execute(
            select(AuditEvent.data)
            .where(AuditEvent.case_id == case_id, AuditEvent.action == "l2_rerun")
            .order_by(AuditEvent.timestamp.desc())
            .limit(1)
        )
        ae_data = ae_row.scalar()
        if ae_data and isinstance(ae_data, dict):
            rerun_note = ae_data.get("note")

    return {
        "case": {
            "id": case.id,
            "exam_id": case.exam_id,
            "state": case.state.value,
            "first_name": case.first_name,
            "last_name": case.last_name,
            "dob": case.dob,
            "policy_num": case.policy_num,
            "center_abbr": case.center_abbr,
            "cpt_code": case.cpt_code,
            "icd1": case.icd1,
            "carrier_id": case.carrier_id,
            "clinical_blob_key": case.clinical_blob_key,
            "file_key": case.file_key,
            "auth_number": getattr(case, "auth_number", None),
            "portal_case_id": getattr(case, "portal_case_id", None),
            "pend_reason": getattr(case, "pend_reason", None),
            "determination_status": getattr(case, "determination_status", None),
            "pathway_name": getattr(case, "pathway_name", None),
            "pathway_id": getattr(case, "pathway_id", None),
            "pathway_options": getattr(case, "pathway_options", None),
            "rerun_count": rerun_count,
            "rerun_note": rerun_note,
            "auto_approved": getattr(case, "auto_approved", None),
            "gold_card_level": getattr(case, "gold_card_level", None),
            "approval_type": getattr(case, "approval_type", None),
            "algorithm_recommendation": getattr(case, "algorithm_recommendation", None),
            "hold_reason": case.hold_reason,
            "auth_pdf_url": getattr(case, "auth_pdf_url", None),
            "signature_replay": getattr(case, "signature_replay", False),
            "signature_id": getattr(case, "signature_id", None),
        },
        "questions": [_serialize_question(q) for q in all_questions],
        "clinical_notes": [
            {
                "id": n.id,
                "filename": n.filename,
                "structured": n.structured,
            }
            for n in notes
        ],
    }


@router.post("/{case_id}/resolve")
async def resolve_batch_legacy(
    case_id: str,
    body: BatchResolveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Legacy single-level resolve — redirects to L1 then auto-promotes to L2."""
    return await _resolve_l1(case_id, body, db, auto_promote=True)


@router.post("/{case_id}/resolve-l1")
async def resolve_l1(
    case_id: str,
    body: BatchResolveRequest,
    db: AsyncSession = Depends(get_db),
):
    """L1 rep approves/edits answers -> moves case to L2_REVIEW.

    Does NOT trigger submission — L2 does that.
    """
    return await _resolve_l1(case_id, body, db, auto_promote=False)


@router.post("/{case_id}/resolve-l2")
async def resolve_l2(
    case_id: str,
    body: BatchResolveRequest,
    db: AsyncSession = Depends(get_db),
):
    """L2 submit — approve all answers and enqueue SUBMIT job.

    Triggers SubmitWorkflow directly via Restate ingress.
    Worker B handles the portal submission.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    if case.state not in (CaseState.L2_REVIEW, CaseState.L1_REVIEW):
        raise HTTPException(status_code=400, detail=f"Case not in L2 review (state={case.state.value})")

    all_questions = await repo.get_questions_for_case(db, case_id)
    rep_answers_by_group = {a.group_id: a.answer_value for a in body.answers}

    # Apply L2 review (record who reviewed, no edits expected on submit)
    _apply_review(all_questions, rep_answers_by_group, body.rep_id, level=2)

    await db.flush()

    await repo.create_audit_event(
        db, case_id=case_id,
        actor=f"l2:{body.rep_id}",
        action="l2_submit",
        data={"total_questions": len(all_questions), "note": body.note},
    )

    case.state = CaseState.SUBMITTING
    await db.commit()

    # Enqueue SUBMIT job and trigger SubmitWorkflow via Restate
    await _enqueue_submit_job(case_id)

    return {"status": "submitted"}


@router.post("/{case_id}/rerun")
async def rerun_l2(
    case_id: str,
    body: BatchResolveRequest,
    db: AsyncSession = Depends(get_db),
):
    """L2 re-run — reviewer changed an answer, re-run first pass with corrections.

    Resets the SubmissionJob to QUEUED/FIRST_PASS so the WorkerLoop picks it up
    for a fresh first pass. The portal will regenerate downstream questions
    based on the corrected answer.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    if case.state not in (CaseState.L2_REVIEW, CaseState.L1_REVIEW):
        raise HTTPException(status_code=400, detail=f"Case not in L2 review (state={case.state.value})")

    all_questions = await repo.get_questions_for_case(db, case_id)
    rep_answers_by_group = {a.group_id: a.answer_value for a in body.answers}

    # Apply L2 edits — find the earliest changed group
    earliest_changed_group, changed_answer_dict = _apply_review(
        all_questions, rep_answers_by_group, body.rep_id, level=2,
    )

    if earliest_changed_group is None:
        # L2 made no edits — fall back to L1's earliest edit as backtrack point
        for q in sorted(all_questions, key=lambda x: x.group_id):
            if q.rep_answer and q.ai_answer:
                rep_val = q.rep_answer.get("Value") if isinstance(q.rep_answer, dict) else None
                ai_vals = q.ai_answer.get("Values", []) if isinstance(q.ai_answer, dict) else []
                ai_val = ai_vals[0] if isinstance(ai_vals, list) and len(ai_vals) == 1 else ai_vals if ai_vals else None
                if rep_val is not None and rep_val != ai_val:
                    earliest_changed_group = q.group_id
                    changed_answer_dict = {
                        "GroupId": q.group_id,
                        "QuestionId": q.portal_question_id,
                        "Type": q.question_type,
                        "Value": rep_val,
                    }
                    break

    # Check for pathway-only change (clinical scenario = GroupId 0)
    pathway_changed = False
    if earliest_changed_group is None:
        # Check if the pathway was changed (stored in case.pathway_id vs original)
        # A pathway change means GroupId=0 changed — all downstream questions are invalid
        if case.raw_data and case.raw_data.get("rerun_changed_group_id") == 0:
            # Already flagged from a previous pathway change
            earliest_changed_group = 0
            pathway_changed = True
        else:
            raise HTTPException(
                status_code=400,
                detail="No answers were changed — use Submit instead of Re-Run",
            )

    await db.flush()

    # Persist changed_group_id in case.raw_data so the worker knows where to backtrack
    raw = dict(case.raw_data or {})
    raw["rerun_changed_group_id"] = earliest_changed_group
    case.raw_data = raw

    await repo.create_audit_event(
        db, case_id=case_id,
        actor=f"l2:{body.rep_id}",
        action="l2_rerun",
        data={
            "total_questions": len(all_questions),
            "changed_group": earliest_changed_group,
            "pathway_changed": pathway_changed,
            "note": body.note,
        },
    )

    # Reset case to NOTES_UPLOADED so WorkerLoop can claim it for fresh first pass
    case.state = CaseState.NOTES_UPLOADED
    await db.commit()

    # Reset job for fresh first pass
    await _enqueue_rerun_job(case_id)

    return {"status": "rerunning", "backtrack_group": earliest_changed_group}


@router.patch("/{case_id}/pathway")
async def update_pathway(
    case_id: str,
    body: PathwayChangeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Rep changes the pathway (clinical scenario) selection.

    After changing, rep should Re-Run to trigger fresh first pass with new pathway.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    if case.state not in (CaseState.L1_REVIEW, CaseState.L2_REVIEW, CaseState.L1_REVIEW):
        raise HTTPException(
            status_code=400,
            detail=f"Case not in review (state={case.state.value})",
        )

    old_pathway = case.pathway_name
    case.pathway_name = body.pathway_name
    case.pathway_id = body.pathway_id

    # Flag that clinical scenario (GroupId=0) was changed — rerun will pick this up
    raw = dict(case.raw_data or {})
    raw["rerun_changed_group_id"] = 0
    case.raw_data = raw

    await repo.create_audit_event(
        db, case_id=case_id,
        actor=body.rep_id,
        action="pathway_changed",
        data={
            "old_pathway": old_pathway,
            "new_pathway": body.pathway_name,
            "new_pathway_id": body.pathway_id,
        },
    )
    await db.commit()

    logger.info(f"Pathway changed for {case_id}: {old_pathway} → {body.pathway_name}")
    return {"status": "updated", "pathway_name": body.pathway_name}


@router.post("/{case_id}/validate-fax")
async def validate_fax(
    case_id: str,
    body: FaxValidateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Rep validates fax details for a pended case — sends fax inline (no awakeable).

    After portal submission returns PENDED, the case enters PENDED_FAX_REVIEW.
    Rep reviews the fax destination, patient info, and clinical documents,
    then approves (fax sends immediately) or rejects (no fax, case stays pended).
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    if case.state != CaseState.PENDED_FAX_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"Case not in PENDED_FAX_REVIEW state (state={case.state.value})",
        )

    await repo.create_audit_event(
        db,
        case_id=case_id,
        actor=f"fax:{body.rep_id}",
        action=f"fax_{body.action}",
        data={"reason": body.reason},
    )

    fax_result = None
    if body.action == "approved":
        # Send fax inline — no awakeable needed
        try:
            fax_result = await _send_fax_inline(case)
            if fax_result and fax_result.get("ok"):
                await repo.create_audit_event(
                    db,
                    case_id=case_id,
                    actor="system",
                    action="fax_sent",
                    data={
                        "message_id": fax_result.get("message_id"),
                        "status": fax_result.get("status"),
                    },
                )
        except Exception as e:
            logger.error(f"validate_fax/{case_id}: fax send failed: {e}")
            fax_result = {"ok": False, "error": str(e)[:300]}
            await repo.create_audit_event(
                db,
                case_id=case_id,
                actor="system",
                action="fax_error",
                data={"error": str(e)[:300]},
            )

    # Move to PENDED regardless of fax success/failure
    case.state = CaseState.PENDED
    await db.commit()

    response = {"status": body.action}
    if fax_result:
        response["fax_sent"] = fax_result.get("ok", False)
        if fax_result.get("ok"):
            response["message_id"] = fax_result.get("message_id")
        else:
            response["fax_error"] = fax_result.get("error", "Unknown error")
    return response


@router.post("/{case_id}/flag")
async def flag_case(
    case_id: str,
    body: FlagRequest,
    db: AsyncSession = Depends(get_db),
):
    """Flag a case — hold for issues (missing docs, etc.)."""
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # Flag all pending questions
    all_questions = await repo.get_questions_for_case(db, case_id)
    for q in all_questions:
        if q.review_state == ReviewState.AI_SUGGESTED:
            q.review_state = ReviewState.FLAGGED
    await db.flush()

    await repo.create_audit_event(
        db,
        case_id=case_id,
        actor=f"rep:{body.rep_id}",
        action="flagged",
        data={"reason": body.reason},
    )
    await db.commit()

    return {"status": "flagged"}


# --- Internal Helpers ---


async def _resolve_l1(
    case_id: str,
    body: BatchResolveRequest,
    db: AsyncSession,
    auto_promote: bool = False,
) -> dict:
    """Internal L1 resolve logic."""
    from app.db.models import SystemSetting

    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    if case.state != CaseState.L1_REVIEW:
        raise HTTPException(status_code=400, detail=f"Case not in L1 review (state={case.state.value})")

    all_questions = await repo.get_questions_for_case(db, case_id)
    rep_answers_by_group = {a.group_id: a.answer_value for a in body.answers}

    # Apply L1 review
    earliest_changed_group, changed_answer_dict = _apply_review(
        all_questions, rep_answers_by_group, body.rep_id, level=1,
        l1_note=body.note,
    )

    await db.flush()

    await repo.create_audit_event(
        db, case_id=case_id,
        actor=f"l1:{body.rep_id}",
        action="l1_review_submitted",
        data={
            "total_questions": len(all_questions),
            "edited": earliest_changed_group is not None,
            "note": body.note,
        },
    )

    # Check if L2 is enabled
    l2_setting = await db.get(SystemSetting, "l2_review_enabled")
    l2_enabled = l2_setting.value if l2_setting else True

    if l2_enabled and not auto_promote:
        # Move to L2 queue
        case.state = CaseState.L2_REVIEW
        await db.commit()
        return {"status": "sent_to_l2", "edited": earliest_changed_group is not None}
    else:
        # L2 disabled or legacy mode — submit immediately
        case.state = CaseState.SUBMITTING
        await db.commit()

        # Enqueue SUBMIT job and trigger SubmitWorkflow
        await _enqueue_submit_job(case_id)

        return {"status": "submitted", "edited": earliest_changed_group is not None}


def _apply_review(
    all_questions: list,
    rep_answers_by_group: dict,
    rep_id: str,
    level: int,
    l1_note: str | None = None,
) -> tuple[int | None, dict | None]:
    """Apply rep answers to questions. Returns (earliest_changed_group, changed_answer_dict)."""
    earliest_changed_group = None
    changed_answer_dict = None

    for q in all_questions:
        if q.review_state == ReviewState.FLAGGED:
            continue

        # Extract the comparable value from a portal answer dict.
        # ai_answer uses {"Values": ["uuid"]} (plural list, portal format).
        # rep_answer uses {"Value": "uuid"} (singular, simplified format).
        def _extract_value(ans: dict | None) -> str | list[str] | None:
            if not ans or not isinstance(ans, dict):
                return ans
            # Try singular "Value" first (rep_answer format)
            if "Value" in ans:
                return ans["Value"]
            # Then plural "Values" (portal ai_answer format)
            vals = ans.get("Values")
            if isinstance(vals, list):
                if len(vals) == 0:
                    return ""
                return vals[0] if len(vals) == 1 else vals
            return None

        def _values_equal(a, b) -> bool:
            """Compare answer values, handling None/empty/list-order differences."""
            # Normalize None and empty to the same thing
            if not a and not b:
                return True
            if not a or not b:
                return False
            # Both are lists — compare as sets (order-independent)
            if isinstance(a, list) and isinstance(b, list):
                return sorted(a) == sorted(b)
            return a == b

        # For L2, compare against L1's answer (rep_answer or ai_answer)
        if level == 2:
            current_value = _extract_value(q.rep_answer) if q.rep_answer else _extract_value(q.ai_answer)
        else:
            # L1 compares against AI answer
            current_value = _extract_value(q.ai_answer)

        rep_value = rep_answers_by_group.get(q.group_id)
        if rep_value is None:
            rep_value = current_value  # No change

        rep_edited = not _values_equal(rep_value, current_value)

        if level == 1:
            q.review_state = ReviewState.REP_EDITED if rep_edited else ReviewState.REP_APPROVED
            q.rep_answer = {"Value": rep_value} if rep_edited else q.rep_answer
            q.l1_reviewed_by = rep_id
            q.l1_reviewed_at = datetime.utcnow()
            q.l1_note = l1_note if (rep_edited and l1_note) else None
        else:
            # L2 can override L1's answer
            if rep_edited:
                q.rep_answer = {"Value": rep_value}
                q.review_state = ReviewState.REP_EDITED
            q.l2_reviewed_by = rep_id
            q.l2_reviewed_at = datetime.utcnow()

        if rep_edited and earliest_changed_group is None:
            earliest_changed_group = q.group_id
            changed_answer_dict = {
                "GroupId": q.group_id,
                "QuestionId": q.portal_question_id,
                "Type": q.question_type,
                "Value": rep_value,
            }

    return earliest_changed_group, changed_answer_dict


def _build_portal_answer(q) -> dict:
    """Build a portal-format answer dict from a Question ORM object.

    Uses rep_answer (if edited) over ai_answer. Returns:
        {QuestionId, QuestionType, GroupId, Sequence, Values}
    """
    if isinstance(q.ai_answer, dict) and "Values" in q.ai_answer:
        portal_ans = dict(q.ai_answer)
        if q.rep_answer and q.rep_answer.get("Value"):
            val = q.rep_answer["Value"]
            portal_ans["Values"] = val if isinstance(val, list) else [val]
        return portal_ans
    else:
        val = (
            q.rep_answer.get("Value")
            if q.rep_answer
            else (q.ai_answer.get("Value") if isinstance(q.ai_answer, dict) else q.ai_answer)
        )
        return {
            "QuestionId": q.portal_question_id,
            "QuestionType": q.question_type,
            "GroupId": q.group_id,
            "Sequence": getattr(q, "sequence", 0) or 0,
            "Values": val if isinstance(val, list) else [val] if val else [],
        }


# --- Job Enqueue Helpers (replace awakeable resolution) ---


async def _enqueue_submit_job(case_id: str) -> None:
    """Reset SubmissionJob for SUBMIT phase. Submission workers pick it up via WorkerLoop.

    Updates the existing SubmissionJob row to QUEUED/SUBMIT and wakes all workers.
    WorkerLoop handles loading case_data, credentials, clinical_context via
    _claim_and_build_event() — same path as first-pass workers.
    """
    from app.db.database import async_session_factory
    from app.db.models import SubmissionJob, JobStatus, JobType
    from sqlalchemy import update

    # Purge stale CaseWorkflow invocation so re-dispatch isn't silently dropped.
    # Only purge CaseWorkflow — NOT SubmitWorkflow. The send-to-discover approach
    # creates a real invocation if none exists, which would start SubmitWorkflow
    # with an empty event dict (no worker_id → KeyError).
    await purge_case_workflow(case_id, workflows=("CaseWorkflow",))

    async with async_session_factory() as db:
        await db.execute(
            update(SubmissionJob)
            .where(SubmissionJob.case_id == case_id)
            .values(
                status=JobStatus.QUEUED,
                job_type="SUBMIT",
                claimed_by=None,
                claimed_at=None,
                attempt=SubmissionJob.attempt + 1,
                last_error=None,
                started_at=None,
                completed_at=None,
            )
        )
        await db.commit()

    logger.info(f"_enqueue_submit_job/{case_id}: job reset to QUEUED/SUBMIT")

    # Wake all workers so submission workers check the queue
    await _wake_workers()


async def _enqueue_rerun_job(case_id: str) -> None:
    """Reset SubmissionJob for fresh FIRST_PASS and wake sleeping workers.

    The WorkerLoop will claim the job and run a new CaseWorkflow with the
    rep's corrected answers already saved as rep_answer in the Question table.
    """
    from app.db.database import async_session_factory
    from app.db.models import SubmissionJob, JobStatus
    from sqlalchemy import update

    # Purge stale CaseWorkflow only — NOT SubmitWorkflow. The send-to-discover
    # approach creates a real invocation if none exists, which would start
    # SubmitWorkflow with an empty event dict (no worker_id → KeyError).
    await purge_case_workflow(case_id, workflows=("CaseWorkflow",))

    async with async_session_factory() as db:
        await db.execute(
            update(SubmissionJob)
            .where(SubmissionJob.case_id == case_id)
            .values(
                status=JobStatus.QUEUED,
                job_type="FIRST_PASS",
                claimed_by=None,
                claimed_at=None,
                attempt=SubmissionJob.attempt + 1,
                last_error=None,
                started_at=None,
                completed_at=None,
            )
        )
        await db.commit()

    logger.info(f"_enqueue_rerun_job/{case_id}: job reset to QUEUED/FIRST_PASS")

    # Wake sleeping workers so they check the queue
    await _wake_workers()


async def _wake_workers() -> None:
    """Resolve WorkerLoop awakeables so sleeping workers check the queue immediately."""
    from app.db.database import async_session_factory
    from app.db.models import WorkerAccount
    from sqlalchemy import select
    import httpx

    async with async_session_factory() as db:
        result = await db.execute(
            select(WorkerAccount.container_id, WorkerAccount.wake_awakeable_id)
            .where(
                WorkerAccount.is_active == True,
                WorkerAccount.wake_awakeable_id.isnot(None),
            )
        )
        workers = result.all()

    for worker_id, wake_id in workers:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{settings.RESTATE_URL}/restate/awakeables/{wake_id}/resolve",
                    json={"action": "wake"},
                    headers={"Content-Type": "application/json"},
                    timeout=5.0,
                )
            logger.info(f"_wake_workers: woke {worker_id}")
        except Exception as e:
            # Non-fatal — worker will wake on its 5-min fallback
            logger.debug(f"_wake_workers: failed to wake {worker_id}: {e}")


async def _send_fax_inline(case) -> dict:
    """Send clinical notes fax inline for a pended case (RingCentral API).

    Called directly from validate-fax endpoint — no awakeable needed.
    Returns the fax result dict from RingCentral.
    """
    from app.services.ringcentral_fax import fax_clinical_notes

    clinical_blob_key = case.clinical_blob_key or case.file_key
    if not clinical_blob_key:
        logger.warning(f"validate_fax/{case.id}: no clinical blob key — cannot fax")
        return {"ok": False, "error": "No clinical documents available to fax"}

    patient_name = f"{case.last_name or ''} {case.first_name or ''}"

    result = await fax_clinical_notes(
        clinical_blob_key=clinical_blob_key,
        order_id=getattr(case, "auth_number", "") or "",
        patient_name=patient_name,
        cpt_code=case.cpt_code or "",
        member_id=case.policy_num or "",
        dob=case.dob or "",
    )

    logger.info(f"validate_fax/{case.id}: fax result={result}")
    return result


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
        # Dual-answer: notes path
        "ai_notes_answer": getattr(q, "ai_notes_answer", None),
        "ai_notes_confidence": getattr(q, "ai_notes_confidence", None),
        "ai_notes_reasoning": getattr(q, "ai_notes_reasoning", None),
        "ai_approval_gap": getattr(q, "ai_approval_gap", None),
        "review_state": q.review_state.value,
        "review_level": getattr(q, "review_level", 1),
        "rep_answer": q.rep_answer,
        "l1_reviewed_by": getattr(q, "l1_reviewed_by", None),
        "l1_reviewed_at": q.l1_reviewed_at.isoformat() if getattr(q, "l1_reviewed_at", None) else None,
        "l1_note": getattr(q, "l1_note", None),
        "l2_reviewed_by": getattr(q, "l2_reviewed_by", None),
        "l2_reviewed_at": q.l2_reviewed_at.isoformat() if getattr(q, "l2_reviewed_at", None) else None,
        "reviewed_by": q.reviewed_by,
        "reviewed_at": q.reviewed_at.isoformat() if q.reviewed_at else None,
    }


# ── Clinical Review endpoints (signature replay cases) ──


class ClinicalReviewRequest(BaseModel):
    rep_id: str
    note: str | None = None


@router.post("/{case_id}/confirm-clinical")
async def confirm_clinical_review(
    case_id: str,
    body: ClinicalReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Rep confirms signature-replayed answers match clinicals → enqueue for submission.

    Moves case from CLINICAL_REVIEW → SUBMITTING and creates a SUBMIT job.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    if case.state != CaseState.CLINICAL_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"Case is in {case.state.value}, expected CLINICAL_REVIEW",
        )

    # Update case state
    case.state = CaseState.SUBMITTING

    # Create SUBMIT job
    from app.db.queue import enqueue_case
    from app.db.models import JobType

    # Check if job already exists
    existing_job = await db.execute(
        select(SubmissionJob).where(SubmissionJob.case_id == case_id)
    )
    job = existing_job.scalar_one_or_none()
    if job:
        # Re-use existing job, change to SUBMIT
        from app.db.models import JobStatus
        job.job_type = "SUBMIT"
        job.status = JobStatus.QUEUED
        job.claimed_by = None
        job.claimed_at = None
    else:
        new_job = SubmissionJob(
            case_id=case_id,
            job_type="SUBMIT",
            priority=100,
            is_stat=case.is_stat,
        )
        db.add(new_job)

    await repo.create_audit_event(
        db,
        case_id=case_id,
        actor=body.rep_id,
        action="clinical_review_confirmed",
        data={
            "from_state": "CLINICAL_REVIEW",
            "to_state": "SUBMITTING",
            "signature_replay": case.signature_replay,
            "signature_id": case.signature_id,
            "note": body.note,
        },
    )
    await db.commit()

    logger.info(f"Clinical review confirmed for {case_id} by {body.rep_id} → SUBMITTING")
    return {"status": "ok", "case_id": case_id, "new_state": "SUBMITTING"}


@router.post("/{case_id}/reject-clinical")
async def reject_clinical_review(
    case_id: str,
    body: ClinicalReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Rep rejects signature answers (clinicals don't match) → re-run with clinicals.

    Moves case from CLINICAL_REVIEW → NOTES_UPLOADED for fresh processing
    once clinicals are uploaded.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    if case.state != CaseState.CLINICAL_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"Case is in {case.state.value}, expected CLINICAL_REVIEW",
        )

    # Check if clinicals are available — if not, go back to PENDING_NOTES
    notes = await repo.get_notes_for_case(db, case_id)
    has_clinicals = any(n.structured for n in notes)

    if has_clinicals:
        case.state = CaseState.NOTES_UPLOADED
        new_state = "NOTES_UPLOADED"
    else:
        case.state = CaseState.PENDING_NOTES
        new_state = "PENDING_NOTES"

    # Clear signature replay flags
    case.signature_replay = False
    case.signature_id = None

    # Purge stale CaseWorkflow only — NOT SubmitWorkflow (send-to-discover
    # would create a new invocation with empty event → worker_id KeyError)
    await purge_case_workflow(case_id, workflows=("CaseWorkflow",))

    # Reset job for re-processing
    existing_job = await db.execute(
        select(SubmissionJob).where(SubmissionJob.case_id == case_id)
    )
    job = existing_job.scalar_one_or_none()
    if job:
        from app.db.models import JobStatus
        job.job_type = "FIRST_PASS"
        job.status = JobStatus.QUEUED
        job.claimed_by = None
        job.claimed_at = None

    await repo.create_audit_event(
        db,
        case_id=case_id,
        actor=body.rep_id,
        action="clinical_review_rejected",
        data={
            "from_state": "CLINICAL_REVIEW",
            "to_state": new_state,
            "has_clinicals": has_clinicals,
            "note": body.note,
        },
    )
    await db.commit()

    logger.info(
        f"Clinical review rejected for {case_id} by {body.rep_id} → {new_state}"
    )
    return {"status": "ok", "case_id": case_id, "new_state": new_state}


# ── No-Auth Review endpoints ──


@router.post("/{case_id}/confirm-no-auth")
async def confirm_no_auth(
    case_id: str,
    body: ClinicalReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Rep confirms portal's 'no auth required' determination.

    Transitions case from L1_REVIEW → NO_AUTH_REQUIRED (terminal).
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    if case.state not in (CaseState.L1_REVIEW):
        raise HTTPException(
            400, f"Case is in {case.state}, expected L1_REVIEW"
        )

    case.state = CaseState.NO_AUTH_REQUIRED

    # Complete the job
    from sqlalchemy import update
    from app.db.models import JobStatus, ExceptionType

    await db.execute(
        update(SubmissionJob)
        .where(SubmissionJob.case_id == case_id)
        .values(
            status=JobStatus.COMPLETED,
            exception_type=ExceptionType.NO_AUTH_REQUIRED,
            exception_detail=case.hold_reason or "Confirmed by rep",
        )
    )

    await repo.create_audit_event(
        db,
        case_id=case_id,
        actor=body.rep_id,
        action="no_auth_confirmed",
        data={"note": body.note},
    )
    await db.commit()

    logger.info(f"No-auth confirmed for {case_id} by {body.rep_id}")
    return {"status": "ok", "case_id": case_id, "new_state": "NO_AUTH_REQUIRED"}


@router.post("/{case_id}/reject-no-auth")
async def reject_no_auth(
    case_id: str,
    body: ClinicalReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Rep rejects portal's 'no auth required' — case needs reprocessing.

    Transitions case from L1_REVIEW → QUEUED for a fresh attempt.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    if case.state not in (CaseState.L1_REVIEW):
        raise HTTPException(
            400, f"Case is in {case.state}, expected L1_REVIEW"
        )

    case.state = CaseState.NOTES_UPLOADED if case.clinical_blob_key else CaseState.QUEUED
    case.hold_reason = None

    # Reset job for reprocessing
    from sqlalchemy import update
    from app.db.models import JobStatus

    await db.execute(
        update(SubmissionJob)
        .where(SubmissionJob.case_id == case_id)
        .values(
            status=JobStatus.QUEUED,
            job_type="FIRST_PASS",
            claimed_by=None,
            claimed_at=None,
        )
    )

    await repo.create_audit_event(
        db,
        case_id=case_id,
        actor=body.rep_id,
        action="no_auth_rejected",
        data={"note": body.note},
    )
    await db.commit()

    new_state = case.state.value
    logger.info(f"No-auth rejected for {case_id} by {body.rep_id} → {new_state}")
    return {"status": "ok", "case_id": case_id, "new_state": new_state}


# ── Already Worked endpoint (legacy system transition) ──

REVIEW_STATES = {
    CaseState.L1_REVIEW,
    CaseState.L2_REVIEW,
    CaseState.CLINICAL_REVIEW,
}


@router.post("/{case_id}/mark-already-worked")
async def mark_already_worked(
    case_id: str,
    body: ClinicalReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Rep flags case as already worked in the legacy system.

    Clears the case from the review queue. Not analytics-protected — can be flushed.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        raise HTTPException(404, "Case not found")
    if case.state not in REVIEW_STATES:
        raise HTTPException(
            400, f"Case is in {case.state}, expected a review state"
        )

    case.state = CaseState.ALREADY_WORKED
    case.hold_reason = body.note or "Worked in legacy system"

    from sqlalchemy import update
    from app.db.models import JobStatus, ExceptionType

    await db.execute(
        update(SubmissionJob)
        .where(SubmissionJob.case_id == case_id)
        .values(
            status=JobStatus.COMPLETED,
            exception_type=ExceptionType.ALREADY_WORKED,
            exception_detail=body.note or "Worked in legacy system",
        )
    )

    await repo.create_audit_event(
        db,
        case_id=case_id,
        actor=body.rep_id,
        action="mark_already_worked",
        data={"note": body.note},
    )
    await db.commit()

    logger.info(f"Case {case_id} marked as already worked by {body.rep_id}")
    return {"status": "ok", "case_id": case_id, "new_state": "ALREADY_WORKED"}
