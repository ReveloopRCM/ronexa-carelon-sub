"""Shared test fixtures for Ronexa backend tests.

Uses an in-memory SQLite database for unit tests. For integration tests
that require Postgres-specific features (SKIP LOCKED), use a real test DB.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import event, JSON
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

from app.db.models import (
    Base,
    Case,
    CaseState,
    ClinicalNote,
    JobStatus,
    SubmissionJob,
    WorkerAccount,
    AccountShift,
    AutomationRule,
)


def _uuid() -> str:
    return str(uuid.uuid4())


# ── SQLite async engine for unit tests ──

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_engine():
    """Create an async SQLite engine for testing.

    SQLite doesn't support JSONB, so we replace it with JSON at the type level.
    """
    # Monkey-patch JSONB columns to use JSON for SQLite compatibility
    from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB

    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, PG_JSONB):
                column.type = JSON()

    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture
async def db(db_engine):
    """Provide an async session for testing."""
    async_session = sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session
        await session.rollback()


# ── Factory Functions ──


def create_test_case(
    *,
    case_id: str | None = None,
    exam_id: str | None = None,
    state: CaseState = CaseState.NOTES_UPLOADED,
    first_name: str = "Test",
    last_name: str = "Patient",
    dob: str = "1990-01-01",
    policy_num: str = "POL123",
    center_npi: str = "1234567890",
    cpt_code: str = "70553",
    icd1: str | None = "M54.5",
    is_stat: bool = False,
    clinical_blob_key: str | None = None,
) -> Case:
    """Create a Case instance for testing."""
    return Case(
        id=case_id or _uuid(),
        exam_id=exam_id or f"EXAM-{_uuid()[:8]}",
        first_name=first_name,
        last_name=last_name,
        dob=dob,
        policy_num=policy_num,
        center_npi=center_npi,
        cpt_code=cpt_code,
        icd1=icd1,
        is_stat=is_stat,
        state=state,
        clinical_blob_key=clinical_blob_key,
        sort_priority=1 if is_stat else 3,
        raw_data={},
    )


def create_test_job(
    *,
    case_id: str,
    status: JobStatus = JobStatus.QUEUED,
    priority: int = 50,
    is_stat: bool = False,
) -> SubmissionJob:
    """Create a SubmissionJob instance for testing."""
    return SubmissionJob(
        id=_uuid(),
        case_id=case_id,
        status=status,
        priority=priority,
        is_stat=is_stat,
        attempt=0,
        max_attempts=3,
        created_at=datetime.utcnow(),
    )


def create_test_worker(
    *,
    container_id: str = "worker-a",
    username: str = "testuser",
    password: str = "testpass",
    mailbox: str = "test@example.com",
    is_active: bool = True,
    shift: AccountShift = AccountShift.DAY,
) -> WorkerAccount:
    """Create a WorkerAccount instance for testing."""
    return WorkerAccount(
        id=_uuid(),
        container_id=container_id,
        username=username,
        password=password,
        mailbox_address=mailbox,
        is_active=is_active,
        shift=shift,
        stop_requested=False,
        wake_awakeable_id=None,
    )


def create_test_note(
    *,
    case_id: str,
    structured: dict | None = None,
) -> ClinicalNote:
    """Create a ClinicalNote instance for testing."""
    return ClinicalNote(
        id=_uuid(),
        case_id=case_id,
        filename="test.pdf",
        page_count=3,
        document_type="CLINICAL",
        document_quality="CLEAN",
        extraction_method="test",
        structured=structured or {"text": "Sample clinical notes", "confidence": 0.95},
    )
