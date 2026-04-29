"""Tests for the (job_type ↔ valid case states) contract.

`backend/app/db/queue.py` defines the canonical per-phase contract used by:
  1. claim_next_job's filter      (which states each job_type can be claimed from)
  2. mark_case_hold's auto_requeue (state to reset to on a transient retry)
  3. reap_stale_processing         (state to reset to after a crash)

Any drift between these three readers silently breaks the system: a
transiently-failed job becomes unreachable to its worker because the case
state doesn't match the claim filter. The job sits QUEUED forever (this
exact failure mode hit Adedeji 73723 and Rivers 73218 on Apr 29 2026 when
helpers.py:198 hardcoded NOTES_UPLOADED for every job_type).

These three tests are cheap, pure-Python invariants that fail loudly if any
contributor ever introduces a new job_type / case state without updating
all three tables together. They run in CI and via `pytest backend/tests/
test_queue_contract.py -v` locally before deploy.
"""
from __future__ import annotations

from app.db.models import JobType
from app.db.queue import (
    PHASE_CLAIM_STATES,
    PHASE_READY_STATE,
    ready_state_for,
)


def test_ready_state_is_claimable_for_every_phase():
    """For every job_type, ready_state_for(t) MUST be in PHASE_CLAIM_STATES[t].

    This is THE invariant the rest of the system relies on. If it ever
    fails, a transiently-failed job becomes unreachable to its worker —
    case state won't match the claim filter, job sits QUEUED forever.
    """
    for job_type, claim_set in PHASE_CLAIM_STATES.items():
        retry_state = ready_state_for(job_type)
        assert retry_state in claim_set, (
            f"BROKEN CONTRACT: ready_state_for({job_type!r}) = "
            f"{retry_state.value!r} is NOT in claim filter "
            f"{[s.value for s in claim_set]}. "
            f"Cases retried for this job_type would become unclaimable."
        )


def test_claim_states_cover_all_job_types():
    """Every JobType enum value MUST have an entry in PHASE_CLAIM_STATES.

    A missing entry means workers of that type cannot claim any case,
    because claim_next_job falls through to the FIRST_PASS default filter.
    Adding a new JobType without updating this table is a silent regression.
    """
    for jt in JobType:
        assert jt.value in PHASE_CLAIM_STATES, (
            f"JobType.{jt.value} missing from PHASE_CLAIM_STATES — "
            f"workers of this type won't be able to claim cases. "
            f"Add an entry to PHASE_CLAIM_STATES (and PHASE_READY_STATE) "
            f"in backend/app/db/queue.py."
        )


def test_ready_state_covers_all_job_types():
    """Same coverage check for PHASE_READY_STATE — paired with claim states.

    Without an entry here, ready_state_for() falls back to NOTES_UPLOADED
    (the safe default), which would silently route SUBMIT/ORDER retries
    to a state that doesn't match their claim filter.
    """
    for jt in JobType:
        assert jt.value in PHASE_READY_STATE, (
            f"JobType.{jt.value} missing from PHASE_READY_STATE — "
            f"transient retries for this job_type would default to "
            f"NOTES_UPLOADED and become unclaimable. Add an entry."
        )


def test_default_falls_back_to_first_pass_safe():
    """Unknown / null job_type must fall back to a FIRST_PASS-claimable state.

    This protects against legacy code paths that pass None or an unknown
    job_type. Better to land in the FIRST_PASS bucket (where a worker
    might still pick it up) than in an unreachable state.
    """
    fallback = ready_state_for(None)
    assert fallback in PHASE_CLAIM_STATES[JobType.FIRST_PASS.value], (
        f"ready_state_for(None) = {fallback.value!r} is not FIRST_PASS-claimable. "
        f"Legacy callers passing None will hit a stuck-case scenario."
    )

    for bogus in ("", "BOGUS", "first_pass", "submit"):  # case-sensitive on purpose
        result = ready_state_for(bogus)
        assert result in PHASE_CLAIM_STATES[JobType.FIRST_PASS.value], (
            f"ready_state_for({bogus!r}) = {result.value!r} not FIRST_PASS-claimable"
        )
