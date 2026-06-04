"""Tests for v158 — physician_call_required must be an early-return status.

Why this is a contract worth pinning:
  - v155 added the PHYSICIAN_CALL_REQUIRED state, the extractor, the helper,
    and the http_server dispatch branch. Detection worked end-to-end.
  - BUT: case_workflow.run() and order_workflow.run() check the http_server's
    returned status against a tuple `("hold", "error", "no_auth_review")` to
    decide whether to early-return. `"physician_call_required"` wasn't in the
    tuple, so the workflow FELL THROUGH and called save_questions /
    save_order_questions, which transitioned the case state to L1_REVIEW
    (case_workflow) or WAITING_CLINICALS (order_workflow) — overwriting the
    PHYSICIAN_CALL_REQUIRED that the helper had just set.
  - Result in prod: Call Worklist showed zero cases while approval_type
    correctly read "physician_call" on cases sitting in L1_REVIEW.
    (Confirmed today: Timothy Geller 17592843 and Alice Leung 17594447.)
  - Fix is one word per file — add "physician_call_required" to the tuple.
  - This test pins the tuple so a future contributor can't drop it.
"""
from __future__ import annotations

import inspect

import pytest


# ─────────────────────────────────────────────────────────────────────
# The contract: both workflow modules must early-return on these statuses
# ─────────────────────────────────────────────────────────────────────

EARLY_RETURN_STATUSES = {
    "hold",
    "error",
    "no_auth_review",
    "physician_call_required",  # v158 — was missing, caused Call Worklist to stay empty
}


def _all_early_return_tuples_from(module) -> list[set[str]]:
    """Pull ALL early-return status tuples out of the module's source.

    Some workflows (AwaitingClinicalWorkflow) have multiple `status in (...)`
    guards because hold/error reset to PENDING_NOTES while no-auth /
    physician-call just complete the job — different downstream actions,
    same early-return semantics. We want any guard to be inspectable.
    """
    src = inspect.getsource(module)
    import re
    matches = re.findall(
        r'status\s+in\s+\(\s*((?:"[^"]+"\s*,?\s*)+)\)',
        src,
    )
    assert matches, (
        f"Could not find any `status in (...)` early-return guard in "
        f"{module.__name__}. Did the structure change?"
    )
    return [set(re.findall(r'"([^"]+)"', m)) for m in matches]


def _early_return_tuple_from(module) -> set[str]:
    """Union of all early-return tuples in the module — what statuses the
    module collectively early-returns on. Used for the v158 single-guard
    workflows (case_workflow, order_workflow) and just unions correctly
    for the multi-guard AwaitingClinicalWorkflow (v159)."""
    tuples = _all_early_return_tuples_from(module)
    return set().union(*tuples)


def test_case_workflow_early_returns_on_physician_call_required():
    """case_workflow.run() must early-return when http_server says
    physician_call_required — otherwise save_questions runs and
    transitions state to L1_REVIEW, overwriting the PHYSICIAN_CALL_REQUIRED
    set by mark_case_physician_call_required."""
    from app.workflow import case_workflow

    tup = _early_return_tuple_from(case_workflow)
    assert EARLY_RETURN_STATUSES.issubset(tup), (
        f"case_workflow early-return tuple is missing required statuses. "
        f"Found: {sorted(tup)}, Required: {sorted(EARLY_RETURN_STATUSES)}, "
        f"Missing: {sorted(EARLY_RETURN_STATUSES - tup)}"
    )


def test_order_workflow_early_returns_on_physician_call_required():
    """Same contract for OrderWorkflow — without the early return,
    save_order_questions runs and transitions to WAITING_CLINICALS,
    overwriting PHYSICIAN_CALL_REQUIRED."""
    from app.workflow import order_workflow

    tup = _early_return_tuple_from(order_workflow)
    assert EARLY_RETURN_STATUSES.issubset(tup), (
        f"order_workflow early-return tuple is missing required statuses. "
        f"Found: {sorted(tup)}, Required: {sorted(EARLY_RETURN_STATUSES)}, "
        f"Missing: {sorted(EARLY_RETURN_STATUSES - tup)}"
    )


def test_awaiting_clinical_workflow_early_returns_on_physician_call_required():
    """v159 — AwaitingClinicalWorkflow (the re-run path triggered when
    clinical notes arrive) must also early-return on physician_call_required.
    Without this, save_questions / set_clinical_review run downstream and
    overwrite the PHYSICIAN_CALL_REQUIRED state with CLINICAL_REVIEW.

    Also pins the v159 dead-code fix: the workflow used to check for
    `"no_auth_required"` (a string http_server never emits) instead of
    the actual `"no_auth_review"` string. Both must be matched now so
    true-no-auth re-runs stop cleanly too."""
    from app.workflow import awaiting_clinical_workflow

    tup = _early_return_tuple_from(awaiting_clinical_workflow)
    required = EARLY_RETURN_STATUSES | {"no_auth_review"}
    assert required.issubset(tup), (
        f"awaiting_clinical_workflow early-return tuple is missing required "
        f"statuses. Found: {sorted(tup)}, Required: {sorted(required)}, "
        f"Missing: {sorted(required - tup)}"
    )


def test_http_server_emits_physician_call_required_status():
    """The complementary contract — http_server must actually return
    status=physician_call_required from the dispatch branch. If this
    string drifts (e.g. somebody renames it to physician_call_review),
    the workflow guard's match would silently fail."""
    from app.worker import http_server
    src = inspect.getsource(http_server)
    assert '"status": "physician_call_required"' in src, (
        "http_server no longer emits status='physician_call_required'. "
        "Either it was renamed (also update workflow guards) or the "
        "v155 dispatch branch was removed."
    )
