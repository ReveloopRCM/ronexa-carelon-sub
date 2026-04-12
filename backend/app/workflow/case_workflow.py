"""CaseWorkflow — Restate Workflow keyed by case_id.

Two-job architecture:
  Job 1 (this workflow): Portal first pass → discover questions → save to DB → done
  Job 2 (SubmitWorkflow): Load approved answers → submit to portal → done

No awakeables, no suspension, no replay. Each job is a clean, independent
portal session. Review happens in the dashboard between jobs.
"""
from __future__ import annotations

import logging

import restate
from restate import WorkflowContext
from restate.exceptions import TerminalError

from app.compiler.portal_compiler import _save_question_batch

logger = logging.getLogger(__name__)

case_workflow = restate.Workflow("CaseWorkflow")


@case_workflow.main()
async def run(ctx: WorkflowContext, case_event: dict) -> dict:
    """Main workflow — first pass only.

    Args:
        case_event: {
            "worker_id": str,
            "case_data": dict,
            "clinical_context": dict | None,
            "auto_submit": bool,
            "credentials": dict | None,
        }

    Returns:
        {"status": "pending_review", "answers": [...]} — questions saved, awaiting rep
        {"status": "hold", ...} — case on hold
        {"status": "error", ...} — unrecoverable error
        {"status": "submitted", ...} — auto-submit completed (no review needed)
    """
    case_id = ctx.key()
    logger.info(
        f"CaseWorkflow/{case_id}: HANDLER ENTERED — "
        f"raw event keys: {list(case_event.keys()) if isinstance(case_event, dict) else type(case_event)}"
    )

    try:
        worker_id = case_event["worker_id"]
        case_data = case_event["case_data"]
        clinical_context = case_event.get("clinical_context")
        auto_submit = case_event.get("auto_submit", False)
        credentials = case_event.get("credentials")
        rerun_rep_answers = case_event.get("rerun_rep_answers")
        rerun_changed_group_id = case_event.get("rerun_changed_group_id")
    except Exception as e:
        logger.error(f"CaseWorkflow/{case_id}: failed to parse event: {e}")
        return {"status": "error", "error": f"Bad event: {e}"}

    logger.info(
        f"CaseWorkflow/{case_id}: starting "
        f"(worker={worker_id}, auto_submit={auto_submit})"
    )

    try:
        return await _run_case_workflow(
            ctx, case_id, worker_id, case_data,
            clinical_context, auto_submit, credentials,
            rerun_rep_answers=rerun_rep_answers,
            rerun_changed_group_id=rerun_changed_group_id,
        )
    except TerminalError:
        raise
    except Exception as e:
        logger.error(f"CaseWorkflow/{case_id}: unhandled error — converting to TerminalError: {e}")
        try:
            await ctx.run(
                "mark_hold_unhandled", _mark_case_hold_from_workflow,
                max_attempts=3, args=(case_id, f"Unhandled: {str(e)[:200]}"),
            )
        except Exception:
            pass
        raise TerminalError(f"CaseWorkflow/{case_id}: {str(e)[:300]}")


