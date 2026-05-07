"""Tests for v154 case-list pagination + multi-state filter contract.

Why this is a contract worth testing:
  - The Cases page silently truncates rows when today's volume exceeds the
    fetch limit (Heredia/2026-05-07 — case in DB but invisible in GUI when
    rank > 500). This test pins the new repository contract that fixes it:
      1. `states` (list) replaces single-`state` filtering — needed so each
         tab can fetch only its own states server-side.
      2. `(rows, total)` return shape — frontend needs total for "page X of Y".
      3. `order_by_recent=True` — most-recently-touched rows first, so
         terminal cases (which complete late in the day) don't sink to
         the back of the result set.
  - Backward-compat: legacy single-`state=X` callers still work (passes
    through as a list-of-one).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.db import repositories as repo
from app.db.models import CaseState
from tests.conftest import create_test_case


@pytest.mark.asyncio
async def test_list_cases_returns_rows_and_total(db):
    """Repo returns (rows, total). Total is the row count BEFORE limit/offset."""
    for i in range(7):
        db.add(create_test_case(state=CaseState.APPROVED))
    await db.commit()

    rows, total = await repo.list_cases(db, limit=3, offset=0)
    assert total == 7
    assert len(rows) == 3

    rows, total = await repo.list_cases(db, limit=3, offset=3)
    assert total == 7
    assert len(rows) == 3  # rows 4, 5, 6

    rows, total = await repo.list_cases(db, limit=3, offset=6)
    assert total == 7
    assert len(rows) == 1  # last page has just one row


@pytest.mark.asyncio
async def test_list_cases_multi_state_filter(db):
    """`states=[A, B]` returns rows in EITHER state (server-side IN clause)."""
    db.add(create_test_case(state=CaseState.APPROVED))
    db.add(create_test_case(state=CaseState.DENIED))
    db.add(create_test_case(state=CaseState.PENDED))
    db.add(create_test_case(state=CaseState.HOLD))     # excluded
    db.add(create_test_case(state=CaseState.PROCESSING))  # excluded
    await db.commit()

    rows, total = await repo.list_cases(
        db,
        states=[CaseState.APPROVED, CaseState.DENIED, CaseState.PENDED],
        limit=100,
    )
    assert total == 3
    states = sorted(r.state.value for r in rows)
    assert states == ["APPROVED", "DENIED", "PENDED"]


@pytest.mark.asyncio
async def test_list_cases_legacy_single_state_still_works(db):
    """Old `state=X` (single value, not list) keeps working — list-of-one
    semantics. Existing callers don't need to change."""
    db.add(create_test_case(state=CaseState.APPROVED))
    db.add(create_test_case(state=CaseState.DENIED))
    await db.commit()

    rows, total = await repo.list_cases(db, state=CaseState.APPROVED)
    assert total == 1
    assert rows[0].state == CaseState.APPROVED


@pytest.mark.asyncio
async def test_list_cases_states_takes_precedence_over_state(db):
    """When both `state` and `states` are passed, `states` wins."""
    db.add(create_test_case(state=CaseState.APPROVED))
    db.add(create_test_case(state=CaseState.DENIED))
    await db.commit()

    rows, total = await repo.list_cases(
        db,
        state=CaseState.APPROVED,                          # would match 1
        states=[CaseState.APPROVED, CaseState.DENIED],     # matches 2
    )
    assert total == 2  # `states` won


@pytest.mark.asyncio
async def test_list_cases_order_by_recent_uses_updated_at_desc(db):
    """`order_by_recent=True` sorts by COALESCE(submitted_at, updated_at,
    ingested_at) DESC. The most-recently-touched row comes first."""
    now = datetime.utcnow()
    older = create_test_case(state=CaseState.APPROVED, last_name="Older")
    newer = create_test_case(state=CaseState.APPROVED, last_name="Newer")
    db.add(older); db.add(newer)
    await db.commit()
    # Manually set updated_at to make the ordering deterministic — the
    # in-memory factory fields default close together in time.
    older.updated_at = now - timedelta(hours=2)
    newer.updated_at = now
    await db.commit()

    rows, total = await repo.list_cases(
        db,
        states=[CaseState.APPROVED],
        order_by_recent=True,
    )
    assert total == 2
    # Newest first
    assert rows[0].last_name == "Newer"
    assert rows[1].last_name == "Older"


@pytest.mark.asyncio
async def test_list_cases_default_order_is_priority(db):
    """Default ordering (no `order_by_recent`) keeps sort_priority ASC —
    the active queue's existing semantics (reps process by priority)."""
    high = create_test_case(state=CaseState.PROCESSING, is_stat=True,
                            last_name="StatPatient")  # sort_priority=1
    low = create_test_case(state=CaseState.PROCESSING, is_stat=False,
                           last_name="NormalPatient")  # sort_priority=3
    db.add(low); db.add(high)
    await db.commit()

    rows, total = await repo.list_cases(
        db,
        states=[CaseState.PROCESSING],
    )
    assert total == 2
    # STAT (priority 1) sorts before normal (priority 3)
    assert rows[0].last_name == "StatPatient"
