"""Mongo Poll Scheduler — background asyncio task.

Reads polling config from system_settings, runs sync on interval.
Launched in FastAPI lifespan, stops when polling_enabled=false or app shuts down.
"""
from __future__ import annotations

import asyncio
import logging

from app.db.database import async_session_factory
from app.db.models import SystemSetting

logger = logging.getLogger(__name__)

_poll_task: asyncio.Task | None = None
_fax_poll_task: asyncio.Task | None = None

# RingCentral fax delivery poller runs on its own schedule, independent
# of the Mongo ingest poller. Every FAX_POLL_INTERVAL_SEC seconds we
# check all still-Queued fax messages younger than FAX_TIMEOUT_HOURS.
FAX_POLL_INTERVAL_SEC = 5 * 60     # 5 min
FAX_TIMEOUT_HOURS = 24


async def start_poll_scheduler() -> None:
    """Start the background polling loops. Called from FastAPI lifespan."""
    global _poll_task, _fax_poll_task
    if _poll_task and not _poll_task.done():
        logger.info("Poll scheduler already running")
    else:
        _poll_task = asyncio.create_task(_poll_loop())
        logger.info("Poll scheduler started")

    if _fax_poll_task and not _fax_poll_task.done():
        logger.info("Fax poll scheduler already running")
    else:
        _fax_poll_task = asyncio.create_task(_fax_poll_loop())
        logger.info("Fax poll scheduler started")


async def stop_poll_scheduler() -> None:
    """Stop the background polling loops. Called from FastAPI shutdown."""
    global _poll_task, _fax_poll_task
    for name, task_ref in (("poll", _poll_task), ("fax poll", _fax_poll_task)):
        if task_ref and not task_ref.done():
            task_ref.cancel()
            try:
                await task_ref
            except asyncio.CancelledError:
                pass
    _poll_task = None
    _fax_poll_task = None
    logger.info("Poll scheduler stopped")


async def _poll_loop() -> None:
    """Main loop — checks settings each iteration, syncs if enabled."""
    while True:
        try:
            async with async_session_factory() as db:
                enabled = await _get(db, "polling_enabled", False)
                interval = await _get(db, "polling_interval_minutes", 15)

            if not enabled:
                # Check again in 30s
                await asyncio.sleep(30)
                continue

            # Run sync using the shared engine
            logger.info("Poll scheduler: running sync...")
            try:
                async with async_session_factory() as db:
                    extract = await _get(db, "polling_extract", True)
                    limit = await _get(db, "polling_limit", 500)

                async with async_session_factory() as db:
                    from app.ingest.sync_engine import run_sync
                    from app.api.routes.settings import record_sync_result

                    result = await run_sync(db, extract=extract, limit=limit)
                    await record_sync_result(db, result)
                    await db.commit()

                logger.info(
                    f"Poll sync: {result.get('new_cases', 0)} new, "
                    f"{result.get('duplicates_skipped', 0)} dupes, "
                    f"{result.get('enqueued', 0)} enqueued"
                )
            except Exception as e:
                logger.error(f"Poll scheduler: sync failed: {e}")

            # Sleep for interval
            await asyncio.sleep(max(interval, 1) * 60)

        except asyncio.CancelledError:
            logger.info("Poll scheduler: cancelled")
            raise
        except Exception as e:
            logger.error(f"Poll scheduler: unexpected error: {e}")
            await asyncio.sleep(60)  # Back off on error


async def _get(db, key: str, default=None):
    """Read a setting value."""
    setting = await db.get(SystemSetting, key)
    return setting.value if setting else default


# ─────────────────────────────────────────────────────────────────────
# Fax delivery status poller
# ─────────────────────────────────────────────────────────────────────


async def _fax_poll_loop() -> None:
    """Poll RingCentral for still-Queued faxes, update case state, generate
    confirmation PDFs on success, surface failures on the Worklist.

    Runs every FAX_POLL_INTERVAL_SEC. Safe to interrupt. Per-case exceptions
    are logged but don't abort the cycle.
    """
    while True:
        try:
            await _fax_poll_cycle()
        except asyncio.CancelledError:
            logger.info("Fax poll scheduler: cancelled")
            raise
        except Exception as e:
            logger.error(f"Fax poll scheduler: unexpected error: {e}")
        await asyncio.sleep(FAX_POLL_INTERVAL_SEC)


async def _fax_poll_cycle() -> None:
    """One iteration: find pending faxes, resolve status for each."""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select, or_
    from app.db.models import Case, SubmissionJob, ExceptionType, JobStatus

    async with async_session_factory() as db:
        timeout_cutoff = datetime.utcnow() - timedelta(hours=FAX_TIMEOUT_HOURS)
        # Candidates: have an RC message_id, status still Queued (or NULL),
        # updated within FAX_TIMEOUT_HOURS so we don't re-poll ancient rows.
        r = await db.execute(
            select(Case).where(
                Case.fax_message_id.is_not(None),
                or_(Case.fax_status.is_(None), Case.fax_status == "Queued"),
                Case.updated_at >= timeout_cutoff,
            )
        )
        cases = r.scalars().all()

    if not cases:
        return

    logger.info(f"Fax poll: {len(cases)} case(s) with pending fax status")

    from app.services.ringcentral_fax import get_fax_status

    for case_ref in cases:
        try:
            await _resolve_one_fax(case_ref.id, case_ref.fax_message_id)
        except Exception as e:
            logger.warning(f"Fax poll: case {case_ref.id} resolve failed: {e}")


