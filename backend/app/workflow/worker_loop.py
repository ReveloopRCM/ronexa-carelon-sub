"""WorkerLoop — Restate VirtualObject (keyed by worker_id).

Pull-based streaming: each worker continuously claims the next case from the
shared Postgres queue, processes it through CaseWorkflow, then claims the next.

When the queue is empty the worker suspends on an awakeable (zero cost).
Sync wakes all sleeping workers instantly after enqueuing new cases.
A 5-minute fallback timer ensures workers self-wake even if the signal is lost.

Usage:
    POST /WorkerLoop/{worker_id}/start/send   {"max_cases": null}
    POST /WorkerLoop/{worker_id}/request_stop
    GET  /WorkerLoop/{worker_id}/status
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import restate
from restate import ObjectContext, ObjectSharedContext
from restate.exceptions import TerminalError

logger = logging.getLogger(__name__)

worker_loop = restate.VirtualObject("WorkerLoop")


# ── Handlers ──


@worker_loop.handler()
async def start(ctx: ObjectContext, config: dict) -> dict:
    """Main pull loop — runs until stopped or queue exhausted.

    config:
        max_cases: int | None   — None = unlimited (run until stopped)
        stop_on_empty: bool     — True = stop when queue empty (for manual trigger)
        job_type: str           — "FIRST_PASS" or "SUBMIT" (default: "FIRST_PASS")
    """
    worker_id = ctx.key()
    max_cases = config.get("max_cases")
    stop_on_empty = config.get("stop_on_empty", False)
    job_type = config.get("job_type", "FIRST_PASS")

    try:
        # Load credentials once at loop start
        credentials = await ctx.run(
            "load_creds", _load_worker_credentials, max_attempts=3, args=(worker_id,)
        )
        if not credentials:
            raise TerminalError(f"WorkerLoop/{worker_id}: no active worker account found")

        cases_processed = 0
        iteration = 0
        consecutive_empty = 0

        while True:
            iteration += 1

            # 1. Check stop flag (DB-backed, writable by shared handler)
            should_stop = await ctx.run(
                f"stop_check_{iteration}",
                _check_stop_flag,
                max_attempts=3,
                args=(worker_id,),
            )
            if should_stop:
                logger.info(f"WorkerLoop/{worker_id}: stop flag set — exiting after {cases_processed} cases")
                break

            # 2. Claim next case from queue — 4-level priority cascade:
            #    SUBMIT → FIRST_PASS → ORDER → SIGNATURE_REPLAY
            #    Every worker tries ALL job types in priority order.
            event = None
            for jt in ("SUBMIT", "FIRST_PASS", "ORDER", "SIGNATURE_REPLAY"):
                event = await ctx.run(
                    f"claim_{jt}_{iteration}",
                    _claim_and_build_event,
                    max_attempts=3,
                    args=(worker_id, jt),
                )
                if event is not None:
                    break

            if event is None:
                # All queues empty
                consecutive_empty += 1

                if stop_on_empty:
                    logger.info(f"WorkerLoop/{worker_id}: queue empty, stop_on_empty=True — exiting")
                    break

                # Close browser after 2 consecutive empty cycles to free resources
                if consecutive_empty >= 2:
                    try:
                        from app.workflow.worker_session import close_browser as ws_close
                        ctx.object_send(ws_close, key=worker_id, arg={"worker_id": worker_id})
                        logger.info(f"WorkerLoop/{worker_id}: closed browser after {consecutive_empty} empty cycles")
                    except Exception as e:
                        logger.warning(f"WorkerLoop/{worker_id}: close_browser send failed: {e}")
                    consecutive_empty = 0

                # Hybrid: suspend on awakeable + 5 min fallback
                wake_id, wake_promise = ctx.awakeable()
                await ctx.run(
                    f"save_wake_{iteration}",
                    _save_wake_awakeable,
                    max_attempts=3,
                    args=(worker_id, wake_id),
                )
                logger.info(f"WorkerLoop/{worker_id}: queue empty — sleeping (awakeable + 5 min fallback)")

                # Sleep 5 min as fallback; sync can resolve the awakeable sooner
                await ctx.sleep(timedelta(minutes=5))

                # Clear awakeable so sync doesn't try to resolve stale ones
                await ctx.run(
                    f"clear_wake_{iteration}",
                    _clear_wake_awakeable,
                    max_attempts=3,
                    args=(worker_id,),
                )
                continue

            # Reset empty counter on successful claim
            consecutive_empty = 0

            # 3. Dispatch case — fire-and-forget so loop continues immediately.
            #    CaseWorkflow manages its own lifecycle (review suspension,
            #    finalize, error→HOLD). WorkerLoop just keeps feeding it work.
            case_id = event["case_id"]
            logger.info(
                f"WorkerLoop/{worker_id}: dispatching case {case_id} "
                f"(#{cases_processed + 1}, iteration={iteration})"
            )

            if event.get("job_type") == "SUBMIT":
                from app.workflow.submit_workflow import run as submit_workflow_run

                ctx.workflow_send(
                    submit_workflow_run,
                    key=case_id,
                    arg={
                        "worker_id": worker_id,
                        "case_data": event["case_data"],
                        "clinical_context": event.get("clinical_context"),
                        "credentials": credentials,
                    },
                )
            elif event.get("job_type") == "ORDER":
                from app.workflow.order_workflow import run as order_workflow_run

                ctx.workflow_send(
                    order_workflow_run,
                    key=case_id,
                    arg={
                        "worker_id": worker_id,
                        "case_data": event["case_data"],
                        "order_context": event.get("clinical_context"),  # Order form OCR data
                        "credentials": credentials,
                    },
                )
            elif event.get("job_type") == "SIGNATURE_REPLAY":
                from app.workflow.awaiting_clinical_workflow import run as awaiting_workflow_run

                ctx.workflow_send(
                    awaiting_workflow_run,
                    key=case_id,
                    arg={
                        "worker_id": worker_id,
                        "case_data": event["case_data"],
                        "credentials": credentials,
                    },
                )
            else:
                from app.workflow.case_workflow import run as case_workflow_run

                workflow_arg = {
                    "worker_id": worker_id,
                    "case_data": event["case_data"],
                    "clinical_context": event.get("clinical_context"),
                    "auto_submit": event.get("auto_submit", False),
                    "credentials": credentials,
                }
                # Forward rerun context so CaseWorkflow → WorkerSession → HTTP
                # server can use the rep's edited answers for backtrack replay
                if event.get("rerun_rep_answers") is not None:
                    workflow_arg["rerun_rep_answers"] = event["rerun_rep_answers"]
                    workflow_arg["rerun_changed_group_id"] = event.get("rerun_changed_group_id")
                    logger.info(
                        f"WorkerLoop/{worker_id}: forwarding {len(event['rerun_rep_answers'])} "
                        f"rep answers to CaseWorkflow (changed_group_id="
                        f"{event.get('rerun_changed_group_id')})"
                    )

                ctx.workflow_send(
                    case_workflow_run,
                    key=case_id,
                    arg=workflow_arg,
                )
            logger.info(f"WorkerLoop/{worker_id}: case {case_id} dispatched ({job_type})")

            # Brief pause between dispatches — WorkerSession is exclusive per
            # worker so cases queue in Restate.  This prevents claiming the
            # entire DB queue at once and gives the worker time to finish each
            # first-pass before we pile up too many pending invocations.
            await ctx.sleep(timedelta(seconds=10))

            cases_processed += 1
            if max_cases and cases_processed >= max_cases:
                logger.info(f"WorkerLoop/{worker_id}: reached max_cases={max_cases} — exiting")
                break

        # Clear stop flag on exit
        await ctx.run("clear_stop", _clear_stop_flag, max_attempts=3, args=(worker_id,))

        # Close browser on loop exit — start fresh next time
        try:
            from app.workflow.worker_session import close_browser as ws_close
            ctx.object_send(ws_close, key=worker_id, arg={"worker_id": worker_id})
            logger.info(f"WorkerLoop/{worker_id}: sent close_browser on exit")
        except Exception as e:
            logger.warning(f"WorkerLoop/{worker_id}: close_browser on exit failed: {e}")

        return {"worker_id": worker_id, "cases_processed": cases_processed, "status": "stopped"}

    except TerminalError:
        raise
    except Exception as e:
        logger.error(f"WorkerLoop/{worker_id}: unhandled error: {e}")
        raise TerminalError(f"WorkerLoop/{worker_id}: {str(e)[:300]}")


@worker_loop.handler(kind="shared")
async def request_stop(ctx: ObjectSharedContext, _: None = None) -> dict:
    """Non-blocking stop signal. Shared handler doesn't queue behind start."""
    worker_id = ctx.key()
    await _set_stop_flag_sync(worker_id)
    logger.info(f"WorkerLoop/{worker_id}: stop requested")
    return {"status": "stop_requested", "worker_id": worker_id}


