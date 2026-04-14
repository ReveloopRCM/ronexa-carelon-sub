"""SubmitWorkflow — Restate Workflow keyed by case_id.

Job 2 in the two-job architecture:
  1. Load approved answers from DB (Question records)
  2. Submit to portal via WorkerSession.run_finalize (worker_id from WorkerLoop)
  3. Handle fax for pended cases (inline, no awakeable)

Dispatched by WorkerLoop with job_type="SUBMIT". Any worker can run submissions.
"""
from __future__ import annotations

import logging

import restate
from restate import WorkflowContext
from restate.exceptions import TerminalError

logger = logging.getLogger(__name__)

submit_workflow = restate.Workflow("SubmitWorkflow")

@submit_workflow.main()
async def run(ctx: WorkflowContext, event: dict) -> dict:
    """Submit a case to the portal with approved answers.

    Args:
        event: {
            "worker_id": str,           # From WorkerLoop (generic, not hardcoded)
            "case_data": dict,          # Full case dict from DB
            "clinical_context": dict,   # Extracted clinical notes (or None)
            "credentials": dict,        # Portal login credentials
        }

    Returns:
        {"status": "submitted", "result": {...}}
        {"status": "hold", "hold_reason": "..."}
    """
    case_id = ctx.key()
    logger.info(f"SubmitWorkflow/{case_id}: HANDLER ENTERED")

    try:
        return await _run_submit(ctx, case_id, event)
    except TerminalError:
        raise
    except Exception as e:
        logger.error(f"SubmitWorkflow/{case_id}: unhandled error: {e}")
        try:
            await ctx.run(
                "mark_hold", _mark_case_hold,
                max_attempts=3, args=(case_id, f"Submit error: {str(e)[:200]}"),
            )
        except Exception:
            pass
        raise TerminalError(f"SubmitWorkflow/{case_id}: {str(e)[:300]}")


async def _run_submit(ctx: WorkflowContext, case_id: str, event: dict) -> dict:
    """Linear submit flow — no loops, no suspension."""

    # ── Set case to SUBMITTING ──
    await ctx.run("set_submitting", _set_case_submitting, max_attempts=3, args=(case_id,))

    # ── Load approved answers from DB ──
    approved_answers = await ctx.run(
        "load_answers", _load_approved_answers, max_attempts=3, args=(case_id,),
    )

    if not approved_answers:
        logger.warning(f"SubmitWorkflow/{case_id}: no approved answers found")
        await ctx.run(
            "mark_hold_no_answers", _mark_case_hold,
            max_attempts=3, args=(case_id, "No approved answers found for submission"),
        )
        return {"status": "hold", "hold_reason": "No approved answers"}

    logger.info(f"SubmitWorkflow/{case_id}: loaded {len(approved_answers)} approved answers")

    # ── Submit via assigned worker (from WorkerLoop) ──
    worker_id = event.get("worker_id")
    if not worker_id:
        logger.error(f"SubmitWorkflow/{case_id}: no worker_id in event — likely stale invocation from purge")
        await ctx.run(
            "mark_hold_no_worker", _mark_case_hold,
            max_attempts=3, args=(case_id, "No worker_id in SubmitWorkflow event — stale invocation"),
        )
        return {"status": "hold", "hold_reason": "No worker_id — stale invocation"}
    from app.workflow.worker_session import run_finalize

    try:
        result = await ctx.object_call(
            run_finalize,
            key=worker_id,
            arg={
                "case_id": case_id,
                "case_data": event.get("case_data", {}),
                "clinical_context": event.get("clinical_context"),
                "approved_answers": approved_answers,
                "credentials": event.get("credentials"),
            },
        )
    except Exception as e:
        logger.error(f"SubmitWorkflow/{case_id}: run_finalize raised: {e}")
        await ctx.run(
            "mark_hold_finalize", _mark_case_hold,
            max_attempts=3, args=(case_id, f"Finalize error: {str(e)[:200]}"),
        )
        return {"status": "hold", "error": str(e)}

    # ── Handle result ──
    if result.get("status") == "hold":
        hold_reason = result.get("hold_reason", "Unknown finalize error")
        # mark_case_hold already called by http_server.py
        logger.warning(f"SubmitWorkflow/{case_id}: finalize → HOLD: {hold_reason}")
        return result

    # ── Mark job complete ──
    await ctx.run("complete_job", _complete_submit_job, max_attempts=3, args=(case_id,))

    # ── Fax for pended cases (inline, no awakeable) ──
    result_data = result.get("result", {})
    if result_data.get("fax_needed"):
        case_data = event.get("case_data", {})
        fax_validate = await ctx.run(
            "check_fax_validation", _check_fax_validation_needed, max_attempts=3,
        )
        if fax_validate:
            # Set case to PENDED_FAX_REVIEW — rep will validate in dashboard
            # and POST /api/queue/{case_id}/validate-fax which sends fax inline
            await ctx.run(
                "set_pended_fax_review", _set_pended_fax_review,
                max_attempts=3, args=(case_id,),
            )
            logger.info(f"SubmitWorkflow/{case_id}: pended → PENDED_FAX_REVIEW for rep validation")
        else:
            # Auto-fax (fax validation disabled)
            await ctx.run(
                "send_fax", _send_fax_for_case,
                max_attempts=2, args=(case_id, case_data, result_data),
            )

    logger.info(
        f"SubmitWorkflow/{case_id}: complete — "
        f"status={result.get('status')}, auth={result_data.get('auth_number')}"
    )
    return result


# ── DB Helpers ──


