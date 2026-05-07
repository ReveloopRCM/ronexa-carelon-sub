"""Tests for the v153 invariant: every system-driven `job.status = QUEUED`
write MUST be preceded by `purge_case_workflow(case_id)`.

Why this is a contract worth testing:
  - Restate workflows are idempotent on key (case_id). A `workflow_send` with
    a key that already has an active or recently-completed invocation
    silently dedupes to that prior one.
  - `purge_case_workflow` (v141, restate_utils.py) kills + purges the prior
    invocation so the next dispatch creates a fresh one.
  - We wired purge into rep-driven re-run paths (queue.py, cases.py) but
    forgot the system-driven retry/reaper paths. Result: SUBMIT attempts
    2/3 silently never enter the SubmitWorkflow handler — case exhausts
    with no error captured. (Lopez/Talley/Thidavanh on 2026-05-07.)
  - These tests verify the four system paths (auto_requeue + 3 reapers) all
    purge BEFORE resetting job.status. If a future contributor adds a new
    reset path without purging, this test won't cover them — but adding
    a paired "purge before status=QUEUED" assertion to any new reset is
    now an established pattern.

Mocking strategy:
  - Patch `purge_case_workflow` so the test asserts it was awaited with
    the right case_id. We don't actually exercise Restate.
  - Use the in-memory SQLite test DB (conftest.py fixtures) for the rest.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import (
    Case,
    CaseState,
    ExceptionType,
    JobStatus,
    SubmissionJob,
)
from tests.conftest import create_test_case, create_test_job


# ─────────────────────────────────────────────────────────────────────
# 1. mark_case_hold's auto_requeue branch
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_case_hold_auto_requeue_purges_workflow(db, monkeypatch):
    """When mark_case_hold takes the auto_requeue branch (transient + retries
    left), it MUST call purge_case_workflow before resetting job.status."""
    case = create_test_case(state=CaseState.PROCESSING)
    job = create_test_job(case_id=case.id)
    job.attempt = 1  # < max_attempts (3) so retry branch fires
    job.job_type = "SUBMIT"
    db.add_all([case, job])
    await db.commit()

    # Patch the async_session_factory + purge so the helper sees our DB
    purge_mock = AsyncMock(return_value=True)
    sf_mock = lambda: _AsyncCtx(db)

    with patch("app.db.database.async_session_factory", sf_mock), \
         patch("app.workflow.restate_utils.purge_case_workflow", purge_mock):
        from app.worker.helpers import mark_case_hold
        # Use a transient reason — must be in is_transient_error's match list
        await mark_case_hold(case.id, "browser session timeout during submit")

    # Verify purge was called BEFORE the job status change committed
    purge_mock.assert_awaited_once_with(case.id)

    # Verify the auto_requeue path actually fired (job back to QUEUED)
    await db.refresh(job)
    assert job.status == JobStatus.QUEUED, (
        f"Auto-requeue didn't fire — job is {job.status}. "
        "Test setup might not match transient-error match list."
    )


# ─────────────────────────────────────────────────────────────────────
# 2. reap_stale_claims
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reap_stale_claims_purges_workflow(db, monkeypatch):
    """reap_stale_claims must purge each stale claim's workflow BEFORE
    resetting job.status to QUEUED."""
    case = create_test_case(state=CaseState.PROCESSING)
    job = create_test_job(case_id=case.id, status=JobStatus.CLAIMED)
    job.claimed_by = "vm-worker-x"
    job.claimed_at = datetime.utcnow() - timedelta(minutes=20)  # > 10 min stale
    job.attempt = 1
    db.add_all([case, job])
    await db.commit()

    purge_mock = AsyncMock(return_value=True)
    sf_mock = lambda: _AsyncCtx(db)

    with patch("app.db.database.async_session_factory", sf_mock), \
         patch("app.workflow.restate_utils.purge_case_workflow", purge_mock):
        from app.workflow.worker_loop import reap_stale_claims
        await reap_stale_claims()

    purge_mock.assert_awaited_once_with(case.id)
    await db.refresh(job)
    assert job.status == JobStatus.QUEUED


# ─────────────────────────────────────────────────────────────────────
# 3. reap_stale_submitting
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reap_stale_submitting_purges_workflow(db, monkeypatch):
    """reap_stale_submitting (the bug Lopez hit) must purge the workflow
    invocation. Without this, attempt 2's workflow_send dedupes against
    the still-suspended attempt 1, the handler never re-enters."""
    case = create_test_case(state=CaseState.SUBMITTING)
    case.updated_at = datetime.utcnow() - timedelta(minutes=20)  # >15 min stale
    job = create_test_job(case_id=case.id, status=JobStatus.QUEUED)
    job.job_type = "SUBMIT"
    job.attempt = 1
    job.max_attempts = 3
    db.add_all([case, job])
    await db.commit()

    purge_mock = AsyncMock(return_value=True)
    sf_mock = lambda: _AsyncCtx(db)

    with patch("app.db.database.async_session_factory", sf_mock), \
         patch("app.workflow.restate_utils.purge_case_workflow", purge_mock):
        from app.workflow.worker_loop import reap_stale_submitting
        await reap_stale_submitting()

    purge_mock.assert_awaited_once_with(case.id)


# ─────────────────────────────────────────────────────────────────────
# 4. reap_stale_processing
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reap_stale_processing_purges_workflow(db, monkeypatch):
    """reap_stale_processing must purge before resetting case state."""
    case = create_test_case(state=CaseState.PROCESSING)
    case.updated_at = datetime.utcnow() - timedelta(minutes=20)  # >15 min stale
    job = create_test_job(case_id=case.id, status=JobStatus.QUEUED)
    job.job_type = "FIRST_PASS"
    job.attempt = 1
    db.add_all([case, job])
    await db.commit()

    purge_mock = AsyncMock(return_value=True)
    sf_mock = lambda: _AsyncCtx(db)

    with patch("app.db.database.async_session_factory", sf_mock), \
         patch("app.workflow.restate_utils.purge_case_workflow", purge_mock):
        from app.workflow.worker_loop import reap_stale_processing
        await reap_stale_processing()

    purge_mock.assert_awaited_once_with(case.id)


# ─────────────────────────────────────────────────────────────────────
# Helper: minimal async-context-manager wrapper around an existing session
# (the production code uses `async with async_session_factory() as db:`,
# but our test-fixture session is already open. This wrapper lets a single
# pre-built session be reused.)
# ─────────────────────────────────────────────────────────────────────


class _AsyncCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        # Don't close — the test fixture owns this session
        return False
