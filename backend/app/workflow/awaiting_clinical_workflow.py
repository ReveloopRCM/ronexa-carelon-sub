"""AwaitingClinicalWorkflow — Restate Workflow keyed by case_id.

Processes PENDING_NOTES cases through signature replay or gold card detection.
Uses idle worker time (secondary job type, no dedicated VMs).

Flow:
  1. Load algorithm signature for case's CPT+ICD combo
  2. Run portal first pass with signature replay (resume_answers from signature)
  3. If algorithm approved (RecType=3) → CLINICAL_REVIEW (rep verifies with clinicals)
  4. If not approved → back to PENDING_NOTES (signature didn't work)
  5. Gold card cases → CLINICAL_REVIEW (detected during portal flow)

Nothing auto-submits. Every case in CLINICAL_REVIEW is touched by a rep.
"""
from __future__ import annotations

import logging

import restate
from restate import WorkflowContext
from restate.exceptions import TerminalError

logger = logging.getLogger(__name__)

awaiting_clinical_workflow = restate.Workflow("AwaitingClinicalWorkflow")


@awaiting_clinical_workflow.main()
async def run(ctx: WorkflowContext, case_event: dict) -> dict:
    """Main workflow — signature replay for awaiting-clinicals cases.

    Args:
        case_event: {
            "worker_id": str,
            "case_data": dict,
            "credentials": dict | None,
        }
    """
    case_id = ctx.key()
    logger.info(f"AwaitingClinicalWorkflow/{case_id}: HANDLER ENTERED")

    try:
        worker_id = case_event["worker_id"]
        case_data = case_event["case_data"]
        credentials = case_event.get("credentials")
    except Exception as e:
        logger.error(f"AwaitingClinicalWorkflow/{case_id}: failed to parse event: {e}")
        return {"status": "error", "error": f"Bad event: {e}"}

    try:
        return await _run_awaiting_workflow(
            ctx, case_id, worker_id, case_data, credentials,
        )
    except TerminalError:
        raise
    except Exception as e:
        logger.error(f"AwaitingClinicalWorkflow/{case_id}: unhandled error: {e}")
        # Reset to PENDING_NOTES — don't HOLD awaiting-clinicals cases for transient errors
        try:
            await ctx.run(
                "reset_pending", _reset_to_pending_notes,
                max_attempts=3, args=(case_id, f"Signature replay error: {str(e)[:200]}"),
            )
        except Exception:
            pass
        raise TerminalError(f"AwaitingClinicalWorkflow/{case_id}: {str(e)[:300]}")