@worker_loop.handler(kind="shared")
async def status(ctx: ObjectSharedContext, _: None = None) -> dict:
    """Non-blocking status check."""
    worker_id = ctx.key()
    return await _get_worker_status_sync(worker_id)


# ── DB Helpers (called via ctx.run — must be plain async functions) ──


async def _load_worker_credentials(worker_id: str) -> dict | None:
    """Load credentials for a worker from WorkerAccount table."""
    from app.db.database import async_session_factory
    from app.db.models import WorkerAccount
    from sqlalchemy import select

    async with async_session_factory() as db:
        result = await db.execute(
            select(WorkerAccount)
            .where(
                WorkerAccount.container_id == worker_id,
                WorkerAccount.is_active == True,
            )
        )
        account = result.scalar_one_or_none()
        if not account:
            return None

        return {
            "username": account.username,
            "password": account.password,
            "mailbox": account.mailbox_address,
        }


async def _check_stop_flag(worker_id: str) -> bool:
    """Check if stop has been requested for this worker."""
    from app.db.database import async_session_factory
    from app.db.models import WorkerAccount
    from sqlalchemy import select

    async with async_session_factory() as db:
        result = await db.execute(
            select(WorkerAccount.stop_requested)
            .where(WorkerAccount.container_id == worker_id)
        )
        row = result.scalar_one_or_none()
        return bool(row) if row is not None else False


