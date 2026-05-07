"""Shared helpers for worker operations.

Extracted from worker_session.py so both the Restate thin handlers
(on orchestrator) and the Worker HTTP server (on worker VMs) can use them.
None of these functions depend on Restate ctx.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.worker.browser_manager import (
    _ensure_session_direct,
    _close_browser,
    _BROWSER_REGISTRY,
)

logger = logging.getLogger(__name__)

# ── Browser Management ──


async def ensure_browser_login(worker_id: str, credentials: dict | None = None) -> None:
    """Ensure a browser session is logged into Carelon for this worker.

    Single code path: delegates to _ensure_session_direct which handles
    create/reuse/re-login. No duplicate browser management.
    """
    await _ensure_session_direct(worker_id, credentials)
    logger.info(f"Worker/{worker_id}: browser ready")


async def close_worker_browser(worker_id: str) -> None:
    """Close the browser for this worker."""
    try:
        await _close_browser(worker_id)
    except Exception as e:
        logger.warning(f"Worker/{worker_id}: close browser error: {e}")
    logger.info(f"Worker/{worker_id}: browser closed")


async def navigate_to_homepage(worker_id: str) -> bool:
    """Navigate back to member search page for the next case.

    Uses the portal's Home icon button which triggers an ASP.NET postback
    (__doPostBack('TopMenu','')) — stays within the same session.
    This is how a real user navigates between cases.

    Selector: #asNavigation_ctl00_hlHome (house icon in top nav)
    Fallback: execute __doPostBack('TopMenu','') directly
    """
    session = _BROWSER_REGISTRY.get(worker_id)
    if not session:
        return False

    try:
        import asyncio

        # Try clicking the Home icon (house icon in top nav)
        clicked = await session.page.evaluate("""
            () => {
                // Primary: the home icon link
                const homeLink = document.getElementById('asNavigation_ctl00_hlHome');
                if (homeLink) {
                    homeLink.click();
                    return 'clicked #asNavigation_ctl00_hlHome';
                }
                // Fallback: any link with title="Home"
                const homeByTitle = document.querySelector('a[title="Home"]');
                if (homeByTitle) {
                    homeByTitle.click();
                    return 'clicked a[title=Home]';
                }
                // Last resort: trigger the postback directly
                if (typeof __doPostBack === 'function') {
                    __doPostBack('TopMenu', '');
                    return 'called __doPostBack(TopMenu)';
                }
                return 'not found';
            }
        """)
        logger.info(f"Worker/{worker_id}: home nav: {clicked}")

        if clicked == "not found":
            logger.warning(f"Worker/{worker_id}: home icon not found in DOM")
            return False

        # Wait for the postback to complete — like a rep waiting for the page
        import random
        settle_time = random.gauss(3.0, 0.5)
        settle_time = max(2.0, min(4.0, settle_time))
        await asyncio.sleep(settle_time)
        try:
            await session.page.wait_for_selector(
                "#asPrimary_ctl00_BtnSearch",
                state="visible",
                timeout=15000,
            )
            logger.info(f"Worker/{worker_id}: on member search page, ready")
            return True
        except Exception:
            # Check if session expired during postback
            try:
                body = await session.page.evaluate(
                    "() => (document.body?.innerText || '').substring(0, 200)"
                )
                if "User Confirmation" in body or "User Login" in body:
                    logger.warning(f"Worker/{worker_id}: session expired during home nav")
                    return False
            except Exception:
                pass
            logger.warning(f"Worker/{worker_id}: search button not found after home click")
            return False

    except Exception as e:
        logger.warning(f"Worker/{worker_id}: navigate error: {e}")
        return False


def is_portal_error(error_msg: str) -> bool:
    """Check if an error indicates a portal/browser issue (vs data issue).

    When True, the caller (process_case / finalize_case) closes the worker's
    browser session so the next claim starts fresh. Critical for Carelon-side
    flakes where the browser ends up in a wedged state (stale cookies,
    redirected to Default.aspx, expired auth tokens).
    """
    portal_indicators = [
        "TargetClosedError",
        "Target page, context or browser has been closed",
        "Session expired",
        "Page.wait_for_selector: Timeout",
        "net::ERR_",
        "Navigation failed",
        "Execution context was destroyed",
        # v148: Carelon-side post-eligibility transition flake. The error
        # message includes the Default.aspx URL fragment, so we match on a
        # stable substring of the full message rather than the URL itself.
        "Provider search page did not load",
        "Could not select provider",
        "Facility continue failed",
    ]
    return any(ind.lower() in error_msg.lower() for ind in portal_indicators)


def is_transient_error(reason: str) -> bool:
    """Check if a hold reason is a transient (retryable) infrastructure error."""
    transient_patterns = [
        "browser", "target page", "closed", "timeout", "navigation",
        "network", "xserver", "chromium.launch", "protocol error",
        "connection refused", "session closed", "frame was detached",
        # Portal page-load failures (retryable)
        "provider search page did not load",
        "fax modal may be blocking",
    ]
    reason_lower = reason.lower()
    return any(p in reason_lower for p in transient_patterns)


# ── DB Helpers ──


async def mark_case_hold(case_id: str, hold_reason: str) -> None:
    """Mark a case as HOLD with reason + update job exception.

    Transient errors (browser crash, timeout, etc.) are auto-requeued up to
    max_attempts (3 total = 1 original + 2 retries). Permanent errors
    (member not found, missing data, etc.) always go to HOLD.
    """
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState, ExceptionType
    from sqlalchemy import select, update
    from app.db.models import SubmissionJob, JobStatus

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)

        # Classify exception
        reason_lower = hold_reason.lower()
        if "member not found" in reason_lower:
            exc_type = ExceptionType.MEMBER_NOT_FOUND
        elif "phone" in reason_lower:
            exc_type = ExceptionType.PHONE_MISSING
        elif "duplicate" in reason_lower:
            exc_type = ExceptionType.DUPLICATE_AUTH
        elif "eligible" in reason_lower or "terminated" in reason_lower:
            exc_type = ExceptionType.ELIGIBILITY_EXPIRED
        elif "referring" in reason_lower and "npi" in reason_lower:
            exc_type = ExceptionType.REFERRING_NPI_MISSING
        elif "no auth required" in reason_lower or "does not require" in reason_lower:
            exc_type = ExceptionType.NO_AUTH_REQUIRED
        else:
            exc_type = ExceptionType.PORTAL_ERROR

        # Check if transient + has retries left
        job_result = await db.execute(
            select(SubmissionJob).where(SubmissionJob.case_id == case_id)
        )
        job = job_result.scalar_one_or_none()

        is_transient = is_transient_error(hold_reason)
        current_attempt = job.attempt if job else 1
        max_attempts = job.max_attempts if job else 2
        can_retry = is_transient and current_attempt < max_attempts

        if can_retry and case and job:
            # Auto-requeue: reset case to the phase's "ready" state so the
            # SAME worker type re-claims it. The state must match
            # claim_next_job's filter for this job_type (ready_state_for is
            # the canonical mapping; see db/queue.py).
            #
            # Historical bug (v138 → v142): this was hardcoded NOTES_UPLOADED
            # for every job_type. SUBMIT / ORDER / SIGNATURE_REPLAY retries
            # silently became unclaimable — case state didn't match its claim
            # filter, job sat QUEUED forever (Adedeji 73723, Rivers 73218).
            #
            # v153: BEFORE resetting job.status=QUEUED, kill any prior Restate
            # workflow invocation keyed by this case_id. Restate workflows are
            # idempotent on key — a `workflow_send` with a key that already has
            # an active (running/suspended/completed-within-retention)
            # invocation silently dedupes to that prior one. So re-claiming
            # without purging leaves the SubmitWorkflow handler never entering
            # again, the worker doing nothing, the Reaper resetting again, and
            # attempts running through 3/3 with ZERO error captured. We saw
            # this on Lopez/Talley/Thidavanh on 2026-05-07 — restate-handler
            # logs showed "HANDLER ENTERED" once, then attempts 2/3 silent.
            # purge_case_workflow is non-fatal: errors are logged but never
            # raised (per its v141 contract), so reset proceeds either way.
            from app.workflow.restate_utils import purge_case_workflow
            await purge_case_workflow(case_id)

            from app.db.queue import ready_state_for
            retry_state = ready_state_for(job.job_type)
            case.state = retry_state
            case.hold_reason = None
            job.status = JobStatus.QUEUED
            job.claimed_by = None
            job.attempt = current_attempt
            job.exception_type = None
            job.exception_detail = None

            # v148: cooldown for known portal-flake patterns where Carelon
            # needs time to recover from a transient state. Without this,
            # three back-to-back retries all hit the same flake window and
            # the case exhausts to permanent HOLD even though the portal
            # would have recovered after 1-2 minutes.
            from datetime import datetime, timedelta
            _PORTAL_FLAKE_PATTERNS = (
                "provider search page",        # post-eligibility transition
                "could not select provider",   # provider grid wait_for_selector
                "facility continue failed",    # post-summary transition
            )
            is_portal_flake = any(p in reason_lower for p in _PORTAL_FLAKE_PATTERNS)
            cooldown_until: "datetime | None" = None
            if is_portal_flake:
                cooldown_until = datetime.utcnow() + timedelta(minutes=5)
                job.cooldown_until = cooldown_until
            else:
                # Reset any prior cooldown — non-flake transients retry
                # immediately as before (browser blip, network reset, etc).
                job.cooldown_until = None

            await repo.create_audit_event(
                db, case_id=case_id, actor="system",
                action="auto_requeue",
                data={
                    "attempt": current_attempt,
                    "max_attempts": max_attempts,
                    "transient_reason": hold_reason,
                    "exception_type": exc_type.value,
                    "retry_state": retry_state.value,
                    "job_type": job.job_type,
                    "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
                    "portal_flake": is_portal_flake,
                },
            )
            await db.commit()
            cooldown_log = f", cooldown 5min until {cooldown_until.strftime('%H:%M:%S')}" if cooldown_until else ""
            logger.info(
                f"Worker: case {case_id} auto-requeued "
                f"(attempt {current_attempt}/{max_attempts}, "
                f"transient: {hold_reason[:80]}{cooldown_log})"
            )
            return

        # Permanent HOLD or retries exhausted
        if case:
            case.state = CaseState.HOLD
            case.hold_reason = hold_reason

        if job:
            job.status = JobStatus.FAILED
            job.exception_type = exc_type
            job.exception_detail = hold_reason
            job.attempt = current_attempt

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action="state_change:HOLD",
            data={
                "state": "HOLD",
                "hold_reason": hold_reason,
                "exception_type": exc_type.value,
                "attempt": current_attempt,
                "retries_exhausted": is_transient and current_attempt >= max_attempts,
            },
        )

        await db.commit()
        if is_transient:
            logger.warning(
                f"Worker: case {case_id} → HOLD (retries exhausted: "
                f"attempt {current_attempt}/{max_attempts}, {hold_reason[:80]})"
            )
        else:
            logger.info(f"Worker: case {case_id} → HOLD ({hold_reason[:80]})")


async def mark_case_submission_error(
    case_id: str,
    *,
    error_type: str,
    title: str,
    body: str,
    prior_order_id: str | None = None,
    screenshot_key: str | None = None,
    matched_rule: str | None = None,
) -> None:
    """Mark a case as SUBMISSION_ERROR — portal refused at the submit step.

    Distinct from HOLD: these are cases where Carelon's server-side rules
    actively rejected our submission (duplicate-order modal, criteria-not-met
    page, session expired, portal error page). Reps get a dedicated Worklist
    tab + the captured screenshot so they can decide without logging into
    Carelon.

    Not auto-retried — retrying deterministically hits the same portal rule.
    """
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState, ExceptionType
    from sqlalchemy import select
    from app.db.models import SubmissionJob, JobStatus

    # Map our internal error_type tag to the existing ExceptionType enum.
    exc_type_map = {
        "duplicate":        ExceptionType.DUPLICATE_AUTH,
        "criteria_not_met": ExceptionType.MED_NECESSITY,
        "portal_error":     ExceptionType.PORTAL_ERROR,
    }
    exc_type = exc_type_map.get(error_type, ExceptionType.PORTAL_ERROR)

    body_short = (body or "").strip()[:300]
    human_reason = f"[{error_type}] {title or error_type}: {body_short}".strip()

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)
        if case:
            case.state = CaseState.SUBMISSION_ERROR
            case.hold_reason = human_reason
            if screenshot_key:
                # Reuse the same column the frontend already renders for
                # APPROVED/PENDED confirmation screenshots.
                case.auth_pdf_url = screenshot_key

        job_result = await db.execute(
            select(SubmissionJob).where(SubmissionJob.case_id == case_id)
        )
        job = job_result.scalar_one_or_none()
        if job:
            job.status = JobStatus.FAILED
            job.exception_type = exc_type
            job.exception_detail = (
                f"{title or error_type}. "
                f"Prior order: {prior_order_id or '—'}. "
                f"{body_short}"
            )[:500]

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action="submission_error",
            data={
                "error_type": error_type,
                "title": title,
                "body": body_short,
                "prior_order_id": prior_order_id,
                "screenshot_key": screenshot_key,
                "matched_rule": matched_rule,
                "exception_type": exc_type.value,
            },
        )

        await db.commit()
        logger.warning(
            f"Worker: case {case_id} → SUBMISSION_ERROR "
            f"(type={error_type}, prior={prior_order_id or '—'})"
        )


async def mark_case_no_auth_review(case_id: str, reason: str, screenshot_key: str | None = None) -> None:
    """Route NO_AUTH_REQUIRED case to rep review queue.

    Portal says no auth needed, but a rep should verify before we finalize.
    Case goes to IN_REVIEW with hold_reason containing the portal message.
    Job stays QUEUED so it appears in the review queue.
    """
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)
        if case:
            case.state = CaseState.L1_REVIEW
            case.hold_reason = reason
            case.approval_type = "no_auth"
            if screenshot_key:
                case.auth_pdf_url = screenshot_key

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action="state_change:NO_AUTH_REVIEW",
            data={"reason": reason},
        )

        # Log billing event: NO_AUTH
        if case:
            await repo.create_execution_log(
                db, event_type="NO_AUTH", case=case,
                detail={"reason": reason[:200]},
            )

        await db.commit()
        logger.info(f"Worker: case {case_id} → IN_REVIEW (no_auth: {reason[:80]})")


async def mark_case_no_auth_required(case_id: str, reason: str) -> None:
    """Mark a case as NO_AUTH_REQUIRED — DI pre-auth not needed for this member's plan.

    Called by rep via queue endpoint after reviewing the no-auth determination.
    This is a terminal outcome (not transient), so no retries.
    """
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState, ExceptionType
    from sqlalchemy import update
    from app.db.models import SubmissionJob, JobStatus

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)
        if case:
            case.state = CaseState.NO_AUTH_REQUIRED
            case.hold_reason = reason

        await db.execute(
            update(SubmissionJob).where(
                SubmissionJob.case_id == case_id
            ).values(
                status=JobStatus.COMPLETED,
                exception_type=ExceptionType.NO_AUTH_REQUIRED,
                exception_detail=reason,
            )
        )

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action="state_change:NO_AUTH_REQUIRED",
            data={"reason": reason},
        )
        await db.commit()
        logger.info(f"Worker: case {case_id} → NO_AUTH_REQUIRED ({reason[:80]})")


async def mark_case_complete(case_id: str, result: dict) -> None:
    """Mark a case as completed after submission."""
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState, ExceptionType
    from sqlalchemy import update
    from app.db.models import SubmissionJob, JobStatus

    # Detect "empty submission result" — the silent-failure path where the
    # portal returned no order_id, no pend reason, and no denial reason.
    # Historically we silently marked these PENDED (creating ghost cases like
    # Jakob Buhrkuhl 17313436 — state=PENDED in Ronexa but no case in Carelon).
    # Now we HOLD them so a rep can verify and re-submit if needed.
    submission_empty = (
        not result.get("auth_number")
        and not result.get("denial_reason")
        and not result.get("pend_reason")
    )

    async with async_session_factory() as db:
        case = await repo.get_case(db, case_id)
        if case:
            auth_number = result.get("auth_number")

            # Determine outcome from portal determination_status FIRST,
            # then fall back to auth_number presence.
            # The portal returns an order_id for ALL outcomes (approved,
            # pended, in-progress, denied) — so auth_number alone is NOT
            # sufficient to determine APPROVED.
            if result.get("denial_reason"):
                case.state = CaseState.DENIED
            elif result.get("pend_reason"):
                case.state = CaseState.PENDED
            elif auth_number:
                case.state = CaseState.APPROVED
            else:
                # No auth, no explicit pend, no denial — submit_and_extract
                # completed without error but didn't capture a confirmation
                # (portal may have intercepted at duplicate-auth check, shown
                # an interstitial, or redirected to an unrecognized page).
                # Don't silently mark PENDED; HOLD for rep verification.
                case.state = CaseState.HOLD
                case.hold_reason = (
                    "Submission completed but no confirmation captured "
                    "(no auth number, no pend/denial reason). "
                    "Verify in Carelon portal before re-submitting."
                )

            # Persist all portal extraction fields
            if result.get("portal_case_id"):
                case.portal_case_id = result["portal_case_id"]
            if auth_number:
                case.auth_number = auth_number
            if result.get("determination_status"):
                case.determination_status = result["determination_status"]
            if result.get("valid_from"):
                case.valid_from = result["valid_from"]
            if result.get("valid_through"):
                case.valid_through = result["valid_through"]
            if result.get("denial_reason"):
                case.denial_reason = result["denial_reason"]
            if result.get("pend_reason"):
                case.pend_reason = result["pend_reason"]
            if result.get("determination_date"):
                case.determination_date = result["determination_date"]
            # Prefer actual PDF, fall back to confirmation screenshot (PNG)
            case.auth_pdf_url = result.get("auth_pdf_blob_key") or result.get("confirmation_screenshot_key")

            # Approval tracking — gold card level + algorithm recommendation + approval type
            case.gold_card_level = result.get("gold_card_level")
            case.auto_approved = result.get("auto_approved", False)
            case.algorithm_recommendation = result.get("algorithm_recommendation")
            if (result.get("gold_card_level") or 0) >= 2:
                case.approval_type = "gold_card"
            elif result.get("auto_approved"):
                case.approval_type = "algorithm"
            else:
                case.approval_type = "manual"

            # Record submission timestamp
            from datetime import datetime as dt
            case.submitted_at = dt.utcnow()

        # If submission result was empty, surface the job on the Worklist as
        # a PORTAL_ERROR exception rather than silently marking it COMPLETED.
        if submission_empty:
            await db.execute(
                update(SubmissionJob).where(
                    SubmissionJob.case_id == case_id
                ).values(
                    status=JobStatus.FAILED,
                    exception_type=ExceptionType.PORTAL_ERROR,
                    exception_detail=(
                        "Submission completed but no confirmation captured "
                        "(no auth number, no pend/denial reason). "
                        "Verify in Carelon portal before re-submitting."
                    ),
                )
            )
        else:
            await db.execute(
                update(SubmissionJob).where(
                    SubmissionJob.case_id == case_id
                ).values(status=JobStatus.COMPLETED)
            )

        await repo.create_audit_event(
            db, case_id=case_id, actor="system",
            action=f"state_change:{case.state.value if case else 'UNKNOWN'}",
            data={"result": {k: v for k, v in result.items() if isinstance(v, (str, int, float, bool, type(None)))}},
        )

        # Log billing events: SUBMITTED + outcome (APPROVED/PENDED/DENIED)
        if case:
            await repo.create_execution_log(
                db, event_type="SUBMITTED", case=case,
                detail={"auth_number": result.get("auth_number")},
            )
            outcome_map = {
                CaseState.APPROVED: "APPROVED",
                CaseState.DENIED: "DENIED",
                CaseState.PENDED: "PENDED",
            }
            if case.state in outcome_map:
                await repo.create_execution_log(
                    db, event_type=outcome_map[case.state], case=case,
                    detail={
                        "auth_number": result.get("auth_number"),
                        "determination_status": result.get("determination_status"),
                    },
                )

        await db.commit()

        # Index outcome patterns for RAG — non-fatal, runs after commit
        try:
            from app.db.outcome_db import index_outcome_patterns
            async with async_session_factory() as outcome_db:
                await index_outcome_patterns(case_id, result, outcome_db)
                await outcome_db.commit()
            logger.info(f"Worker: indexed outcome patterns for case {case_id}")
        except Exception as e:
            logger.warning(f"Worker: outcome indexing failed (non-fatal) for {case_id}: {e}")

        # Index algorithm signature for replay — non-fatal, runs after commit
        try:
            from app.db.outcome_db import index_algorithm_signature
            async with async_session_factory() as sig_db:
                await index_algorithm_signature(case_id, result, sig_db)
                await sig_db.commit()
            logger.info(f"Worker: indexed algorithm signature for case {case_id}")
        except Exception as e:
            logger.warning(f"Worker: signature indexing failed (non-fatal) for {case_id}: {e}")


async def save_flow_checks(case_id: str, result: dict) -> None:
    """Save flow check audit events from compiler result.

    The compiler accumulates results across phases. Key mappings:
      - eligibility: result["eligibility"] = {effective_date, term_date}
      - eligibility_check: result["eligibility_check"] = {eligible, reason}
      - existing_auths: result["existing_auths"] = [list of auths]
      - provider_match: result["provider_match"] = {name, address, match_method, ...}
      - contrast: result["contrast"] = "label string"
    """
    from app.db.database import async_session_factory
    from app.db import repositories as repo

    checks = []

    # Eligibility — nested under result["eligibility"]
    elig = result.get("eligibility")
    if elig and isinstance(elig, dict):
        elig_check = result.get("eligibility_check", {})
        checks.append(("flow_check:eligibility", {
            "eligible": elig_check.get("eligible", True),
            "effective": elig.get("effective_date"),
            "term": elig.get("term_date"),
            "reason": elig_check.get("reason"),
        }))

    # Duplicate auth check — from existing_auths extraction
    existing_auths = result.get("existing_auths")
    if existing_auths is not None:
        # If we have existing auths, record them as a flow check
        duplicate_check = result.get("duplicate_check")
        if duplicate_check:
            checks.append(("flow_check:duplicate_auth", duplicate_check))
        else:
            checks.append(("flow_check:duplicate_auth", {
                "duplicate": False,
                "existing_count": len(existing_auths) if isinstance(existing_auths, list) else 0,
            }))

    # Recent auth warning (8-30 day window) — informational, not blocking
    if result.get("recent_auth_warning"):
        checks.append(("flow_check:recent_auth_warning", {
            "warning": result["recent_auth_warning"],
        }))

    # Provider match — already at top level from result.update(r["data"])
    if result.get("provider_match"):
        pm = result["provider_match"]
        checks.append(("flow_check:provider_match", {
            "matched": True,
            "method": pm.get("match_method"),
            "provider_name": pm.get("name"),
            "provider_address": pm.get("address"),
            "results_count": pm.get("results_count"),
            "fax_entered": pm.get("fax_entered"),
        }))

    # Facility match — from facility_search phase
    if result.get("facility_match"):
        fm = result["facility_match"]
        checks.append(("flow_check:facility_match", {
            "matched": True,
            "method": fm.get("match_method"),
            "facility_name": fm.get("name"),
            "facility_address": fm.get("address"),
            "results_count": fm.get("results_count"),
            "selected_index": fm.get("selected_index"),
            "center_address": fm.get("center_address"),
        }))

    # Contrast selection
    if result.get("contrast"):
        contrast = result["contrast"]
        checks.append(("flow_check:contrast_selection", {
            "contrast": contrast if isinstance(contrast, str) else str(contrast),
        }))

    # Clinical context (if available)
    if result.get("clinical_context_loaded"):
        checks.append(("flow_check:clinical_context", {
            "has_notes": True,
        }))

    if checks:
        async with async_session_factory() as db:
            for action, data in checks:
                await repo.create_audit_event(
                    db, case_id=case_id, actor="system", action=action, data=data,
                )
            await db.commit()
        logger.info(f"Saved {len(checks)} flow checks for case {case_id}")
