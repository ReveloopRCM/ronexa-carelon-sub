"""OrderWorkflow — Restate Workflow keyed by case_id.

Processes cases that have an order form (file_key) but no clinical notes.
Same portal flow as CaseWorkflow but:
  - Uses order form OCR data as clinical context (separate prompt template)
  - Low-confidence answers route to WAITING_CLINICALS (not L1_REVIEW)
  - High-confidence answers route to normal L1/L2 review

When clinicals arrive later, sync_engine auto-transitions the case to
NOTES_UPLOADED → CaseWorkflow re-runs with full clinical context.
"""
from __future__ import annotations

import logging

import restate
from restate import WorkflowContext
from restate.exceptions import TerminalError

logger = logging.getLogger(__name__)

order_workflow = restate.Workflow("OrderWorkflow")


@order_workflow.main()
async def run(ctx: WorkflowContext, case_event: dict) -> dict:
    """Order-only first pass — same portal flow, different LLM context.

    Args:
        case_event: {
            "worker_id": str,
            "case_data": dict,
            "order_context": dict | None,   # Extracted order form (replaces clinical_context)
            "auto_submit": bool,
            "credentials": dict | None,
        }

    Returns:
        {"status": "pending_review", "answers": [...]}
        {"status": "waiting_clinicals", ...}
        {"status": "hold", ...}
        {"status": "error", ...}
    """
    case_id = ctx.key()
    logger.info(
        f"OrderWorkflow/{case_id}: HANDLER ENTERED — "
        f"raw event keys: {list(case_event.keys()) if isinstance(case_event, dict) else type(case_event)}"
    )

    try:
        worker_id = case_event["worker_id"]
        case_data = case_event["case_data"]
        order_context = case_event.get("order_context")
        auto_submit = case_event.get("auto_submit", False)
        credentials = case_event.get("credentials")
    except Exception as e:
        logger.error(f"OrderWorkflow/{case_id}: failed to parse event: {e}")
        return {"status": "error", "error": f"Bad event: {e}"}

    logger.info(
        f"OrderWorkflow/{case_id}: starting "
        f"(worker={worker_id}, has_order_context={order_context is not None})"
    )

    try:
        return await _run_order_workflow(
            ctx, case_id, worker_id, case_data,
            order_context, auto_submit, credentials,
        )
    except TerminalError:
        raise
    except Exception as e:
        logger.error(f"OrderWorkflow/{case_id}: unhandled error: {e}")
        try:
            await ctx.run(
                "mark_hold_unhandled", _mark_case_hold,
                max_attempts=3, args=(case_id, f"Unhandled: {str(e)[:200]}"),
            )
        except Exception:
            pass
        raise TerminalError(f"OrderWorkflow/{case_id}: {str(e)[:300]}")