async def _resolve_one_fax(case_id: str, message_id: str) -> None:
    """Check RC for this message, persist the outcome to the Case row,
    generate a PDF on success, and add an audit event.
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select
    from app.db.models import Case, SubmissionJob, AuditEvent, JobStatus, ExceptionType
    from app.db import repositories as repo
    from app.services.ringcentral_fax import get_fax_status
    from app.services.fax_confirmation_pdf import (
        build_fax_confirmation_pdf,
        upload_fax_confirmation_pdf,
    )

    status_data = await get_fax_status(message_id)
    if not status_data.get("ok"):
        logger.warning(f"Fax poll: {case_id} RC status fetch failed: {status_data.get('error')}")
        return

    status = status_data.get("status") or "Unknown"

    # Nothing to do — still Queued, try again next cycle
    if status == "Queued":
        # But check for timeout
        async with async_session_factory() as db:
            case = await db.get(Case, case_id)
            if not case:
                return
            # If the case was updated >24h ago and is still Queued, timeout it
            if case.updated_at and case.updated_at < datetime.utcnow() - timedelta(hours=24):
                logger.warning(f"Fax poll: {case_id} still Queued after 24h — marking Timeout")
                await _mark_fax_failed(db, case, status_data, "Timeout",
                                       "Fax delivery confirmation timed out after 24 hours.")
                await db.commit()
        return

    async with async_session_factory() as db:
        case = await db.get(Case, case_id)
        if not case:
            return

        if status == "Sent":
            await _mark_fax_delivered(db, case, status_data)
            # Generate + upload PDF
            try:
                submitted_at, approved_by, approved_at = await _load_fax_audit_context(db, case_id)
                pdf_bytes = build_fax_confirmation_pdf(
                    case=case,
                    fax_status_data=status_data,
                    submitted_at=submitted_at,
                    approved_by=approved_by,
                    approved_at=approved_at,
                )
                blob_key = upload_fax_confirmation_pdf(
                    pdf_bytes=pdf_bytes,
                    exam_id=str(case.exam_id or case_id),
                    message_id=message_id,
                )
                case.fax_confirmation_pdf_key = blob_key
            except Exception as e:
                logger.warning(f"Fax poll: {case_id} PDF generation failed (non-fatal): {e}")

            await repo.create_audit_event(
                db, case_id=case_id, actor="system",
                action="fax_delivered",
                data={
                    "message_id": message_id,
                    "status": status,
                    "last_modified": status_data.get("last_modified_at").isoformat()
                        if status_data.get("last_modified_at") else None,
                    "pages": status_data.get("fax_page_count"),
                    "confirmation_pdf_key": case.fax_confirmation_pdf_key,
                },
            )
            await db.commit()
            logger.info(f"Fax poll: {case_id} delivered (Sent)")

        elif status in ("SendingFailed", "Failed", "DeliveryFailed"):
            error_hint = _first_to_error(status_data) or status
            await _mark_fax_failed(db, case, status_data, status, error_hint)
            await db.commit()
            logger.warning(f"Fax poll: {case_id} failed ({status}): {error_hint}")
        else:
            # Received / Unknown — log and leave as-is (shouldn't happen for outbound)
            logger.info(f"Fax poll: {case_id} unexpected status={status}")


async def _mark_fax_delivered(db, case, status_data: dict) -> None:
    case.fax_status = "Sent"
    case.fax_delivered_at = status_data.get("last_modified_at")


async def _mark_fax_failed(db, case, status_data: dict,
                           status_value: str, reason: str) -> None:
    from sqlalchemy import select
    from app.db.models import SubmissionJob, ExceptionType, JobStatus
    from app.db import repositories as repo

    case.fax_status = status_value

    # Surface on Worklist
    jr = await db.execute(select(SubmissionJob).where(SubmissionJob.case_id == case.id))
    job = jr.scalar_one_or_none()
    if job:
        job.exception_type = ExceptionType.FAX_FAILED
        job.exception_detail = f"Fax delivery {status_value}: {reason[:200]}"

    await repo.create_audit_event(
        db, case_id=case.id, actor="system",
        action="fax_failed",
        data={
            "message_id": status_data.get("message_id"),
            "status": status_value,
            "reason": reason,
            "to": status_data.get("to"),
        },
    )


async def _load_fax_audit_context(db, case_id: str):
    """Pull timestamps + approver from earlier audit events for the PDF."""
    from sqlalchemy import select
    from app.db.models import AuditEvent

    r = await db.execute(
        select(AuditEvent)
        .where(AuditEvent.case_id == case_id)
        .where(AuditEvent.action.in_(("fax_sent", "fax_approved")))
        .order_by(AuditEvent.timestamp)
    )
    rows = r.scalars().all()
    submitted_at = None
    approved_by = None
    approved_at = None
    for a in rows:
        if a.action == "fax_sent":
            submitted_at = a.timestamp
        elif a.action == "fax_approved":
            approved_at = a.timestamp
            approved_by = (a.actor or "").split(":", 1)[-1] or None
    return submitted_at, approved_by, approved_at


def _first_to_error(status_data: dict) -> str | None:
    to = status_data.get("to") or []
    if not to:
        return None
    return to[0].get("error")
