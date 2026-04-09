"""Completeness Gate — pure Python business rule check before portal submission.

Runs AFTER LLM answers all clinical questions, BEFORE finalize.
Catches low-confidence chains and missing evidence that would cause pends.

No LLM calls here — just counting and thresholds.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Thresholds
LOW_CONFIDENCE_THRESHOLD = 50  # Below this = flagged
MAX_LOW_CONFIDENCE_ALLOWED = 2  # More than this = halt


def check_completeness(
    answers: list[dict],
    clinical_context: dict | None = None,
) -> dict:
    """Gate between LLM answers and portal finalization.

    Args:
        answers: List of TypedDecision-like dicts from the answer loop.
            Each has: confidence, evidence, gap, reasoning, answer_value
        clinical_context: The clinical extraction dict (to check if notes exist)

    Returns:
        {
            passed: bool,
            has_clinical_notes: bool,
            total_answers: int,
            low_confidence_count: int,
            no_evidence_count: int,
            gaps: [str],
            halt_reason: str | None,
        }
    """
    has_notes = bool(clinical_context) and not clinical_context.get("error")

    low_conf = []
    no_evidence = []
    gaps = []

    for i, answer in enumerate(answers):
        conf = answer.get("confidence", 0)
        evidence = answer.get("evidence")
        gap = answer.get("gap")
        q_text = answer.get("question_text", f"Q{i+1}")

        if conf < LOW_CONFIDENCE_THRESHOLD:
            low_conf.append(q_text)

        if not evidence:
            no_evidence.append(q_text)

        if gap:
            gaps.append(gap)

    # Decision logic
    halt_reason = None
    passed = True

    if not has_notes and len(answers) > 0:
        # No clinical notes at all — can't ground any answers
        halt_reason = "No clinical documentation available — all answers are ungrounded"
        passed = False
    elif len(low_conf) > MAX_LOW_CONFIDENCE_ALLOWED:
        halt_reason = (
            f"{len(low_conf)} answers below {LOW_CONFIDENCE_THRESHOLD}% confidence "
            f"(max allowed: {MAX_LOW_CONFIDENCE_ALLOWED})"
        )
        passed = False

    result = {
        "passed": passed,
        "has_clinical_notes": has_notes,
        "total_answers": len(answers),
        "low_confidence_count": len(low_conf),
        "no_evidence_count": len(no_evidence),
        "gaps": gaps,
        "halt_reason": halt_reason,
    }

    logger.info(
        f"Completeness gate: {'PASSED' if passed else 'HALTED'} — "
        f"{len(answers)} answers, {len(low_conf)} low-conf, "
        f"{len(no_evidence)} no-evidence, notes={'yes' if has_notes else 'no'}"
    )

    return result
