"""Worker HTTP Server — plain FastAPI app for portal automation.

Runs on each worker VM (port 9081). NOT registered with Restate.
Receives HTTP calls from the thin WorkerSession Restate handlers on the orchestrator.

Endpoints:
  POST /process-case   — first pass: login → portal → questions → return (Job 1)
  POST /finalize-case  — finalize with approved answers → submit → return (Job 2)
  POST /close-browser  — close browser + Playwright cleanup
  GET  /status         — health check + browser state
"""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI

from app.compiler.portal_compiler import load_compiler
from app.portal.session import PlaywrightPortalSession
from app.worker.browser_manager import PortalSession, _BROWSER_REGISTRY
from app.worker.helpers import (
    ensure_browser_login,
    close_worker_browser,
    navigate_to_homepage,
    is_portal_error,
    mark_case_hold,
    mark_case_no_auth_review,
    mark_case_complete,
    save_flow_checks,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Ronexa Worker", version="1.0.0")


def _to_portal_session(raw: PortalSession) -> PlaywrightPortalSession:
    """Bridge PortalSession (browser lifecycle) → PlaywrightPortalSession (clinical API).

    The compiler and clinical flow expect PlaywrightPortalSession which has
    the .api() method for in-page fetch() calls. The worker's browser_manager
    creates PortalSession dataclasses. This wraps one into the other.
    """
    ps = PlaywrightPortalSession(
        context=raw.context,
        page=raw.page,
        center_npi=raw.provider_id or "",
        provider_id=raw.provider_id,
        client_id=raw.client_id,
    )
    # Use the existing BehaviorEngine from the session (already configured)
    if raw.behavior:
        ps.behavior = raw.behavior
    return ps


@app.post("/process-case")
async def process_case(event: dict) -> dict:
    """Run portal first pass for a case. Returns result directly.

    This is the business logic that was previously inside
    WorkerSession.run_first_pass Restate handler.
    """
    worker_id = event.get("worker_id", "unknown")
    case_id = event["case_id"]
    case_data = event["case_data"]
    clinical_context = event.get("clinical_context")
    credentials = event.get("credentials")
    order_mode = event.get("order_mode", False)

    # Rerun context (loaded from DB when rep changed answers)
    rerun_rep_answers = event.get("rerun_rep_answers")
    rerun_changed_group_id = event.get("rerun_changed_group_id")

    if rerun_rep_answers:
        logger.info(
            f"Worker/{worker_id}: process-case RERUN for {case_id} "
            f"({len(rerun_rep_answers)} rep answers, changed_group_id={rerun_changed_group_id})"
        )
    else:
        logger.info(f"Worker/{worker_id}: process-case for {case_id}")

    try:
        # Step 1: Ensure browser is logged in
        await ensure_browser_login(worker_id, credentials)

        # Step 2: Run compiler — all portal phases
        raw_session = _BROWSER_REGISTRY[worker_id]
        session = _to_portal_session(raw_session)
        compiler = load_compiler("carelon_provider_portal")
        result = await compiler.execute(
            case=case_data,
            session=session,
            clinical_context=clinical_context,
            dry_run=True,   # First pass: never click "Submit This Request".
                            # Auto-approved (0 questions) cases would otherwise
                            # fall through the review gate and submit at the
                            # portal before rep approval. SubmitWorkflow runs
                            # finalize-case with dry_run=False to actually submit.
            resume_answers=rerun_rep_answers,
            changed_group_id=rerun_changed_group_id,
            order_mode=order_mode,
        )

        # Step 3: Save flow checks for ALL outcomes (eligibility, provider, etc.)
        try:
            await save_flow_checks(case_id, result)
        except Exception as fc_err:
            logger.warning(f"Worker/{worker_id}: flow check save failed (non-fatal): {fc_err}")

        # Step 3.5: Save pathway metadata if present (from GetPathwayOptions + SetPathway)
        if result.get("pathway"):
            try:
                from app.compiler.portal_compiler import _save_pathway_to_case
                await _save_pathway_to_case(case_id, result["pathway"])
                logger.info(f"Worker/{worker_id}: saved pathway '{result['pathway'].get('name')}' for {case_id}")
            except Exception as pw_err:
                logger.warning(f"Worker/{worker_id}: pathway save failed (non-fatal): {pw_err}")

        # Step 4: Handle result
        if result.get("case_state") == "NO_AUTH_REQUIRED":
            reason = result.get("hold_reason", "No auth required per portal")
            screenshot_key = result.get("no_auth_screenshot_key")
            await mark_case_no_auth_review(case_id, reason, screenshot_key=screenshot_key)
            logger.info(f"Worker/{worker_id}: case {case_id} → IN_REVIEW (no_auth): {reason}")
            await navigate_to_homepage(worker_id)
            return {"status": "no_auth_review", "reason": reason}

        if result.get("case_state") == "HOLD":
            await mark_case_hold(case_id, result.get("hold_reason", "Unknown hold"))
            logger.info(f"Worker/{worker_id}: case {case_id} → HOLD: {result.get('hold_reason')}")
            nav_ok = await navigate_to_homepage(worker_id)
            if not nav_ok:
                logger.warning(f"Worker/{worker_id}: home nav failed after HOLD — closing browser for fresh session")
                await close_worker_browser(worker_id)
            return {"status": "hold", "hold_reason": result.get("hold_reason")}

        if result.get("answers"):
            logger.info(
                f"Worker/{worker_id}: case {case_id} → review "
                f"({len(result['answers'])} questions, "
                f"auto_approved={result.get('auto_approved')}, "
                f"gold_card_level={result.get('gold_card_level')})"
            )
            await navigate_to_homepage(worker_id)
            return {
                "status": "review",
                "answers": result["answers"],
                "review_round": result.get("review_round", 1),
                "auto_approved": result.get("auto_approved"),
                "cdo_approved": result.get("cdo_approved"),
                "gold_card_level": result.get("gold_card_level"),
            }

        # No questions — auto-approved pathway or error
        logger.info(f"Worker/{worker_id}: case {case_id} → auto_approved")
        await navigate_to_homepage(worker_id)
        return {"status": "auto_approved", "result": result}

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Worker/{worker_id}: case {case_id} error: {error_msg}")

        # Mark case as HOLD with error detail
        try:
            await mark_case_hold(case_id, f"Portal error: {error_msg[:200]}")
        except Exception:
            pass

        # Only close browser if it's truly dead
        browser_dead = "TargetClosedError" in error_msg or "browser has been closed" in error_msg
        if browser_dead:
            await close_worker_browser(worker_id)
            logger.info(f"Worker/{worker_id}: browser dead — closed for re-login")
        else:
            nav_ok = await navigate_to_homepage(worker_id)
            if not nav_ok:
                logger.warning(f"Worker/{worker_id}: home nav failed after error — closing browser")
                await close_worker_browser(worker_id)
            else:
                logger.info(f"Worker/{worker_id}: recovered to home after error")

        return {
            "status": "hold",
            "hold_reason": f"Portal error: {error_msg[:200]}",
            "case_id": case_id,
        }

    finally:
        pass  # Batch counter removed — WorkerLoop handles flow control


@app.post("/finalize-case")
async def finalize_case(event: dict) -> dict:
    """Run portal finalize with approved answers → submit.

    Called after rep approval. Browser may have been closed
    during the review period — ensure_login handles re-login.
    """
    worker_id = event.get("worker_id", "unknown")
    case_id = event["case_id"]
    case_data = event["case_data"]
    clinical_context = event.get("clinical_context")
    approved_answers = event["approved_answers"]
    credentials = event.get("credentials")

    logger.info(
        f"Worker/{worker_id}: finalize-case for {case_id} "
        f"({len(approved_answers)} approved answers)"
    )

    try:
        # Step 1: Ensure browser is logged in
        await ensure_browser_login(worker_id, credentials)

        # Step 2: Run compiler with approved answers → fast-forward → submit
        raw_session = _BROWSER_REGISTRY[worker_id]
        session = _to_portal_session(raw_session)
        compiler = load_compiler("carelon_provider_portal")
        result = await compiler.execute(
            case=case_data,
            session=session,
            clinical_context=clinical_context,
            dry_run=False,
            resume_answers=approved_answers,
        )

        # Step 3: Save flow checks from the finalize pass
        try:
            await save_flow_checks(case_id, result)
        except Exception as fc_err:
            logger.warning(f"Worker/{worker_id}: flow check save failed (non-fatal): {fc_err}")

        # Step 4: Check result — compiler may return HOLD (e.g. facility timeout)
        if result.get("case_state") == "HOLD":
            hold_reason = result.get("hold_reason", "Unknown finalize error")
            await mark_case_hold(case_id, hold_reason)
            logger.warning(
                f"Worker/{worker_id}: case {case_id} finalize → HOLD: {hold_reason}"
            )
            await navigate_to_homepage(worker_id)
            return {"status": "hold", "hold_reason": hold_reason}

        # Step 5: Save completion state
        await mark_case_complete(case_id, result)

        logger.info(
            f"Worker/{worker_id}: case {case_id} submitted — "
            f"auth={result.get('auth_number')}"
        )

        await navigate_to_homepage(worker_id)
        return {"status": "submitted", "result": result}

    except Exception as e:
        logger.error(f"Worker/{worker_id}: finalize error for {case_id}: {e}")
        if is_portal_error(str(e)):
            await close_worker_browser(worker_id)
        return {"status": "error", "error": str(e)}


@app.post("/close-browser")
async def close_browser(event: dict | None = None) -> dict:
    """Close the browser after batch is done."""
    worker_id = (event or {}).get("worker_id", "unknown")
    await close_worker_browser(worker_id)

    # Also kill the Playwright instance to fully release Chrome process
    import app.worker.browser_manager as bm
    if bm._PLAYWRIGHT_INSTANCE:
        try:
            await bm._PLAYWRIGHT_INSTANCE.stop()
            bm._PLAYWRIGHT_INSTANCE = None
        except Exception:
            pass

    logger.info(f"Worker/{worker_id}: browser closed (batch complete)")
    return {"status": "closed"}


@app.get("/status")
async def status() -> dict:
    """Query worker status — health check for orchestrator."""
    # Find all active browser sessions on this worker
    active_workers = list(_BROWSER_REGISTRY.keys())
    return {
        "active_workers": active_workers,
        "browser_count": len(active_workers),
        "batch_remaining": {},  # Deprecated — WorkerLoop handles flow control
        "timestamp": time.time(),
    }
