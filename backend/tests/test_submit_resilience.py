"""Tests for SUBMIT-phase resilience improvements (v145).

Two of the three v145 fixes are easily covered by focused unit tests:

  - Fix B: `_extract_confirmation` body-text fallback. When the canonical
    DOM selectors miss (Whittier 17397736 — "Submission completed but no
    confirmation captured" was a regression from commit 3b79b76), the
    method now scans the page body text for outcome keywords. APPROVED
    cases still flow through cleanly via the standard selector path;
    PENDED/DENIED cases that render with non-standard markup get
    recognized via body scan.

The other two fixes (Fix A live-LLM fallback in SUBMIT step-through, and
Fix C hdnAction=6 retry budget) sit deep inside the compiler's
WebForms+Playwright orchestration loop and are awkward to unit-test in
isolation; they're verified via the live deploy verification path.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.portal.webforms_client import WebFormsClient


def _make_client() -> WebFormsClient:
    """Construct a WebFormsClient with mocked Playwright dependencies."""
    session = MagicMock()
    session.page = MagicMock()
    session.page.evaluate = AsyncMock(return_value="")
    client = WebFormsClient(session)
    # Replace reader.read_multiple — that's the canonical-selector layer
    # we're trying to fall back from. Tests configure its return value.
    client.reader = MagicMock()
    client.reader.read_multiple = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_canonical_selectors_populated_no_body_scan():
    """Happy path: all selectors return values; body scan never runs."""
    client = _make_client()
    client.reader.read_multiple.return_value = {
        "order_id": "ORD-123456",
        "status": "Approved",
        "valid_from": "01/01/2026",
        "valid_through": "01/01/2027",
        "health_plan": "Aetna",
    }

    result = await client._extract_confirmation()

    assert result["status"] == "Approved"
    assert result["order_id"] == "ORD-123456"
    # No body-scan markers added when canonical path succeeds
    assert "status_source" not in result
    assert "order_id_source" not in result
    # page.evaluate should NOT have been called — body scan didn't trigger
    assert client.session.page.evaluate.await_count == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_body_scan_recovers_pended_status():
    """Selectors all empty + body contains 'Pended' → status='Pended'.

    This is Whittier 17397736's failure mode. Before Fix B, this case
    would have returned `status=None` → `submission_empty=True` →
    HOLD with "no confirmation captured". After Fix B, the body scan
    recognizes the pended outcome from page text.
    """
    client = _make_client()
    client.reader.read_multiple.return_value = {
        "order_id": None,
        "status": None,
        "valid_from": None,
        "valid_through": None,
        "health_plan": None,
    }
    client.session.page.evaluate.return_value = (
        "Your authorization request has been submitted.\n"
        "Status: Pended for clinical review.\n"
        "Order Number: ORD-78901234"
    )

    result = await client._extract_confirmation()

    assert result["status"] == "Pended"
    assert result["status_source"] == "body_scan"
    # order_id regex picks up the body-text reference
    assert result.get("order_id") == "ORD-78901234"
    assert result["order_id_source"] == "body_scan"


@pytest.mark.asyncio
async def test_body_scan_keyword_priority_denied_beats_review():
    """Keyword priority: 'denied' is more specific than 'review'."""
    client = _make_client()
    client.reader.read_multiple.return_value = {"status": None}
    # Body contains BOTH 'denied' and the substring 'review' — denied wins.
    client.session.page.evaluate.return_value = (
        "This authorization request was denied. "
        "You may request a review through the appeals process."
    )

    result = await client._extract_confirmation()

    assert result["status"] == "Denied"
    assert result["status_source"] == "body_scan"


@pytest.mark.asyncio
async def test_body_scan_no_match_leaves_status_empty():
    """Body has no recognizable outcome keyword → status stays unset.

    The HOLD-on-empty path then fires (per helpers.py:466) — correct
    behavior, since we genuinely don't know the outcome and shouldn't
    fake one (this is what 3b79b76 was protecting against).
    """
    client = _make_client()
    client.reader.read_multiple.return_value = {"status": None}
    client.session.page.evaluate.return_value = (
        "An unexpected error has occurred. Please contact support."
    )

    result = await client._extract_confirmation()

    assert not result.get("status")
    assert result.get("status_source") is None


@pytest.mark.asyncio
async def test_body_scan_resilient_to_evaluate_failure():
    """If page.evaluate raises (browser crashed, page closed, etc.) we
    don't break — we just leave status empty and let the upstream HOLD
    path fire. Better to HOLD than crash the worker."""
    client = _make_client()
    client.reader.read_multiple.return_value = {"status": None}
    client.session.page.evaluate.side_effect = RuntimeError("Page closed")

    # Should not raise
    result = await client._extract_confirmation()

    assert not result.get("status")