async def _set_stop_flag_sync(worker_id: str) -> None:
    """Set the stop flag — called from shared handler (direct DB write)."""
    from app.db.database import async_session_factory
    from app.db.models import WorkerAccount
    from sqlalchemy import update

    async with async_session_factory() as db:
        await db.execute(
            update(WorkerAccount)
            .where(WorkerAccount.container_id == worker_id)
            .values(stop_requested=True)
        )
        await db.commit()


async def _clear_stop_flag(worker_id: str) -> None:
    """Clear the stop flag — called at loop exit."""
    from app.db.database import async_session_factory
    from app.db.models import WorkerAccount
    from sqlalchemy import update

    async with async_session_factory() as db:
        await db.execute(
            update(WorkerAccount)
            .where(WorkerAccount.container_id == worker_id)
            .values(stop_requested=False)
        )
        await db.commit()


async def _save_wake_awakeable(worker_id: str, awakeable_id: str) -> None:
    """Store the awakeable ID so sync can resolve it to wake the worker."""
    from app.db.database import async_session_factory
    from app.db.models import WorkerAccount
    from sqlalchemy import update

    async with async_session_factory() as db:
        await db.execute(
            update(WorkerAccount)
            .where(WorkerAccount.container_id == worker_id)
            .values(wake_awakeable_id=awakeable_id)
        )
        await db.commit()


async def _clear_wake_awakeable(worker_id: str) -> None:
    """Clear the awakeable ID after waking."""
    from app.db.database import async_session_factory
    from app.db.models import WorkerAccount
    from sqlalchemy import update

    async with async_session_factory() as db:
        await db.execute(
            update(WorkerAccount)
            .where(WorkerAccount.container_id == worker_id)
            .values(wake_awakeable_id=None)
        )
        await db.commit()