async def _run_awaiting_workflow(
    ctx: WorkflowContext,
    case_id: str,
    worker_id: str,
    case_data: dict,
    credentials: dict | None,
) -> dict:
    """Signature replay flow — no clinicals needed."""

    # ── Step 1: Load algorithm signature for this CPT+ICD ──
    signature_data = await ctx.run(
        "load_signature", _load_signature,
        max_attempts=3, args=(case_id, case_data.get("cpt_code"), case_data.get("icd1")),
    )

    if not signature_data:
        logger.info(f"AwaitingClinicalWorkflow/{case_id}: no signature found — staying PENDING_NOTES")
        await ctx.run("complete_job", _complete_job, max_attempts=3, args=(case_id,))
        return {"status": "no_signature", "case_id": case_id}

    # ── Step 2: Set case to PROCESSING ──
    await ctx.run(
        "set_processing", _set_case_processing,
        max_attempts=3, args=(case_id, signature_data["signature_id"]),
    )

    # ── Step 3: Run portal with signature replay via worker HTTP ──
    from app.workflow.worker_session import run_first_pass

    try:
        first_pass_result = await ctx.object_call(
            run_first_pass,
            key=worker_id,
            arg={
                "case_id": case_id,
                "case_data": case_data,
                "clinical_context": None,  # No clinicals
                "credentials": credentials,
                "signature_replay": True,
                "resume_answers": signature_data["resume_answers"],
            },
        )
    except Exception as e:
        logger.error(f"AwaitingClinicalWorkflow/{case_id}: first pass raised: {e}")
        await ctx.run(
            "reset_pending", _reset_to_pending_notes,
            max_attempts=3, args=(case_id, f"Signature replay portal error: {str(e)[:200]}"),
        )
        return {"status": "error", "error": str(e)}

    logger.info(
        f"AwaitingClinicalWorkflow/{case_id}: first pass → {first_pass_result.get('status')}, "
        f"auto_approved={first_pass_result.get('auto_approved')}, "
        f"algorithm_recommendation={first_pass_result.get('algorithm_recommendation')}"
    )

    # ── Step 4: Handle result ──
    status = first_pass_result.get("status")

    if status in ("hold", "error"):
        # Portal error — reset to PENDING_NOTES, don't block the case
        await ctx.run(
            "reset_pending", _reset_to_pending_notes,
            max_attempts=3, args=(case_id, first_pass_result.get("hold_reason", "Signature replay failed")),
        )
        await ctx.run("complete_job", _complete_job, max_attempts=3, args=(case_id,))
        return first_pass_result

    if status == "no_auth_required":
        await ctx.run("complete_job", _complete_job, max_attempts=3, args=(case_id,))
        return first_pass_result

    # Check algorithm result
    algo_rec = first_pass_result.get("algorithm_recommendation")
    auto_approved = first_pass_result.get("auto_approved")
    gold_card = (first_pass_result.get("gold_card_level") or 0) >= 2

    if auto_approved or gold_card:
        # Algorithm approved or gold card → CLINICAL_REVIEW for rep verification
        await ctx.run(
            "save_questions",
            _save_signature_questions,
            max_attempts=3,
            args=(
                case_id,
                first_pass_result.get("answers", []),
                signature_data["signature_id"],
                auto_approved,
                first_pass_result.get("gold_card_level"),
                first_pass_result.get("algorithm_recommendation"),
            ),
        )

        await ctx.run(
            "set_clinical_review", _set_clinical_review,
            max_attempts=3, args=(case_id, signature_data["signature_id"], gold_card),
        )

        # Update signature stats
        await ctx.run(
            "update_sig_stats", _increment_signature_stats,
            max_attempts=3, args=(signature_data["signature_id"], True),
        )

        await ctx.run("complete_job", _complete_job, max_attempts=3, args=(case_id,))

        logger.info(
            f"AwaitingClinicalWorkflow/{case_id}: → CLINICAL_REVIEW "
            f"(gold_card={gold_card}, algo={algo_rec})"
        )
        return {
            "status": "clinical_review",
            "auto_approved": auto_approved,
            "gold_card": gold_card,
            "algorithm_recommendation": algo_rec,
        }

    else:
        # Not algorithm approved — signature didn't work for this case
        # Reset to PENDING_NOTES, keep waiting for clinicals
        await ctx.run(
            "reset_pending", _reset_to_pending_notes,
            max_attempts=3, args=(case_id, f"Signature replay not approved (RecType={algo_rec})"),
        )

        # Track failed replay
        await ctx.run(
            "update_sig_stats", _increment_signature_stats,
            max_attempts=3, args=(signature_data["signature_id"], False),
        )

        await ctx.run("complete_job", _complete_job, max_attempts=3, args=(case_id,))

        logger.info(
            f"AwaitingClinicalWorkflow/{case_id}: signature replay not approved "
            f"(RecType={algo_rec}) — back to PENDING_NOTES"
        )
        return {"status": "not_approved", "algorithm_recommendation": algo_rec}


# ── DB Helpers (called via ctx.run for durability) ──