async def _set_case_submitting(case_id: str) -> None:
    """Set case state to SUBMITTING and job to RUNNING."""
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState, SubmissionJob, JobStatus
    from sqlalchemy import update

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)
        if case:
            case.state = CaseState.SUBMITTING

        await db.execute(
            update(SubmissionJob)
            .where(SubmissionJob.case_id == case_id)
            .values(status=JobStatus.RUNNING)
        )

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action="state_change:SUBMITTING",
            data={"state": "SUBMITTING"},
        )
        await db.commit()


async def _load_approved_answers(case_id: str) -> list[dict]:
    """Build portal-format answer list from approved/edited Question records.

    Uses rep_answer (if edited) over ai_answer. Returns portal-format dicts:
        {QuestionId, QuestionType, GroupId, Sequence, Values}

    Filters out legacy GroupId=0 / pathway_selection questions.
    """
    from app.db.database import async_session_factory
    from app.db.models import Question, ReviewState
    from sqlalchemy import select

    async with async_session_factory() as db:
        result = await db.execute(
            select(Question).where(
                Question.case_id == case_id,
                Question.review_state.in_([
                    ReviewState.REP_APPROVED,
                    ReviewState.REP_EDITED,
                    ReviewState.AI_SUGGESTED,   # Include AI answers if not yet reviewed
                ]),
                Question.group_id != 0,         # Skip legacy GroupId=0
            ).order_by(Question.group_id, Question.sequence)
        )
        questions = result.scalars().all()

    answers = []
    for q in questions:
        if q.portal_question_id == "pathway_selection":
            continue  # Skip legacy pathway questions

        # Use rep_answer if available, otherwise ai_answer
        if q.rep_answer and isinstance(q.rep_answer, dict) and q.rep_answer.get("Value"):
            val = q.rep_answer["Value"]
            values = val if isinstance(val, list) else [val]
        elif isinstance(q.ai_answer, dict) and "Values" in q.ai_answer:
            # ai_answer already in portal format
            values = q.ai_answer["Values"]
        elif isinstance(q.ai_answer, dict) and "Value" in q.ai_answer:
            val = q.ai_answer["Value"]
            values = val if isinstance(val, list) else [val]
        else:
            values = []

        # If ai_answer has the full portal structure, start from it
        if isinstance(q.ai_answer, dict) and "QuestionId" in q.ai_answer:
            portal_ans = dict(q.ai_answer)
            portal_ans["Values"] = values  # Override with rep value if edited
            # Add text fields for submission answer matching (Tier 2 fallback)
            portal_ans["question_text"] = q.question_text
            portal_ans["options_json"] = q.options_json
            answers.append(portal_ans)
        else:
            answers.append({
                "QuestionId": q.portal_question_id,
                "QuestionType": q.question_type,
                "GroupId": q.group_id,
                "Sequence": q.sequence or 0,
                "Values": values,
                "question_text": q.question_text,       # For submission matching
                "options_json": q.options_json,          # For submission matching
            })

    return answers


async def _complete_submit_job(case_id: str) -> None:
    """Mark the submit job as COMPLETED."""
    from app.db.database import async_session_factory
    from app.db.models import SubmissionJob, JobStatus
    from sqlalchemy import update
    from sqlalchemy.sql import func

    async with async_session_factory() as db:
        await db.execute(
            update(SubmissionJob)
            .where(SubmissionJob.case_id == case_id)
            .values(status=JobStatus.COMPLETED, completed_at=func.now())
        )
        await db.commit()


async def _mark_case_hold(case_id: str, reason: str) -> None:
    """Mark case as HOLD."""
    from app.worker.helpers import mark_case_hold
    await mark_case_hold(case_id, reason)


async def _check_fax_validation_needed() -> bool:
    """Check if fax_validation_enabled setting is True."""
    from app.db.database import async_session_factory
    from app.db.models import SystemSetting

    async with async_session_factory() as db:
        setting = await db.get(SystemSetting, "fax_validation_enabled")
        return setting.value if setting else True


async def _set_pended_fax_review(case_id: str) -> None:
    """Set case to PENDED_FAX_REVIEW for rep validation."""
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)
        if case:
            case.state = CaseState.PENDED_FAX_REVIEW

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action="fax_validation_pending",
            data={},
        )
        await db.commit()


async def _send_fax_for_case(
    case_id: str, case_data: dict, result: dict
) -> None:
    """Send clinical notes fax for a pended case (RingCentral API)."""
    from app.services.ringcentral_fax import fax_clinical_notes
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState

    clinical_blob_key = case_data.get("clinical_blob_key") or case_data.get("file_key")
    if not clinical_blob_key:
        logger.warning(f"SubmitWorkflow/{case_id}: no clinical blob key — cannot fax")
        return

    order_id = result.get("auth_number", result.get("order_id", ""))
    patient_name = f"{case_data.get('last_name', '')} {case_data.get('first_name', '')}"
    cpt = case_data.get("cpt_code", "")

    fax_result = await fax_clinical_notes(
        clinical_blob_key=clinical_blob_key,
        order_id=order_id,
        patient_name=patient_name,
        cpt_code=cpt,
        member_id=case_data.get("policy_num", ""),
        dob=case_data.get("dob", ""),
    )

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)
        if case:
            case.state = CaseState.PENDED

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action="fax_sent",
            data={
                k: v for k, v in fax_result.items()
                if isinstance(v, (str, int, float, bool, type(None)))
            },
        )
        await db.commit()

    logger.info(f"SubmitWorkflow/{case_id}: fax result: {fax_result}")