async def _claim_and_build_event(worker_id: str, job_type: str | None = None) -> dict | None:
    """Claim next job from queue, load case data + clinical context + auto_submit.

    Returns the full event dict ready for CaseWorkflow/SubmitWorkflow, or None if queue empty.
    Reuses existing claim_next_job() from queue.py.
    """
    from app.db.database import async_session_factory
    from app.db.models import Case, ClinicalNote, AutomationRule, Question
    from app.db.queue import claim_next_job
    from sqlalchemy import select

    async with async_session_factory() as db:
        job = await claim_next_job(db, worker_id, job_type=job_type)
        if not job:
            await db.commit()
            return None

        # Load case data
        case = await db.get(Case, job.case_id)
        if not case:
            logger.error(f"WorkerLoop/{worker_id}: claimed job {job.id} but case {job.case_id} not found")
            await db.commit()
            return None

        # Load clinical context
        notes_result = await db.execute(
            select(ClinicalNote).where(ClinicalNote.case_id == case.id).limit(1)
        )
        note = notes_result.scalar_one_or_none()
        clinical_context = note.structured if (note and note.structured) else None

        # Check auto_submit rules
        auto_result = await db.execute(
            select(AutomationRule.cpt_code, AutomationRule.icd_code)
            .where(AutomationRule.enabled == True)
        )
        auto_enabled = {(r[0], r[1]) for r in auto_result.all()}
        auto_submit = (case.cpt_code, case.icd1) in auto_enabled

        # Load rep answers for reruns (when changed_group_id is set in raw_data)
        rerun_rep_answers = None
        rerun_changed_group_id = None
        raw_data = case.raw_data or {}
        if raw_data.get("rerun_changed_group_id") is not None and job.attempt > 0:
            rerun_changed_group_id = raw_data["rerun_changed_group_id"]
            # Load all questions with rep_answer set
            q_result = await db.execute(
                select(Question)
                .where(Question.case_id == case.id)
                .where(Question.rep_answer.isnot(None))
                .order_by(Question.group_id)
            )
            rep_questions = q_result.scalars().all()
            if rep_questions:
                rerun_rep_answers = []
                for q in rep_questions:
                    rep_val = q.rep_answer
                    # rep_answer is {"Value": "uuid"} — normalize to Values list
                    values = []
                    if isinstance(rep_val, dict):
                        v = rep_val.get("Value") or rep_val.get("Values")
                        if isinstance(v, list):
                            values = v
                        elif v:
                            values = [v]
                    rerun_rep_answers.append({
                        "QuestionId": q.portal_question_id,
                        "GroupId": q.group_id,
                        "QuestionType": q.question_type,
                        "Sequence": q.sequence,
                        "Values": values,
                        "question_text": q.question_text,
                        "answer_text": "",  # text resolved at match time
                        "group_id": q.group_id,
                        "question_id": q.portal_question_id,
                        "answer_value": values,
                    })
                logger.info(
                    f"WorkerLoop/{worker_id}: loaded {len(rerun_rep_answers)} "
                    f"rep answers for rerun (changed_group_id={rerun_changed_group_id})"
                )

            # Clear the rerun flag so future first-passes don't pick it up
            raw_data_copy = dict(raw_data)
            raw_data_copy.pop("rerun_changed_group_id", None)
            case.raw_data = raw_data_copy

        await db.commit()

        event = {
            "case_id": case.id,
            "job_id": job.id,
            "job_type": job.job_type or "FIRST_PASS",
            "auto_submit": auto_submit,
            "clinical_context": clinical_context,
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
                "clinical_blob_key": case.clinical_blob_key,
                "file_key": case.file_key,
                "scheduled_dt": str(case.scheduled_dt) if case.scheduled_dt else None,
                "raw_data": case.raw_data or {},
            },
        }

        # Include rerun context if available
        if rerun_rep_answers is not None:
            event["rerun_rep_answers"] = rerun_rep_answers
            event["rerun_changed_group_id"] = rerun_changed_group_id

        return event


async def _get_worker_status_sync(worker_id: str) -> dict:
    """Get worker status from DB — called from shared handler."""
    from app.db.database import async_session_factory
    from app.db.models import WorkerAccount
    from sqlalchemy import select

    async with async_session_factory() as db:
        result = await db.execute(
            select(WorkerAccount).where(WorkerAccount.container_id == worker_id)
        )
        account = result.scalar_one_or_none()
        if not account:
            return {"worker_id": worker_id, "error": "not found"}

        return {
            "worker_id": worker_id,
            "is_active": account.is_active,
            "stop_requested": account.stop_requested,
            "is_sleeping": account.wake_awakeable_id is not None,
            "cases_today": account.cases_today,
            "total_cases": account.total_cases,
            "current_case_id": account.current_case_id,
        }


# ── Utility functions (called from sync_engine, not from Restate ctx) ──


