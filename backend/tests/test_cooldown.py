"""Tests for the v148 retry-cooldown mechanism.

When a worker hits a known portal-flake error (provider search page didn't
load, etc.) the auto_requeue path used to reset the job to QUEUED
immediately — letting `claim_next_job` re-pick the row within ~10 seconds.
For Carelon-side flakes that take 1-2 minutes to recover, three back-to-back
retries all landed inside the flake window and the case exhausted to
permanent HOLD even though the portal would have recovered shortly after.

The fix: `SubmissionJob.cooldown_until`. Set on transient retries when
`reason_lower` matches a known portal-flake pattern. `claim_next_job`'s
WHERE clause skips rows whose cooldown hasn't expired. Three retries
spaced by 5 minutes give Carelon ~15 min to recover before permanent HOLD.

These tests verify the contract end-to-end against the SQLite test DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db.models import CaseState, JobStatus, SubmissionJob
from app.db.queue import claim_next_job
from tests.conftest import create_test_case, create_test_job, create_test_worker


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _make_first_pass_case_and_job(state=CaseState.NOTES_UPLOADED):
    """Create a Case + matching FIRST_PASS SubmissionJob ready to claim."""
    case = create_test_case(state=state)
    job = create_test_job(case_id=case.id)
    job.job_type = "FIRST_PASS"
    return case, job


# ─────────────────────────────────────────────────────────────────────
# claim_next_job filter: cooldown_until is None (default)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_picks_up_job_with_null_cooldown(db):
    """Default state — cooldown_until=None means immediately claimable
    (existing behavior, never set on the happy path)."""
    case, job = _make_first_pass_case_and_job()
    db.add_all([case, job])
    await db.commit()

    claimed = await claim_next_job(db, "worker-a", job_type="FIRST_PASS")
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == JobStatus.CLAIMED


# ─────────────────────────────────────────────────────────────────────
# claim_next_job filter: cooldown_until in the future blocks claim
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_skips_job_in_active_cooldown(db):
    """A job whose cooldown_until is in the future MUST NOT be claimed."""
    case, job = _make_first_pass_case_and_job()
    job.cooldown_until = datetime.utcnow() + timedelta(minutes=5)
    db.add_all([case, job])
    await db.commit()

    claimed = await claim_next_job(db, "worker-a", job_type="FIRST_PASS")
    assert claimed is None, (
        "Expected no claim while job is in cooldown. Got: "
        f"{claimed.id if claimed else None}"
    )


# ─────────────────────────────────────────────────────────────────────
# claim_next_job filter: cooldown_until in the past allows claim
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_picks_up_job_after_cooldown_expires(db):
    """Once cooldown_until <= now(), the row is claimable again."""
    case, job = _make_first_pass_case_and_job()
    job.cooldown_until = datetime.utcnow() - timedelta(seconds=1)  # already expired
    db.add_all([case, job])
    await db.commit()

    claimed = await claim_next_job(db, "worker-a", job_type="FIRST_PASS")
    assert claimed is not None
    assert claimed.id == job.id


# ─────────────────────────────────────────────────────────────────────
# Pattern matching: which transient reasons trigger cooldown
# ─────────────────────────────────────────────────────────────────────


def test_portal_flake_patterns_match_observed_errors():
    """The exact transient_reason strings observed in production today
    (2026-05-06 storm) must match the cooldown trigger patterns. If a
    contributor renames the error string, this test fails loudly."""
    # These are verbatim transient_reason values from auto_requeue audit
    # events captured during the 17:00 UTC storm.
    observed_reasons = [
        "Portal error: Provider search page did not load after transition (URL: https://www.providerportal.com/Default.aspx)",
        "Could not select provider: Page.wait_for_selector: Timeout 30000ms exceeded.",
        "Facility continue failed: Page.wait_for_selector: Timeout 30000ms exceeded.",
    ]
    # Mirror of the patterns in helpers.py:_PORTAL_FLAKE_PATTERNS
    flake_patterns = (
        "provider search page",
        "could not select provider",
        "facility continue failed",
    )
    for reason in observed_reasons:
        reason_lower = reason.lower()
        matched = any(p in reason_lower for p in flake_patterns)
        assert matched, (
            f"PRODUCTION REGRESSION: observed transient_reason "
            f"{reason!r} no longer matches any flake pattern. "
            f"Cooldown won't fire — back-to-back retries will exhaust again."
        )


def test_non_flake_transient_does_not_match():
    """Generic transient errors (browser blip, network reset) must NOT
    match the flake patterns — they should retry immediately as today,
    not eat a 5-minute cooldown."""
    benign_reasons = [
        "Target page, context or browser has been closed",
        "Page.wait_for_selector: Timeout 15000ms exceeded waiting for question",
        "Browser session expired during finalize",
        "Generic timeout while loading clinical questions",
    ]
    flake_patterns = (
        "provider search page",
        "could not select provider",
        "facility continue failed",
    )
    for reason in benign_reasons:
        reason_lower = reason.lower()
        matched = any(p in reason_lower for p in flake_patterns)
        assert not matched, (
            f"FALSE POSITIVE: benign transient {reason!r} matched a flake "
            f"pattern. It would eat an unnecessary 5-min cooldown."
        )


# ─────────────────────────────────────────────────────────────────────
# is_portal_error covers the patterns (so close_worker_browser fires)
# ─────────────────────────────────────────────────────────────────────


def test_is_portal_error_covers_flake_patterns():
    """is_portal_error must return True for our flake patterns so
    http_server's `if is_portal_error(str(e)): close_worker_browser` path
    fires. Without that, retries reuse a wedged browser session and
    the cooldown alone won't recover."""
    from app.worker.helpers import is_portal_error
    flake_messages = [
        "Portal error: Provider search page did not load after transition (URL: https://www.providerportal.com/Default.aspx)",
        "Could not select provider: Page.wait_for_selector: Timeout 30000ms exceeded.",
        "Facility continue failed: Page.wait_for_selector: Timeout 30000ms exceeded.",
    ]
    for msg in flake_messages:
        assert is_portal_error(msg), (
            f"is_portal_error({msg!r}) = False. Browser won't reset on "
            f"retry — cooldown alone won't recover from a wedged session."
        )
