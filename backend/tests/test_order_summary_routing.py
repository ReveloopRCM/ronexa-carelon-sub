"""Tests for v157 Order Summary view detection in extract_eligibility_details.

Why this contract is worth pinning:
  - Carelon's portal can skip the standard eligibility page entirely
    and redirect to a standalone Order Summary view (rendered by the
    PrintActivity ASP.NET user control). This view has the
    `#PrintActivity_ctl00_lblIneligible` span with one of two messages:
      • physician-call: "treating physician about initiating" / "Carelon
        Order number may be required"
      • true no-auth: same span but with a different phrasing
  - Pre-v157, extract_eligibility_details waited 30s for `Effective`
    text (which doesn't exist on Order Summary), then returned
    di_requires_auth=True by default, and the compiler cascaded to a
    "Select DI failed" HOLD that masqueraded as a portal flake.
  - v157 reads the lblIneligible span FIRST (before the 30s wait) and
    returns a `page_type=order_summary` short-circuit. Tests verify:
      1. Physician-call text routes correctly.
      2. True-no-auth text on this view routes to NO_AUTH (not physician).
      3. Missing span falls through to the regular eligibility path.
      4. Page.evaluate exception doesn't crash the extractor.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.portal.webforms_client import WebFormsClient


def _make_client(pre_check_eval, eligibility_eval=None) -> WebFormsClient:
    """Build a WebFormsClient whose page.evaluate returns one value per call.

    First call → pre_check_eval (the v157 lblIneligible read).
    Subsequent calls → eligibility_eval (the regular extractor JS), if reached.
    Bypass real Playwright by skipping __init__.
    """
    session = MagicMock()
    page = MagicMock()
    side_effects = [pre_check_eval]
    if eligibility_eval is not None:
        side_effects.append(eligibility_eval)
    page.evaluate = AsyncMock(side_effect=side_effects)
    page.wait_for_selector = AsyncMock(return_value=None)
    page.wait_for_load_state = AsyncMock(return_value=None)
    session.page = page
    client = WebFormsClient.__new__(WebFormsClient)
    client.session = session
    client.page = page
    return client


# ─────────────────────────────────────────────────────────────────────
# Physician-call short-circuit
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_order_summary_physician_call_short_circuits():
    """Order Summary view with physician-call text → v157 returns
    page_type=order_summary + physician_initiation_required=True BEFORE
    the 30s `Effective` wait."""
    text = (
        "A Carelon Order number may be required for this member. "
        "Please contact the treating physician about initiating the "
        "Carelon Order Request process."
    )
    client = _make_client(pre_check_eval=text)
    result = await client.extract_eligibility_details()

    assert result["ok"] is True
    assert result["data"]["page_type"] == "order_summary"
    assert result["data"]["physician_initiation_required"] is True
    assert result["data"]["di_requires_auth"] is False
    assert "treating physician about initiating" in result["data"]["ineligible_text"].lower()
    # Critical: page.wait_for_selector for "Effective" must NOT have been
    # called — we short-circuited before that 30s wait.
    client.page.wait_for_selector.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# True no-auth via Order Summary (no physician-call)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_order_summary_true_no_auth_short_circuits():
    """Order Summary view with non-physician text → still short-circuits,
    but physician_initiation_required=False so the compiler routes to
    NO_AUTH_REQUIRED rather than the Call Worklist."""
    text = "DI does not require pre-authorization for this member's plan."
    client = _make_client(pre_check_eval=text)
    result = await client.extract_eligibility_details()

    assert result["data"]["page_type"] == "order_summary"
    assert result["data"]["physician_initiation_required"] is False
    assert result["data"]["di_requires_auth"] is False
    client.page.wait_for_selector.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Fall-through: regular eligibility page (no lblIneligible)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_order_summary_falls_through_to_eligibility_path():
    """When lblIneligible is absent, the pre-check returns None and the
    extractor proceeds to the regular eligibility flow (30s wait, then
    JS section-text extraction)."""
    # First evaluate (pre-check) returns None; second returns eligibility data
    eligibility_data = {
        "effective_date": "01/01/2024",
        "plan_info": {"product": "BCBS"},
        "di_requires_auth": True,
    }
    client = _make_client(pre_check_eval=None, eligibility_eval=eligibility_data)
    result = await client.extract_eligibility_details()

    assert result["ok"] is True
    assert result["data"].get("page_type") != "order_summary"
    assert result["data"]["effective_date"] == "01/01/2024"
    assert result["data"]["di_requires_auth"] is True
    # Confirm the 30s wait was reached (means pre-check fell through correctly)
    client.page.wait_for_selector.assert_called()


# ─────────────────────────────────────────────────────────────────────
# Exception safety
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_check_exception_falls_through_safely():
    """If the pre-check page.evaluate raises (page detached, etc.),
    fall through to the regular eligibility path — don't crash."""
    eligibility_data = {
        "effective_date": "01/01/2024",
        "plan_info": None,
        "di_requires_auth": True,
    }
    # First call raises, second succeeds
    session = MagicMock()
    page = MagicMock()
    page.evaluate = AsyncMock(side_effect=[Exception("page detached"), eligibility_data])
    page.wait_for_selector = AsyncMock(return_value=None)
    page.wait_for_load_state = AsyncMock(return_value=None)
    session.page = page
    client = WebFormsClient.__new__(WebFormsClient)
    client.session = session
    client.page = page

    result = await client.extract_eligibility_details()
    assert result["ok"] is True
    # Fell through to regular path — got eligibility shape, not order_summary
    assert result["data"].get("page_type") != "order_summary"
    assert result["data"]["effective_date"] == "01/01/2024"