async def wake_sleeping_workers() -> int:
    """Resolve awakeables for all sleeping workers — called after sync enqueues new cases.

    Returns the number of workers woken up.
    """
    from app.db.database import async_session_factory
    from app.db.models import WorkerAccount
    from app.core.settings import settings
    from sqlalchemy import select, update
    import httpx

    async with async_session_factory() as db:
        result = await db.execute(
            select(WorkerAccount.container_id, WorkerAccount.wake_awakeable_id)
            .where(
                WorkerAccount.is_active == True,
                WorkerAccount.wake_awakeable_id.isnot(None),
            )
        )
        sleeping = result.all()

        if not sleeping:
            return 0

        woken = 0
        async with httpx.AsyncClient(timeout=5.0) as client:
            for container_id, awakeable_id in sleeping:
                try:
                    resp = await client.post(
                        f"{settings.RESTATE_URL}/restate/awakeables/{awakeable_id}/resolve",
                        json={"wake": True},
                        headers={"Content-Type": "application/json"},
                    )
                    if resp.status_code in (200, 202):
                        woken += 1
                        logger.info(f"Woke worker {container_id} via awakeable")
                    else:
                        logger.warning(
                            f"Failed to wake {container_id}: Restate returned {resp.status_code}"
                        )
                except Exception as e:
                    logger.warning(f"Failed to wake {container_id}: {e}")

        # Clear all awakeable IDs (workers will re-register if still empty)
        if woken > 0:
            await db.execute(
                update(WorkerAccount)
                .where(WorkerAccount.wake_awakeable_id.isnot(None))
                .values(wake_awakeable_id=None)
            )
            await db.commit()

        logger.info(f"Woke {woken}/{len(sleeping)} sleeping workers")
        return woken


async def reap_stale_claims() -> int:
    """Reset CLAIMED jobs older than 10 min back to QUEUED.

    Handles crash between claim_next_job and CaseWorkflow dispatch.
    Called from sync_engine during each sync cycle.
    """
    from app.db.database import async_session_factory
    from app.db.models import SubmissionJob, JobStatus
    from sqlalchemy import update

    async with async_session_factory() as db:
        stale = await db.execute(
            update(SubmissionJob)
            .where(
                SubmissionJob.status == JobStatus.CLAIMED,
                SubmissionJob.claimed_at < datetime.utcnow() - timedelta(minutes=10),
            )
            .values(status=JobStatus.QUEUED, claimed_by=None, claimed_at=None)
            .returning(SubmissionJob.id)
        )
        reaped = stale.scalars().all()
        if reaped:
            logger.warning(f"Reaped {len(reaped)} stale CLAIMED jobs (>10 min)")
        await db.commit()
        return len(reaped)


async def reap_stale_processing() -> int:
    """Reset cases stuck in PROCESSING with QUEUED/RUNNING jobs.

    Two scenarios recovered:
    1. PROCESSING case + QUEUED job (>15 min) — workflow crashed after
       _set_case_processing, then reap_stale_claims reset job but not case.
    2. PROCESSING case + RUNNING job (>30 min) — workflow died mid-execution,
       no error handler ran. Reset both job and case.

    Called from sync_engine during each sync cycle.
    """
    from app.db.database import async_session_factory
    from app.db.models import Case, CaseState, SubmissionJob, JobStatus
    from sqlalchemy import select

    recovered = 0

    async with async_session_factory() as db:
        # Find PROCESSING cases with stale jobs
        result = await db.execute(
            select(Case, SubmissionJob)
            .join(SubmissionJob, SubmissionJob.case_id == Case.id)
            .where(
                Case.state == CaseState.PROCESSING,
                Case.updated_at < datetime.utcnow() - timedelta(minutes=15),
                SubmissionJob.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)),
            )
        )
        rows = result.all()

        for case, job in rows:
            # For RUNNING jobs, only reap if truly stale (>30 min)
            if job.status == JobStatus.RUNNING:
                stale_since = job.started_at or job.claimed_at or case.updated_at
                if stale_since and stale_since > datetime.utcnow() - timedelta(minutes=30):
                    continue
                job.status = JobStatus.QUEUED
                job.claimed_by = None
                job.claimed_at = None

            # Reset case state based on job type
            if job.job_type == "SUBMIT":
                case.state = CaseState.SUBMITTING
            elif job.job_type == "ORDER":
                case.state = CaseState.ORDER_READY
            elif job.job_type == "SIGNATURE_REPLAY":
                case.state = CaseState.PENDING_NOTES
            else:
                case.state = CaseState.NOTES_UPLOADED

            case.hold_reason = None
            recovered += 1
            logger.warning(
                f"Reaped stale PROCESSING case {case.id} "
                f"(job={job.job_type}/{job.status.value}) → {case.state.value}"
            )

        if recovered:
            await db.commit()

    return recovered