async def _run_order_workflow(
    ctx: WorkflowContext,
    case_id: str,
    worker_id: str,
    case_data: dict,
    order_context: dict | None,
    auto_submit: bool,
    credentials: dict | None,
) -> dict:
    """Order-only first pass — identical portal flow to CaseWorkflow.

    The key difference: order_context is passed as clinical_context to the
    compiler/evaluator. The evaluator detects the order_context flag and uses
    the order-specific prompt template.
    """

    # ── Set case to PROCESSING ──
    try:
        await ctx.run("set_processing", _set_case_processing, max_attempts=3, args=(case_id,))
    except Exception as e:
        logger.error(f"OrderWorkflow/{case_id}: set_processing failed: {e}")
        return {"status": "error", "error": f"set_processing failed: {e}"}

    # ── First pass: same portal navigation + clinical questions ──
    # Pass order_context as clinical_context — the compiler passes it through
    # to the evaluator which uses the order-specific prompt template.
    from app.workflow.worker_session import run_first_pass

    first_pass_arg = {
        "case_id": case_id,
        "case_data": case_data,
        "clinical_context": order_context,  # Order form data used as clinical context
        "credentials": credentials,
        "order_mode": True,  # Signal to evaluator: use order prompt template
    }

    try:
        first_pass_result = await ctx.object_call(
            run_first_pass,
            key=worker_id,
            arg=first_pass_arg,
        )
    except Exception as e:
        logger.error(f"OrderWorkflow/{case_id}: run_first_pass raised: {e}")
        await ctx.run(
            "mark_hold", _mark_case_hold,
            max_attempts=3, args=(case_id, f"WorkerSession error: {str(e)[:200]}"),
        )
        return {"status": "hold", "error": str(e)}

    logger.info(f"OrderWorkflow/{case_id}: first pass → {first_pass_result.get('status')}")

    # ── Save pathway metadata if present ──
    if first_pass_result.get("pathway"):
        from app.compiler.portal_compiler import _save_pathway_to_case
        await ctx.run(
            "save_pathway", _save_pathway_to_case,
            args=(case_id, first_pass_result["pathway"]),
        )

    # ── Non-review outcomes → return immediately ──
    # NO_AUTH, GOLD_CARD, and auto-approved are detected during the same portal run.
    # http_server already handled state transitions for these:
    #   - "no_auth_review" → mark_case_no_auth_review() → L1_REVIEW
    #   - "physician_call_required" → mark_case_physician_call_required()
    #       → PHYSICIAN_CALL_REQUIRED (v158 bug fix — was falling through
    #       and overwritten by save_order_questions/WAITING_CLINICALS)
    #   - "hold" → mark_case_hold() → HOLD
    #   - "auto_approved" → zero-question pathway, needs state transition here
    status = first_pass_result.get("status", "")

    if status == "auto_approved":
        # Zero-question auto-approved pathway — http_server didn't set state
        await ctx.run(
            "mark_auto_approved", _mark_auto_approved,
            max_attempts=3, args=(case_id, first_pass_result),
        )
        return first_pass_result

    if status in ("hold", "error", "no_auth_review", "physician_call_required"):
        return first_pass_result

    # ── Save questions with order-specific routing ──
    # _save_order_question_batch: high confidence → L1/L2, low confidence → WAITING_CLINICALS
    from app.compiler.portal_compiler import _save_order_question_batch
    await ctx.run(
        "save_order_questions",
        _save_order_question_batch,
        max_attempts=3,
        args=(
            case_id,
            first_pass_result.get("answers", []),
            1,     # review_round
            first_pass_result.get("auto_approved"),
            first_pass_result.get("gold_card_level"),
            first_pass_result.get("algorithm_recommendation"),
        ),
    )

    # Mark job as COMPLETED
    await ctx.run("complete_job", _complete_job, max_attempts=3, args=(case_id,))

    logger.info(
        f"OrderWorkflow/{case_id}: complete — "
        f"{len(first_pass_result.get('answers', []))} questions processed"
    )

    return {
        "status": "pending_review",
        "answers": first_pass_result.get("answers", []),
    }


# ── DB Helpers (called via ctx.run for durability) ──


async def _set_case_processing(case_id: str) -> None:
    """Set case state to PROCESSING and job to RUNNING."""
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState, SubmissionJob, JobStatus
    from sqlalchemy import update

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)
        if case:
            case.state = CaseState.PROCESSING

        await db.execute(
            update(SubmissionJob)
            .where(SubmissionJob.case_id == case_id)
            .values(status=JobStatus.RUNNING)
        )

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action="state_change:PROCESSING",
            data={"state": "PROCESSING", "workflow": "OrderWorkflow"},
        )
        await db.commit()


async def _complete_job(case_id: str) -> None:
    """Mark the SubmissionJob as COMPLETED."""
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


async def _mark_auto_approved(case_id: str, first_pass_result: dict) -> None:
    """Handle zero-question auto-approved pathway.

    Portal auto-approved the case during the pipeline but returned 0 questions.
    http_server returned status='auto_approved' without setting case state.
    We mark the case as IN_REVIEW so a rep can confirm before submission.
    """
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState, SubmissionJob, JobStatus
    from sqlalchemy import update
    from sqlalchemy.sql import func

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)
        if case:
            case.state = CaseState.L1_REVIEW
            case.auto_approved = True
            result_data = first_pass_result.get("result", {})
            case.gold_card_level = result_data.get("gold_card_level")
            case.algorithm_recommendation = result_data.get("algorithm_recommendation")
            if (case.gold_card_level or 0) >= 2:
                case.approval_type = "gold_card"
            else:
                case.approval_type = "auto_approved"

        await db.execute(
            update(SubmissionJob)
            .where(SubmissionJob.case_id == case_id)
            .values(status=JobStatus.COMPLETED, completed_at=func.now())
        )

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action="state_change:IN_REVIEW",
            data={
                "state": "IN_REVIEW",
                "reason": "Zero-question auto-approved pathway",
                "workflow": "OrderWorkflow",
                "auto_approved": True,
            },
        )
        await db.commit()

    logger.info(f"OrderWorkflow/{case_id}: zero-question auto-approved → IN_REVIEW")


async def _mark_case_hold(case_id: str, reason: str) -> None:
    """Mark case as HOLD. Delegates to worker helpers for auto-requeue logic."""
    from app.worker.helpers import mark_case_hold
    await mark_case_hold(case_id, reason)
