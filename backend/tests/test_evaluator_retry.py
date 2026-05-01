"""Tests for the LLM evaluator retry-with-backoff path.

`decide_answer` wraps `_call_llm` in a 3-attempt exponential-backoff loop
(1s, 2s, 4s — total worst case ~7s). A transient `RuntimeError` from
`_call_llm` (which itself signals "both Anthropic and Gemini failed once
each") should NOT immediately leak `_fallback_decision` into review.

These tests mock `_call_llm` to exercise:
  1. Two failures then success → returns the third-attempt answer.
  2. All three failures → falls back to `_fallback_decision`.
  3. First-attempt success (happy path) → no retry, no warning.

Without this retry, the Elizabeth-Siddons symptom (Q3/Q4 came back as
`ai_reasoning='Fallback: all LLM providers unavailable'`) leaks
unreviewable answers into the rep queue. Single transient blips on
either provider would cascade through the whole question batch.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.intelligence.evaluator import decide_answer
from app.intelligence.models import PortalObservation


# Minimal valid LLM response — must parse as JSON and have answer_value.
_GOOD_LLM_RESPONSE = json.dumps({
    "answer_value": "opt-uuid-1",
    "confidence": 95,
    "evidence": "test-evidence",
    "reasoning": "test-reasoning",
})


def _make_observation() -> PortalObservation:
    return PortalObservation(
        question_id="q1",
        group_id=1,
        question_text="Test question?",
        question_type=3,  # single-select
        options=[{"id": "opt-uuid-1", "text": "Yes"}, {"id": "opt-uuid-2", "text": "No"}],
        cpt_code="73721",
        sequence=0,
        icd1="M25.551",
        carrier_id="CAR1",
    )


@pytest.mark.asyncio
async def test_retry_succeeds_after_two_transient_failures():
    """First two _call_llm calls raise; third succeeds. Should NOT fall back."""
    obs = _make_observation()
    call_count = {"n": 0}

    async def flaky_call_llm(prompt, system):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise RuntimeError(f"All LLM providers failed: anthropic: 503; gemini: 503")
        return _GOOD_LLM_RESPONSE

    with patch("app.intelligence.evaluator._call_llm", side_effect=flaky_call_llm), \
         patch("app.intelligence.evaluator.build_evaluation_prompt", return_value="prompt"), \
         patch("app.intelligence.evaluator.get_evaluation_system_prompt", return_value="sys"), \
         patch("app.intelligence.evaluator.asyncio.sleep", return_value=None):  # speed up test
        result = await decide_answer(obs, clinical_context={})

    assert call_count["n"] == 3, f"expected 3 attempts, got {call_count['n']}"
    assert result.answer_value == "opt-uuid-1", "should return third-attempt answer, not fallback"
    assert result.confidence == 95
    assert result.reasoning != "Fallback: all LLM providers unavailable", \
        "should NOT have hit the fallback path"


@pytest.mark.asyncio
async def test_retry_falls_back_after_three_failures():
    """All three attempts fail. Should return _fallback_decision (first option, conf=0)."""
    obs = _make_observation()
    call_count = {"n": 0}

    async def always_fail(prompt, system):
        call_count["n"] += 1
        raise RuntimeError("All LLM providers failed: anthropic: timeout; gemini: timeout")

    with patch("app.intelligence.evaluator._call_llm", side_effect=always_fail), \
         patch("app.intelligence.evaluator.build_evaluation_prompt", return_value="prompt"), \
         patch("app.intelligence.evaluator.get_evaluation_system_prompt", return_value="sys"), \
         patch("app.intelligence.evaluator.asyncio.sleep", return_value=None):
        result = await decide_answer(obs, clinical_context={})

    assert call_count["n"] == 3, f"expected exactly 3 attempts, got {call_count['n']}"
    # _fallback_decision picks first option, confidence=0
    assert result.answer_value == "opt-uuid-1"
    assert result.confidence == 0.0
    assert result.reasoning == "Fallback: all LLM providers unavailable"
    assert result.gap == "LLM evaluation unavailable — manual review required"


@pytest.mark.asyncio
async def test_first_attempt_success_does_not_retry():
    """Happy path: first call succeeds. No retries, no extra calls."""
    obs = _make_observation()
    call_count = {"n": 0}

    async def good_call(prompt, system):
        call_count["n"] += 1
        return _GOOD_LLM_RESPONSE

    with patch("app.intelligence.evaluator._call_llm", side_effect=good_call), \
         patch("app.intelligence.evaluator.build_evaluation_prompt", return_value="prompt"), \
         patch("app.intelligence.evaluator.get_evaluation_system_prompt", return_value="sys"):
        result = await decide_answer(obs, clinical_context={})

    assert call_count["n"] == 1, "happy path should call _call_llm exactly once"
    assert result.answer_value == "opt-uuid-1"
    assert result.confidence == 95
