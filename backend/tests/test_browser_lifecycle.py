"""Tests for v156 browser-lifecycle resilience.

Why these contracts are worth pinning:
  - Pre-v156 we only closed the worker browser on errors whose string
    contained "TargetClosedError" or "browser has been closed". A
    `Page.wait_for_selector: Timeout` waiting for BtnSearch, an
    `ElementHandle.fill: Element is not attached`, a `Page.evaluate:
    Target page closed` — all left the browser open. The next case
    picked up by the same worker reused the (wedged) browser and hit
    an identical error. Three retries in a row → exhausted → HOLD.
  - Pre-v156 the liveness check in `_ensure_session_direct` was
    `document.readyState` + URL match. A page wedged on a modal still
    evaluates that readyState fine, so the zombie session was reused.

v156 fixes both. Tests below:
  1. mark_case_hold's auto_requeue branch sets a 60s cooldown when the
     hold_reason matches a browser-lifecycle pattern (so the next claim
     doesn't race the browser teardown).
  2. mark_case_hold keeps the existing 5-min cooldown for portal-flake
     patterns (no regression on v148 behavior).
  3. mark_case_hold leaves cooldown=None for transient errors that match
     neither bucket (one-off network blips retry immediately, as before).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.db.models import (
    Case,
    CaseState,
    JobStatus,
    SubmissionJob,
)
from app.worker.helpers import mark_case_hold
from tests.conftest import create_test_case, create_test_job


# ─────────────────────────────────────────────────────────────────────
# Helpers — patch the helper's session_factory so it reads our test DB
# ─────────────────────────────────────────────────────────────────────


class _AsyncCtx:
    """Minimal async-context-manager wrapper around an existing session.
    The production helper does `async with async_session_factory() as db:`,
    but the test fixture session is already open. This lets the
    pre-built session be reused for the inner block."""
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


# ─────────────────────────────────────────────────────────────────────
# Browser-lifecycle 60s cooldown (v156)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("reason", [
    "Portal error: Page.wait_for_selector: Timeout 15000ms exceeded waiting for #asPrimary_ctl00_BtnSearch",
    "Portal error: ElementHandle.fill: Element is not attached to the DOM",
    "Portal error: Page.evaluate: Target page, context or browser has been closed",
    "Portal error: Page.reload: Target page, context or browser has been closed",
    "Portal error: frame was detached during click",
])
@pytest.mark.asyncio
async def test_browser_lifecycle_pattern_sets_60s_cooldown(db, reason):
    """Each of the actual prod error messages from Kathryn Harris /
    Tyler Tanjuatco / Willie Parks / Jay Stephens / Shawn Adams triggers
    the SHORT cooldown bucket. 5-min would unnecessarily delay the next
    retry; immediate would race the close+relaunch from /process-case."""
    case = create_test_case(state=CaseState.PROCESSING)
    job = create_test_job(case_id=case.id)
    job.attempt = 1                     # < max_attempts so auto_requeue fires
    job.job_type = "ORDER"
    job.cooldown_until = None
    db.add_all([case, job])
    await db.commit()

    sf_mock = lambda: _AsyncCtx(db)
    with patch("app.db.database.async_session_factory", sf_mock), \
         patch("app.workflow.restate_utils.purge_case_workflow",
               new=_async_noop_true):
        await mark_case_hold(case.id, reason)

    await db.refresh(job)
    assert job.cooldown_until is not None, (
        f"Expected cooldown set for browser-lifecycle pattern: {reason!r}"
    )
    delta = job.cooldown_until - datetime.utcnow()
    # Allow some slack — should be ~60s, definitely NOT ~5 min
    assert timedelta(seconds=45) <= delta <= timedelta(seconds=75), (
        f"Expected ~60s cooldown, got {delta.total_seconds():.0f}s "
        f"for reason: {reason!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# Portal-flake 5min cooldown — existing v148 behavior, no regression
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("reason", [
    "Portal error: Provider search page did not load after transition",
    "Portal error: Could not select provider — postback timeout",
    "Portal error: Facility continue failed",
])
@pytest.mark.asyncio
async def test_portal_flake_pattern_keeps_5min_cooldown(db, reason):
    """The v148 portal-flake patterns still get the 5-min cooldown.
    Verifying we didn't regress when adding the v156 short bucket."""
    case = create_test_case(state=CaseState.PROCESSING)
    job = create_test_job(case_id=case.id)
    job.attempt = 1
    job.job_type = "ORDER"
    job.cooldown_until = None
    db.add_all([case, job])
    await db.commit()

    sf_mock = lambda: _AsyncCtx(db)
    with patch("app.db.database.async_session_factory", sf_mock), \
         patch("app.workflow.restate_utils.purge_case_workflow",
               new=_async_noop_true):
        await mark_case_hold(case.id, reason)

    await db.refresh(job)
    assert job.cooldown_until is not None
    delta = job.cooldown_until - datetime.utcnow()
    # ~5min ± slack
    assert timedelta(minutes=4, seconds=45) <= delta <= timedelta(minutes=5, seconds=15), (
        f"Expected ~5min cooldown, got {delta.total_seconds():.0f}s "
        f"for reason: {reason!r}"
    )


# ─────────────────────────────────────────────────────────────────────
# Generic transient — no cooldown, retry immediately
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generic_transient_has_no_cooldown(db):
    """An error that's transient (e.g. matches "network" or "timeout")
    but isn't one of the named buckets should NOT get a cooldown —
    retry immediately, as before. "network" alone is a one-off blip,
    not the 60s-worth-of-teardown case."""
    case = create_test_case(state=CaseState.PROCESSING)
    job = create_test_job(case_id=case.id)
    job.attempt = 1
    job.job_type = "ORDER"
    job.cooldown_until = datetime.utcnow() + timedelta(seconds=999)  # pre-set, should be cleared
    db.add_all([case, job])
    await db.commit()

    sf_mock = lambda: _AsyncCtx(db)
    with patch("app.db.database.async_session_factory", sf_mock), \
         patch("app.workflow.restate_utils.purge_case_workflow",
               new=_async_noop_true):
        await mark_case_hold(case.id, "Network glitch during postback")

    await db.refresh(job)
    assert job.cooldown_until is None, (
        "Generic transient should clear any prior cooldown to retry immediately"
    )


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


async def _async_noop_true(*args, **kwargs):
    """AsyncMock-equivalent for purge_case_workflow."""
    return True