async def _run_case_workflow(
    ctx: WorkflowContext,
    case_id: str,
    worker_id: str,
    case_data: dict,
    clinical_context: dict | None,
    auto_submit: bool,
    credentials: dict | None,
    rerun_rep_answers: list[dict] | None = None,
    rerun_changed_group_id: int | None = None,
) -> dict:
    """First pass only — no awakeable, no suspension."""

    # ── Set case to PROCESSING ──
    try:
        await ctx.run("set_processing", _set_case_processing, max_attempts=3, args=(case_id,))
    except Exception as e:
        logger.error(f"CaseWorkflow/{case_id}: set_processing failed: {e}")
        return {"status": "error", "error": f"set_processing failed: {e}"}

    # ── First pass: portal navigation + clinical questions ──

    from app.workflow.worker_session import run_first_pass

    if rerun_rep_answers:
        logger.info(
            f"CaseWorkflow/{case_id}: RERUN with {len(rerun_rep_answers)} rep answers, "
            f"changed_group_id={rerun_changed_group_id}"
        )

    first_pass_arg = {
        "case_id": case_id,
        "case_data": case_data,
        "clinical_context": clinical_context,
        "credentials": credentials,
    }
    # Forward rerun context to WorkerSession → HTTP server → compiler
    if rerun_rep_answers is not None:
        first_pass_arg["rerun_rep_answers"] = rerun_rep_answers
        first_pass_arg["rerun_changed_group_id"] = rerun_changed_group_id

    try:
        first_pass_result = await ctx.object_call(
            run_first_pass,
            key=worker_id,
            arg=first_pass_arg,
        )
    except Exception as e:
        logger.error(f"CaseWorkflow/{case_id}: run_first_pass raised: {e}")
        await ctx.run(
            "mark_hold", _mark_case_hold_from_workflow,
            max_attempts=3, args=(case_id, f"WorkerSession error: {str(e)[:200]}"),
        )
        return {"status": "hold", "error": str(e)}

    logger.info(f"CaseWorkflow/{case_id}: first pass → {first_pass_result.get('status')}")

    # ── Save pathway metadata if present ──
    if first_pass_result.get("pathway"):
        from app.compiler.portal_compiler import _save_pathway_to_case
        await ctx.run(
            "save_pathway", _save_pathway_to_case,
            args=(case_id, first_pass_result["pathway"]),
        )

    # ── Non-review outcomes → return immediately ──
    if first_pass_result["status"] in ("hold", "error", "auto_approved"):
        return first_pass_result

    # ── Auto-submit: skip review, finalize in same session ──
    if auto_submit and first_pass_result["status"] == "review":
        logger.info(f"CaseWorkflow/{case_id}: AUTO-SUBMIT — skipping review")

        # Still save questions for data moat (AI answers recorded before outcome)
        await ctx.run(
            "save_questions_auto",
            _save_question_batch,
            max_attempts=3,
            args=(
                case_id,
                first_pass_result.get("answers", []),
                None,  # awakeable_id
                1,     # review_round
                first_pass_result.get("auto_approved"),
                first_pass_result.get("gold_card_level"),
                first_pass_result.get("algorithm_recommendation"),
            ),
        )

        from app.workflow.worker_session import run_finalize

        try:
            finalize_result = await ctx.object_call(
                run_finalize,
                key=worker_id,
                arg={
                    "case_id": case_id,
                    "case_data": case_data,
                    "clinical_context": clinical_context,
                    "approved_answers": first_pass_result.get("answers", []),
                    "credentials": credentials,
                },
            )
        except Exception as e:
            logger.error(f"CaseWorkflow/{case_id}: auto-submit finalize raised: {e}")
            await ctx.run(
                "mark_hold_auto", _mark_case_hold_from_workflow,
                max_attempts=3, args=(case_id, f"Auto-submit error: {str(e)[:200]}"),
            )
            return {"status": "hold", "error": str(e)}

        # Mark job complete
        await ctx.run("complete_job_auto", _complete_job, max_attempts=3, args=(case_id,))
        return finalize_result

    # ── Normal path: save questions for rep review ──
    # _save_question_batch handles bypass rules, L1/L2 routing, case state transitions
    is_rerun = rerun_rep_answers is not None
    await ctx.run(
        "save_questions",
        _save_question_batch,
        max_attempts=3,
        args=(
            case_id,
            first_pass_result.get("answers", []),
            None,  # awakeable_id (deprecated)
            1,     # review_round
            first_pass_result.get("auto_approved"),
            first_pass_result.get("gold_card_level"),
            first_pass_result.get("algorithm_recommendation"),
            is_rerun,
        ),
    )

    # Mark first-pass job as COMPLETED
    await ctx.run("complete_first_pass", _complete_job, max_attempts=3, args=(case_id,))

    logger.info(
        f"CaseWorkflow/{case_id}: first pass complete — "
        f"{len(first_pass_result.get('answers', []))} questions saved for review"
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
            data={"state": "PROCESSING"},
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


async def _mark_case_hold_from_workflow(case_id: str, reason: str) -> None:
    """Mark case as HOLD. Delegates to worker helpers for auto-requeue logic."""
    from app.worker.helpers import mark_case_hold
    await mark_case_hold(case_id, reason)
