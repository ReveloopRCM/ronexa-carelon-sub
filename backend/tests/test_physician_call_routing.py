"""Tests for v155 PHYSICIAN_CALL_REQUIRED routing.

Why this is a contract worth testing:
  - Carelon reuses the same #*_lblIneligible element on multiple pages
    with different text per outcome. The ONLY signal that distinguishes
    "rep must call physician" from "true no-auth, case is done" is the
    span TEXT. Misclassifying either way is bad:
      a) Routing a true-no-auth case to PHYSICIAN_CALL_REQUIRED → rep
         wastes a phone call on a done case.
      b) Routing a physician-call case to NO_AUTH_REQUIRED → case hides
         in Completed; physician never gets called; member's auth
         languishes (Ian Lawler 15517158 / 17578976).
  - Three patterns matter:
      • Physician-call phrase → physician_initiation_required=True
      • True no-auth phrases → true_no_auth=True
      • Empty page → present=False (no false positives)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.portal.webforms_client import WebFormsClient


def _make_client(eval_return: str | Exception) -> WebFormsClient:
    """Build a WebFormsClient whose page.evaluate returns the given string
    (or raises the given Exception). Bypasses the real Playwright page +
    session machinery — we're testing classification logic, not DOM."""
    session = MagicMock()
    page = MagicMock()
    if isinstance(eval_return, Exception):
        page.evaluate = AsyncMock(side_effect=eval_return)
    else:
        page.evaluate = AsyncMock(return_value=eval_return)
    session.page = page
    client = WebFormsClient.__new__(WebFormsClient)
    client.session = session
    client.page = page
    return client


@pytest.mark.asyncio
async def test_physician_call_phrase_classified_correctly():
    """Carelon's "treating physician about initiating" text → physician_call=True."""
    text = (
        "A Carelon Order number may be required for this member. "
        "Please contact the treating physician about initiating the "
        "Carelon Order Request process."
    )
    client = _make_client(text)
    result = await client.extract_ineligible_message()

    assert result["ok"] is True
    assert result["present"] is True
    assert result["physician_initiation_required"] is True
    assert result["true_no_auth"] is False
    assert "treating physician about initiating" in result["text"].lower()


@pytest.mark.asyncio
async def test_true_no_auth_phrase_classified_correctly():
    """Genuine no-auth text → true_no_auth=True, physician_call=False."""
    text = "DI does not require pre-authorization for this member's plan."
    client = _make_client(text)
    result = await client.extract_ineligible_message()

    assert result["ok"] is True
    assert result["present"] is True
    assert result["physician_initiation_required"] is False
    assert result["true_no_auth"] is True


@pytest.mark.asyncio
async def test_empty_page_not_present():
    """No matching span → present=False (caller falls through to legacy logic)."""
    client = _make_client("")
    result = await client.extract_ineligible_message()

    assert result["ok"] is True
    assert result["present"] is False
    # Should NOT have these keys when present=False — caller must
    # check `present` before reading classification fields.
    assert "physician_initiation_required" not in result
    assert "true_no_auth" not in result


@pytest.mark.asyncio
async def test_physician_call_short_phrase_alone():
    """The shorter "Carelon Order number may be required" variant alone
    still classifies as physician-call (it's the same outcome)."""
    text = "A Carelon Order number may be required for this member."
    client = _make_client(text)
    result = await client.extract_ineligible_message()

    assert result["physician_initiation_required"] is True
    assert result["true_no_auth"] is False


@pytest.mark.asyncio
async def test_ambiguous_text_not_physician_call():
    """A generic 'contact the physician' phrase WITHOUT the 'initiating'
    or 'Carelon Order number' keywords should NOT trigger physician-call
    routing. (Don't false-positive on unrelated portal messages.)"""
    text = "Please contact the physician's office for further information."
    client = _make_client(text)
    result = await client.extract_ineligible_message()

    assert result["present"] is True
    assert result["physician_initiation_required"] is False
    assert result["true_no_auth"] is False


@pytest.mark.asyncio
async def test_page_evaluate_exception_handled_safely():
    """Playwright exception → ok=False, present=False. The caller's
    fall-through path is the existing NO_AUTH logic — safe default."""
    client = _make_client(Exception("page detached"))
    result = await client.extract_ineligible_message()

    assert result["ok"] is False
    assert result["present"] is False
    assert "error" in result


@pytest.mark.asyncio
async def test_physician_call_takes_precedence_over_no_auth_phrases():
    """If the text has BOTH a physician-call phrase AND a no-auth phrase
    (defensive — Carelon's wording can drift), physician-call wins. The
    rep still needs to call; better to route to Call Worklist than to
    silently mark done."""
    text = (
        "A Carelon Order number may be required. Contact the treating "
        "physician about initiating. Note: this DI does not require "
        "pre-authorization from the imaging center directly."
    )
    client = _make_client(text)
    result = await client.extract_ineligible_message()

    assert result["physician_initiation_required"] is True
    assert result["true_no_auth"] is False  # mutually-exclusive guard in extractor
