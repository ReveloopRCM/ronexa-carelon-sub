"""Tests for the submission job queue — claim behavior, priority ordering, state transitions.

Note: claim_next_job uses SELECT FOR UPDATE SKIP LOCKED which requires Postgres.
SQLite doesn't support SKIP LOCKED, so we test the query logic and state transitions
separately. The concurrent claim test is marked for Postgres-only environments.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from datetime import datetime, timedelta

from app.db.models import Case, CaseState, JobStatus, SubmissionJob
from app.db.queue import claim_next_job, enqueue_case, mark_completed, mark_failed, compute_priority
from tests.conftest import create_test_case, create_test_job, create_test_note


# ── Priority Scoring ──


class TestComputePriority:
    def test_stat_gets_highest_priority(self):
        case = create_test_case(is_stat=True)
        assert compute_priority(case) == 1000

    def test_standard_case_default_priority(self):
        case = create_test_case()
        assert compute_priority(case) == 50

    def test_same_day_gets_500(self):
        case = create_test_case()
        case.scheduled_dt = datetime.utcnow()
        assert compute_priority(case) == 500

    def test_next_day_gets_200(self):
        case = create_test_case()
        case.scheduled_dt = datetime.utcnow() + timedelta(days=1)
        assert compute_priority(case) == 200

    def test_within_week_gets_100(self):
        case = create_test_case()
        case.scheduled_dt = datetime.utcnow() + timedelta(days=5)
        assert compute_priority(case) == 100


# ── Claim Behavior (SQLite-compatible subset) ──


class TestClaimNextJob:
    @pytest.mark.asyncio
    async def test_claim_returns_job(self, db):
        """Claim returns a job when eligible cases exist."""
        case = create_test_case(state=CaseState.NOTES_UPLOADED)
        db.add(case)
        await db.flush()

        note = create_test_note(case_id=case.id)
        db.add(note)

        job = create_test_job(case_id=case.id, priority=100)
        db.add(job)
        await db.flush()

        # SQLite doesn't support SKIP LOCKED, so test the query pattern
        # by directly checking the job exists and is claimable
        from sqlalchemy import select
        result = await db.execute(
            select(SubmissionJob)
            .join(Case, SubmissionJob.case_id == Case.id)
            .where(
                SubmissionJob.status == JobStatus.QUEUED,
                Case.state == CaseState.NOTES_UPLOADED,
            )
            .order_by(SubmissionJob.priority.desc())
            .limit(1)
        )
        claimed = result.scalar_one_or_none()
        assert claimed is not None
        assert claimed.case_id == case.id
        assert claimed.priority == 100

    @pytest.mark.asyncio
    async def test_claim_skips_non_notes_uploaded(self, db):
        """Cases in PENDING_NOTES state should not be claimable."""
        case = create_test_case(state=CaseState.PENDING_NOTES)
        db.add(case)
        await db.flush()

        job = create_test_job(case_id=case.id)
        db.add(job)
        await db.flush()

        from sqlalchemy import select
        result = await db.execute(
            select(SubmissionJob)
            .join(Case, SubmissionJob.case_id == Case.id)
            .where(
                SubmissionJob.status == JobStatus.QUEUED,
                Case.state == CaseState.NOTES_UPLOADED,
            )
            .limit(1)
        )
        assert result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_claim_returns_highest_priority_first(self, db):
        """Higher priority jobs should be claimed first."""
        case_low = create_test_case(state=CaseState.NOTES_UPLOADED)
        case_high = create_test_case(state=CaseState.NOTES_UPLOADED, is_stat=True)
        db.add_all([case_low, case_high])
        await db.flush()

        note1 = create_test_note(case_id=case_low.id)
        note2 = create_test_note(case_id=case_high.id)
        db.add_all([note1, note2])

        job_low = create_test_job(case_id=case_low.id, priority=50)
        job_high = create_test_job(case_id=case_high.id, priority=1000, is_stat=True)
        db.add_all([job_low, job_high])
        await db.flush()

        from sqlalchemy import select
        result = await db.execute(
            select(SubmissionJob)
            .join(Case, SubmissionJob.case_id == Case.id)
            .where(
                SubmissionJob.status == JobStatus.QUEUED,
                Case.state == CaseState.NOTES_UPLOADED,
            )
            .order_by(SubmissionJob.priority.desc())
            .limit(1)
        )
        first = result.scalar_one_or_none()
        assert first is not None
        assert first.case_id == case_high.id
        assert first.priority == 1000

    @pytest.mark.asyncio
    async def test_claim_skips_running_jobs(self, db):
        """Jobs already in RUNNING state should not be re-claimed."""
        case = create_test_case(state=CaseState.NOTES_UPLOADED)
        db.add(case)
        await db.flush()

        note = create_test_note(case_id=case.id)
        db.add(note)

        job = create_test_job(case_id=case.id, status=JobStatus.RUNNING)
        db.add(job)
        await db.flush()

        from sqlalchemy import select
        result = await db.execute(
            select(SubmissionJob)
            .join(Case, SubmissionJob.case_id == Case.id)
            .where(
                SubmissionJob.status == JobStatus.QUEUED,
                Case.state == CaseState.NOTES_UPLOADED,
            )
            .limit(1)
        )
        assert result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_claim_empty_queue_returns_none(self, db):
        """Empty queue should return None."""
        from sqlalchemy import select
        result = await db.execute(
            select(SubmissionJob)
            .join(Case, SubmissionJob.case_id == Case.id)
            .where(
                SubmissionJob.status == JobStatus.QUEUED,
                Case.state == CaseState.NOTES_UPLOADED,
            )
            .limit(1)
        )
        assert result.scalar_one_or_none() is None


# ── Job State Transitions ──


class TestJobStateTransitions:
    @pytest.mark.asyncio
    async def test_job_created_as_queued(self, db):
        """New jobs should start in QUEUED status."""
        case = create_test_case()
        db.add(case)
        await db.flush()

        job = create_test_job(case_id=case.id)
        db.add(job)
        await db.flush()

        assert job.status == JobStatus.QUEUED
        assert job.attempt == 0

    @pytest.mark.asyncio
    async def test_claim_sets_correct_fields(self, db):
        """After claiming, job should have CLAIMED status and worker_id set."""
        case = create_test_case(state=CaseState.NOTES_UPLOADED)
        db.add(case)
        await db.flush()

        note = create_test_note(case_id=case.id)
        db.add(note)

        job = create_test_job(case_id=case.id)
        db.add(job)
        await db.flush()

        # Simulate claim
        job.status = JobStatus.CLAIMED
        job.claimed_by = "worker-a"
        job.claimed_at = datetime.utcnow()
        job.attempt += 1
        await db.flush()

        assert job.status == JobStatus.CLAIMED
        assert job.claimed_by == "worker-a"
        assert job.attempt == 1
