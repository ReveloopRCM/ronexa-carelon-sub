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


def _early_return_tuple_from(module) -> set[str]:
    """Pull the early-return status tuple out of the module's source.

    Greps the source for `status in (...)` and parses out the literals.
    Brittle, but the point is to *catch a code change* — if someone rewrites
    the check shape, this test will (correctly) fail and force them to
    update the test along with the code.
    """
    src = inspect.getsource(module)
    # Find the tuple literal in `status in ("...", "...", ...)`
    import re
    match = re.search(
        r'status\s+in\s+\(\s*((?:"[^"]+"\s*,?\s*)+)\)',
        src,
    )
    assert match, (
        f"Could not find `status in (...)` early-return guard in "
        f"{module.__name__}. Did the structure change?"
    )
    literals = re.findall(r'"([^"]+)"', match.group(1))
    return set(literals)


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
