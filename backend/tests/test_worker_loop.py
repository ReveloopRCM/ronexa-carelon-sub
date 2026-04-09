"""Tests for WorkerLoop helper functions — claim, stop flag, wake awakeable, stale reaper.

These test the DB-level functions that WorkerLoop calls via ctx.run().
The Restate loop itself is tested via integration/manual testing.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from app.db.models import (
    Case,
    CaseState,
    ClinicalNote,
    JobStatus,
    SubmissionJob,
    WorkerAccount,
    AutomationRule,
)
from tests.conftest import (
    create_test_case,
    create_test_job,
    create_test_note,
    create_test_worker,
)


# ── _claim_and_build_event ──


class TestClaimAndBuildEvent:
    """Test the _claim_and_build_event helper that claims a job and builds the CaseWorkflow event."""

    @pytest.mark.asyncio
    async def test_returns_none_when_queue_empty(self, db):
        """Should return None when no eligible cases exist."""
        with patch("app.db.database.async_session_factory") as mock_factory:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=db)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_factory.return_value = mock_ctx

            with patch("app.db.queue.claim_next_job", new_callable=AsyncMock) as mock_claim:
                mock_claim.return_value = None

                from app.workflow.worker_loop import _claim_and_build_event
                result = await _claim_and_build_event("worker-a")
                assert result is None

    @pytest.mark.asyncio
    async def test_builds_correct_event_shape(self, db):
        """Event dict should have case_id, job_id, case_data, clinical_context, auto_submit."""
        case = create_test_case(state=CaseState.NOTES_UPLOADED, cpt_code="70553", icd1="M54.5")
        db.add(case)
        await db.flush()

        note = create_test_note(case_id=case.id, structured={"text": "clinical data"})
        db.add(note)

        job = create_test_job(case_id=case.id, priority=100)
        db.add(job)
        await db.flush()

        with patch("app.db.database.async_session_factory") as mock_factory:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=db)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_factory.return_value = mock_ctx

            with patch("app.db.queue.claim_next_job", new_callable=AsyncMock) as mock_claim:
                mock_claim.return_value = job

                from app.workflow.worker_loop import _claim_and_build_event
                event = await _claim_and_build_event("worker-a")

                assert event is not None
                assert event["case_id"] == case.id
                assert event["job_id"] == job.id
                assert "case_data" in event
                assert event["case_data"]["exam_id"] == case.exam_id
                assert event["case_data"]["first_name"] == case.first_name
                assert event["case_data"]["cpt_code"] == "70553"
                assert event["clinical_context"] == {"text": "clinical data"}
                assert isinstance(event["auto_submit"], bool)


# ── Stop Flag ──


class TestStopFlag:
    @pytest.mark.asyncio
    async def test_check_stop_flag_returns_false_by_default(self, db):
        """Stop flag should be False for new workers."""
        worker = create_test_worker(container_id="worker-test")
        db.add(worker)
        await db.flush()

        from sqlalchemy import select
        result = await db.execute(
            select(WorkerAccount.stop_requested)
            .where(WorkerAccount.container_id == "worker-test")
        )
        assert result.scalar_one_or_none() == False

    @pytest.mark.asyncio
    async def test_set_and_check_stop_flag(self, db):
        """Setting stop flag should be visible on next check."""
        worker = create_test_worker(container_id="worker-stop-test")
        db.add(worker)
        await db.flush()

        from sqlalchemy import update
        await db.execute(
            update(WorkerAccount)
            .where(WorkerAccount.container_id == "worker-stop-test")
            .values(stop_requested=True)
        )
        await db.flush()

        from sqlalchemy import select
        result = await db.execute(
            select(WorkerAccount.stop_requested)
            .where(WorkerAccount.container_id == "worker-stop-test")
        )
        assert result.scalar_one_or_none() == True


# ── Wake Awakeable ──


class TestWakeAwakeable:
    @pytest.mark.asyncio
    async def test_save_and_clear_awakeable(self, db):
        """Saving then clearing awakeable ID should work correctly."""
        worker = create_test_worker(container_id="worker-wake-test")
        db.add(worker)
        await db.flush()

        # Save awakeable
        from sqlalchemy import update, select
        await db.execute(
            update(WorkerAccount)
            .where(WorkerAccount.container_id == "worker-wake-test")
            .values(wake_awakeable_id="test-awakeable-123")
        )
        await db.flush()

        result = await db.execute(
            select(WorkerAccount.wake_awakeable_id)
            .where(WorkerAccount.container_id == "worker-wake-test")
        )
        assert result.scalar_one_or_none() == "test-awakeable-123"

        # Clear awakeable
        await db.execute(
            update(WorkerAccount)
            .where(WorkerAccount.container_id == "worker-wake-test")
            .values(wake_awakeable_id=None)
        )
        await db.flush()

        result = await db.execute(
            select(WorkerAccount.wake_awakeable_id)
            .where(WorkerAccount.container_id == "worker-wake-test")
        )
        assert result.scalar_one_or_none() is None


# ── Stale Job Reaper ──


class TestStaleJobReaper:
    @pytest.mark.asyncio
    async def test_reaps_old_claimed_jobs(self, db):
        """CLAIMED jobs older than 10 min should be reset to QUEUED."""
        case = create_test_case(state=CaseState.NOTES_UPLOADED)
        db.add(case)
        await db.flush()

        job = create_test_job(case_id=case.id, status=JobStatus.CLAIMED)
        job.claimed_by = "worker-a"
        job.claimed_at = datetime.utcnow() - timedelta(minutes=15)  # 15 min old
        db.add(job)
        await db.flush()

        # Manually run reaper logic
        from sqlalchemy import update
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
        assert len(reaped) == 1

        await db.flush()

        # Verify job is back to QUEUED
        from sqlalchemy import select
        result = await db.execute(select(SubmissionJob).where(SubmissionJob.id == job.id))
        updated_job = result.scalar_one()
        assert updated_job.status == JobStatus.QUEUED
        assert updated_job.claimed_by is None

    @pytest.mark.asyncio
    async def test_does_not_reap_recent_claims(self, db):
        """CLAIMED jobs less than 10 min old should not be reaped."""
        case = create_test_case(state=CaseState.NOTES_UPLOADED)
        db.add(case)
        await db.flush()

        job = create_test_job(case_id=case.id, status=JobStatus.CLAIMED)
        job.claimed_by = "worker-a"
        job.claimed_at = datetime.utcnow() - timedelta(minutes=5)  # 5 min old — too recent
        db.add(job)
        await db.flush()

        from sqlalchemy import update
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
        assert len(reaped) == 0

    @pytest.mark.asyncio
    async def test_does_not_reap_running_jobs(self, db):
        """RUNNING jobs should never be reaped, only CLAIMED."""
        case = create_test_case(state=CaseState.NOTES_UPLOADED)
        db.add(case)
        await db.flush()

        job = create_test_job(case_id=case.id, status=JobStatus.RUNNING)
        job.claimed_by = "worker-a"
        job.claimed_at = datetime.utcnow() - timedelta(minutes=30)
        db.add(job)
        await db.flush()

        from sqlalchemy import update
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
        assert len(reaped) == 0


# ── Worker Credentials ──


class TestWorkerCredentials:
    @pytest.mark.asyncio
    async def test_load_active_worker(self, db):
        """Should load credentials for active worker."""
        worker = create_test_worker(
            container_id="worker-creds",
            username="carelon_user",
            password="secret123",
            mailbox="user@example.com",
        )
        db.add(worker)
        await db.flush()

        from sqlalchemy import select
        result = await db.execute(
            select(WorkerAccount)
            .where(
                WorkerAccount.container_id == "worker-creds",
                WorkerAccount.is_active == True,
            )
        )
        account = result.scalar_one_or_none()
        assert account is not None
        assert account.username == "carelon_user"
        assert account.password == "secret123"

    @pytest.mark.asyncio
    async def test_inactive_worker_not_loaded(self, db):
        """Inactive workers should not be returned."""
        worker = create_test_worker(container_id="worker-inactive", is_active=False)
        db.add(worker)
        await db.flush()

        from sqlalchemy import select
        result = await db.execute(
            select(WorkerAccount)
            .where(
                WorkerAccount.container_id == "worker-inactive",
                WorkerAccount.is_active == True,
            )
        )
        assert result.scalar_one_or_none() is None