async def _load_signature(case_id: str, cpt_code: str, icd1: str | None) -> dict | None:
    """Load algorithm signature and convert to resume_answers format."""
    from app.db.database import async_session_factory
    from app.db.outcome_db import get_signature_for_case

    if not cpt_code:
        return None

    async with async_session_factory() as db:
        sig = await get_signature_for_case(db, cpt_code, icd1)
        if not sig:
            return None

        # Convert qa_sequence to portal resume_answers format
        resume_answers = []
        for qa in sig.qa_sequence:
            answer_value = qa.get("answer_value")
            # Normalize to Values list
            if isinstance(answer_value, list):
                values = answer_value
            elif isinstance(answer_value, dict):
                val = answer_value.get("Value") or answer_value.get("Values")
                values = val if isinstance(val, list) else [str(val)] if val else []
            elif answer_value:
                values = [str(answer_value)]
            else:
                values = []

            resume_answers.append({
                "QuestionId": qa.get("question_id", ""),
                "QuestionType": qa.get("question_type", 3),
                "GroupId": qa.get("group_id", 0),
                "Sequence": qa.get("sequence", 0),
                "Values": values,
                "question_text": qa.get("question_text", ""),
                "answer_text": qa.get("answer_text", ""),
                "question_id": qa.get("question_id", ""),
                "answer_value": values,
                "group_id": qa.get("group_id", 0),
            })

        logger.info(
            f"AwaitingClinicalWorkflow: loaded signature {sig.id[:8]} for "
            f"{cpt_code}/{icd1}/{sig.pathway_id} ({len(resume_answers)} Q&As)"
        )
        return {
            "signature_id": sig.id,
            "pathway_id": sig.pathway_id,
            "resume_answers": resume_answers,
        }


async def _set_case_processing(case_id: str, signature_id: str) -> None:
    """Set case state to PROCESSING with signature_replay flag."""
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState, SubmissionJob, JobStatus
    from sqlalchemy import update

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)
        if case:
            case.state = CaseState.PROCESSING
            case.signature_replay = True
            case.signature_id = signature_id

        await db.execute(
            update(SubmissionJob)
            .where(SubmissionJob.case_id == case_id)
            .values(status=JobStatus.RUNNING)
        )

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action="state_change:PROCESSING",
            data={"state": "PROCESSING", "signature_replay": True, "signature_id": signature_id},
        )
        await db.commit()


async def _set_clinical_review(case_id: str, signature_id: str, is_gold_card: bool) -> None:
    """Set case state to CLINICAL_REVIEW."""
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)
        if case:
            case.state = CaseState.CLINICAL_REVIEW

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action="state_change:CLINICAL_REVIEW",
            data={
                "state": "CLINICAL_REVIEW",
                "signature_replay": True,
                "signature_id": signature_id,
                "is_gold_card": is_gold_card,
            },
        )
        await db.commit()


async def _save_signature_questions(
    case_id: str,
    answers: list[dict],
    signature_id: str,
    auto_approved: bool | None,
    gold_card_level: int | None,
    algorithm_recommendation: int | None,
) -> None:
    """Save questions from signature replay to DB."""
    from app.compiler.portal_compiler import _save_question_batch
    await _save_question_batch(
        case_id=case_id,
        answers=answers,
        awakeable_id=None,
        review_round=1,
        auto_approved=auto_approved,
        gold_card_level=gold_card_level,
        algorithm_recommendation=algorithm_recommendation,
    )


async def _reset_to_pending_notes(case_id: str, reason: str) -> None:
    """Reset case back to PENDING_NOTES — signature replay didn't work."""
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)
        if case:
            case.state = CaseState.PENDING_NOTES
            case.signature_replay = False
            case.signature_id = None

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action="state_change:PENDING_NOTES",
            data={"state": "PENDING_NOTES", "reason": reason, "from": "signature_replay"},
        )
        await db.commit()


async def _increment_signature_stats(signature_id: str, success: bool) -> None:
    """Update signature usage stats."""
    from app.db.database import async_session_factory
    from app.db.models import AlgorithmSignature

    async with async_session_factory() as db:
        sig = await db.get(AlgorithmSignature, signature_id)
        if sig:
            sig.times_used = (sig.times_used or 0) + 1
            if success:
                sig.success_count = (sig.success_count or 0) + 1
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
