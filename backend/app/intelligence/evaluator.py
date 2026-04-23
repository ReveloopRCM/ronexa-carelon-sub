"""LLM Evaluator — question → TypedDecision with RAG context.

Called once per question group during CLINICAL_TREE loop.
Supports Anthropic (primary) and Google Gemini (fallback).
Model + provider resolved from DB settings via llm_config.

Also provides match_answer() for submission step-through:
  - Tier 1: exact QuestionId match (no LLM, instant)
  - Tier 2: text similarity via Gemini Flash (fallback)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.core.settings import settings
from app.intelligence.models import PortalObservation, TypedDecision
from app.intelligence.prompts import build_evaluation_prompt, get_evaluation_system_prompt

logger = logging.getLogger(__name__)


# ── Temporal guard (narrow safety net for multi-select "features present?" picks) ──
#
# The main fix for "LLM selects Known malignancy from a history-of-melanoma mention"
# lives in the prompt. This guard is belt-and-suspenders: if the LLM still picks a
# positive clinical feature off historical language, demote confidence so the case
# routes to L1 rep review via the existing threshold. We never override the answer
# value — just the confidence — so there is no silent auto-correction.

_CLINICAL_FEATURE_QUESTION_RE = re.compile(
    r"(clinical features|any of the following).{0,120}(present|apply)",
    re.IGNORECASE | re.DOTALL,
)

# Option texts that, when present in the options list, mark a question as a
# clinical-features checklist even if the stem doesn't match the regex above.
_CLINICAL_FEATURE_OPTION_MARKERS: tuple[str, ...] = (
    "known malignancy",
    "tuberculosis",
    "iv drug abuse",
    "fever",
)

# "None" / "unknown" / explicit-negative option text prefixes. If the LLM's selected
# option text starts with any of these, we treat the pick as a truthful negative and
# the guard does nothing.
_NEGATIVE_OPTION_PREFIXES: tuple[str, ...] = (
    "none of these",
    "unknown",
    "no symptoms",
    "neither",
    "no ",  # "No", "No ...", but we already match "none of these" first
)

# Historical-language red flags. Matching any of these against the LLM's reasoning +
# evidence text indicates the evidence for a positive pick is historical.
_TEMPORAL_RED_FLAG_TOKENS: tuple[str, ...] = (
    "history of",
    "h/o ",
    "past medical history",
    "status post",
    "s/p ",
    "previously",
    "resolved",
    "excised",
    "prior ",
    "remote history",
    "denies current",
    "per prior records",
)
_DIAGNOSED_IN_YEAR_RE = re.compile(r"diagnosed\s+in\s+\d{4}", re.IGNORECASE)

# Present-tense confirmers. Any of these in the evidence blob is enough to treat the
# quote as present-tense — covers both explicit "current/active" language and the
# "implicit present" case where a finding lives under a Physical Exam / Assessment /
# Plan / HPI header.
_PRESENT_TENSE_CONFIRMERS: tuple[str, ...] = (
    "current",
    "currently",
    "active",
    "today",
    "on exam",
    "on this visit",
    "ongoing",
    "at present",
    "physical exam",
    "physical examination",
    "assessment:",
    "assessment /",
    "assessment/",
    "plan:",
    "hpi",
    "history of present illness",
)


def _is_clinical_feature_question(observation: PortalObservation) -> bool:
    """Detect a multi-select 'Are any of the following clinical features present?' question."""
    if _CLINICAL_FEATURE_QUESTION_RE.search(observation.question_text or ""):
        return True
    # Fallback: the option set gives it away even when the stem is phrased differently.
    option_texts = " | ".join((o.get("text") or "").lower() for o in (observation.options or []))
    return any(m in option_texts for m in _CLINICAL_FEATURE_OPTION_MARKERS)


def _is_positive_option(option_text: str) -> bool:
    """True when the option text is an actual positive clinical feature (not a negative/unknown)."""
    t = (option_text or "").strip().lower()
    if not t:
        return False
    return not any(t.startswith(p) for p in _NEGATIVE_OPTION_PREFIXES)


def _evidence_blob(result: dict) -> str:
    """Concatenate the LLM's reasoning + evidence fields into a single scan blob."""
    parts: list[str] = []
    if result.get("reasoning"):
        parts.append(str(result["reasoning"]))
    ev = result.get("evidence")
    if isinstance(ev, dict):
        for k in ("explicit", "inferred"):
            v = ev.get(k)
            if v:
                parts.append(str(v))
    elif ev:
        parts.append(str(ev))
    return "\n".join(parts).lower()


def _temporal_guard(observation: PortalObservation, result: dict, answer_value) -> None:
    """Narrow safety net for multi-select clinical-feature questions.

    Demotes `result["confidence"]` to ≤40 and annotates `reasoning` / `approval_gap`
    when a positive feature pick is only supported by historical language. Never
    overrides the answer value.
    """
    if not _is_clinical_feature_question(observation):
        return

    # Build map from option id → option text for the LLM's selected picks.
    opt_by_id = {o.get("id"): o.get("text", "") for o in (observation.options or [])}
    selected_ids = answer_value if isinstance(answer_value, list) else ([answer_value] if answer_value else [])
    positive_texts = [opt_by_id.get(i, "") for i in selected_ids if _is_positive_option(opt_by_id.get(i, ""))]
    if not positive_texts:
        return  # All picks were negative / unknown — nothing to guard against.

    blob = _evidence_blob(result)
    if not blob:
        return

    # Temporal red flags present?
    red_flag_hit = next(
        (tok for tok in _TEMPORAL_RED_FLAG_TOKENS if tok in blob),
        None,
    )
    if red_flag_hit is None and _DIAGNOSED_IN_YEAR_RE.search(blob):
        red_flag_hit = "diagnosed in <year>"

    if red_flag_hit is None:
        return  # No historical language detected — nothing to guard.

    # Present-tense confirmer in the same blob? If yes, the positive pick is
    # likely supported by a current-visit finding (e.g. "Physical Exam: ...") and
    # we don't want to over-correct. BUT: "denies current X" / "no current X" /
    # "not currently X" are negations that *contain* the literal word "current";
    # strip those phrases before checking so their "current" doesn't spuriously
    # satisfy a confirmer.
    confirmer_blob = blob
    for neg_phrase in ("denies current", "no current", "not currently"):
        confirmer_blob = confirmer_blob.replace(neg_phrase, "")
    if any(c in confirmer_blob for c in _PRESENT_TENSE_CONFIRMERS):
        return

    # Demote + annotate.
    orig_conf = float(result.get("confidence") or 0)
    new_conf = min(orig_conf, 40.0)
    result["confidence"] = new_conf

    note = (
        "TEMPORAL GUARD: positive pick rests on historical language "
        f"(matched '{red_flag_hit}') with no present-tense confirmation; "
        "rep verification required."
    )
    existing_reasoning = result.get("reasoning") or ""
    result["reasoning"] = (existing_reasoning + "\n" + note).strip()

    # Only populate approval_gap if empty so we don't clobber a real gap note.
    if not (result.get("approval_gap") or "").strip():
        result["approval_gap"] = note

    logger.info(
        "temporal_guard: demoted conf %.0f→%.0f on Q=%s options=%s token=%r",
        orig_conf,
        new_conf,
        (observation.question_id or "")[:8],
        positive_texts,
        red_flag_hit,
    )


async def _call_anthropic(prompt: str, system: str, model: str, api_key: str) -> str:
    """Call Anthropic Messages API."""
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=model,
        max_tokens=500,
        temperature=0.1,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def _call_gemini(prompt: str, system: str, model: str, api_key: str) -> str:
    """Call Google Gemini via google.genai SDK.

    gemini-2.5-pro uses thinking mode — cannot use response_mime_type.
    Instead we instruct JSON in the prompt and parse from the response text.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    # Build config — NO response_mime_type (incompatible with 2.5-pro thinking)
    config = types.GenerateContentConfig(
        temperature=0.1,
        max_output_tokens=8000,  # Thinking models need budget for thinking + answer
        system_instruction=system + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no code fences, no explanation outside the JSON.",
        safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
        ],
    )

    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=config,
    )

    # Extract text from response — thinking models put text in parts
    text = None

    # Try .text accessor first
    try:
        if response.text:
            text = response.text
    except Exception:
        pass

    # Fallback: extract from candidate parts (required for thinking models)
    if not text:
        try:
            for candidate in (response.candidates or []):
                for part in (candidate.content.parts or []):
                    if hasattr(part, "text") and part.text:
                        # Skip thinking parts — only take the final answer
                        if hasattr(part, "thought") and part.thought:
                            continue
                        text = part.text
                        break
                if text:
                    break
        except Exception:
            pass

    if not text:
        raise ValueError(f"Gemini returned empty response for model={model}")

    # Strip markdown code fences (thinking models wrap JSON in ```json ... ```)
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    return text


async def _call_llm(prompt: str, system: str) -> str:
    """Route to the configured LLM provider. Falls back on failure."""
    from app.intelligence.llm_config import get_llm_config

    errors = []

    # Primary: configured provider
    config = await get_llm_config("eval")
    provider = config["provider"]
    model = config["model"]
    api_key = config["api_key"]

    if api_key:
        try:
            if provider == "anthropic":
                return await _call_anthropic(prompt, system, model, api_key)
            elif provider == "google":
                return await _call_gemini(prompt, system, model, api_key)
        except Exception as e:
            errors.append(f"{provider}: {e}")
            logger.warning(f"{provider} failed: {e}")

    # Fallback: try the other provider
    fallback_provider = "google" if provider == "anthropic" else "anthropic"
    fallback_key_attr = (
        "GOOGLE_API_KEY" if fallback_provider == "google" else "ANTHROPIC_API_KEY"
    )
    fallback_key = getattr(settings, fallback_key_attr, "")
    fallback_model = (
        settings.get_gemini_model() if fallback_provider == "google"
        else settings.get_anthropic_model()
    )

    if fallback_key:
        try:
            if fallback_provider == "google":
                return await _call_gemini(prompt, system, fallback_model, fallback_key)
            else:
                return await _call_anthropic(prompt, system, fallback_model, fallback_key)
        except Exception as e:
            errors.append(f"{fallback_provider}: {e}")
            logger.warning(f"{fallback_provider} failed: {e}")

    raise RuntimeError(f"All LLM providers failed: {'; '.join(errors)}")


async def decide_answer(
    observation: PortalObservation,
    clinical_context: dict,
    multi_select: bool = False,
    rag_examples: str = "",
    rep_answers: list[dict] | None = None,
    changed_group_id: int | None = None,
    pathway_name: str = "",
    previous_answers: list[dict] | None = None,
    order_mode: bool = False,
    signature_answers: list[dict] | None = None,
) -> TypedDecision:
    """Evaluate a portal question using LLM and return a TypedDecision.

    For reruns, rep_answers contains the auth rep's approved answers
    up to changed_group_id. The LLM trusts these and selects them.
    For questions beyond changed_group_id (or first pass with no rep_answers),
    the LLM evaluates fresh from clinical documentation.

    Args:
        order_mode: If True, use order-specific prompt templates. The
                    clinical_context contains extracted order form data.
    """

    # build_evaluation_prompt is now async (reads template from DB)
    prompt = await build_evaluation_prompt(
        observation={
            "question_text": observation.question_text,
            "question_type": observation.question_type,
            "options": observation.options,
            "cpt_code": observation.cpt_code,
            "icd1": observation.icd1,
            "carrier_id": observation.carrier_id,
            "group_id": observation.group_id,
        },
        clinical_context=clinical_context,
        rag_examples=rag_examples,
        multi_select=multi_select,
        rep_answers=rep_answers,
        changed_group_id=changed_group_id,
        pathway_name=pathway_name,
        previous_answers=previous_answers,
        order_mode=order_mode,
        signature_answers=signature_answers,
    )

    # System prompt also from DB
    system = await get_evaluation_system_prompt(order_mode=order_mode)

    try:
        raw_text = await _call_llm(prompt, system)
    except RuntimeError as e:
        logger.error(f"All LLM providers failed: {e}")
        return _fallback_decision(observation)

    # Strip markdown fences if present
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3].strip()

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse evaluator response: {raw_text[:200]}")
        return _fallback_decision(observation)

    answer_value = result.get("answer_value")

    # LLM may return null/empty — fall back to first option
    is_empty = answer_value is None or answer_value == "" or answer_value == []
    if is_empty and observation.options:
        first_opt = observation.options[0]["id"]
        answer_value = [first_opt] if multi_select else first_opt
        logger.warning(
            f"LLM returned empty answer — defaulting to first option: "
            f"{observation.options[0].get('text', first_opt)}"
        )
        result["gap"] = result.get("gap") or "LLM could not determine answer from available context"

    if multi_select and isinstance(answer_value, str):
        answer_value = [answer_value]

    # Parse notes_answer_value for multi-select consistency
    notes_val = result.get("notes_answer_value")
    if multi_select and isinstance(notes_val, str):
        notes_val = [notes_val]

    # Temporal guard — narrow safety net for multi-select "features present?" questions
    # where the LLM still picks a positive from historical language. Mutates
    # result["confidence"] / result["reasoning"] / result["approval_gap"] in place; does
    # not override answer_value. See _temporal_guard() docstring for scope + rules.
    _temporal_guard(observation, result, answer_value)

    # Parse evidence — v2 schema returns {explicit, inferred} object; serialize to string
    evidence_raw = result.get("evidence")
    if isinstance(evidence_raw, dict):
        evidence_str = json.dumps(evidence_raw)
    else:
        evidence_str = evidence_raw

    return TypedDecision(
        question_id=observation.question_id,
        question_type=observation.question_type,
        group_id=observation.group_id,
        sequence=observation.sequence,
        answer_value=answer_value,
        confidence=float(result.get("confidence", 0)),
        evidence=evidence_str,
        gap=result.get("gap"),  # legacy — v2 schema uses pathway_rationale instead
        reasoning=result.get("reasoning"),
        # Pathway + chain-of-thought (v2 prompts)
        pathway_rationale=result.get("pathway_rationale"),
        chain_coherence=result.get("chain_coherence"),
        # Dual-answer: notes path
        notes_answer_value=notes_val,
        notes_confidence=float(result.get("notes_confidence", 0)) if result.get("notes_confidence") is not None else None,
        notes_reasoning=result.get("notes_reasoning"),
        approval_gap=result.get("approval_gap"),
    )


def _fallback_decision(observation: PortalObservation) -> TypedDecision:
    """Generate a low-confidence decision when all LLMs are unavailable."""
    first_option = observation.options[0]["id"] if observation.options else ""
    return TypedDecision(
        question_id=observation.question_id,
        question_type=observation.question_type,
        group_id=observation.group_id,
        sequence=observation.sequence,
        answer_value=[first_option] if observation.question_type == 4 else first_option,
        confidence=0.0,
        evidence=None,
        gap="LLM evaluation unavailable — manual review required",
        reasoning="Fallback: all LLM providers unavailable",
    )


# ── Submission Answer Matching ──


@dataclass
class MatchResult:
    """Result of matching an incoming portal question to a saved answer."""
    matched: bool
    saved_answer: dict | None = None
    match_type: str = "no_match"    # "exact_id", "text_match", "no_match"
    confidence: float = 0.0
    reasoning: str | None = None


async def _call_match_llm(prompt: str, system: str) -> str:
    """Call the match LLM (Gemini Flash). No fallback — failure = HOLD."""
    from app.intelligence.llm_config import get_llm_config

    config = await get_llm_config("match")
    provider = config["provider"]
    model = config["model"]
    api_key = config["api_key"]

    if not api_key:
        raise RuntimeError(f"No API key for match LLM (provider={provider})")

    if provider == "google":
        return await _call_gemini(prompt, system, model, api_key)
    elif provider == "anthropic":
        return await _call_anthropic(prompt, system, model, api_key)
    else:
        raise RuntimeError(f"Unknown match LLM provider: {provider}")


async def match_answer(
    question_id: str,
    question_text: str,
    question_type: int,
    options: list[dict],
    saved_answers: list[dict],
) -> MatchResult:
    """Match an incoming portal question to the closest saved answer.

    Two-tier strategy:
      Tier 1: Exact QuestionId match (no LLM, instant) — the common case
      Tier 2: Text similarity via Gemini Flash (fallback for changed IDs)

    Returns MatchResult with matched=False if no match found → caller HOLDs case.
    """
    if not saved_answers:
        return MatchResult(matched=False, reasoning="No saved answers to match against")

    # ── Tier 1: Exact QuestionId match (no LLM) ──
    for saved in saved_answers:
        saved_qid = saved.get("QuestionId") or saved.get("question_id", "")
        if saved_qid and saved_qid == question_id:
            return MatchResult(
                matched=True,
                saved_answer=saved,
                match_type="exact_id",
                confidence=100.0,
                reasoning=f"Exact QuestionId match: {question_id[:12]}...",
            )

    # ── Tier 2: Text similarity via Gemini Flash ──
    from app.intelligence.prompts import build_match_prompt, get_match_system_prompt

    try:
        prompt = await build_match_prompt(
            question_id=question_id,
            question_text=question_text,
            question_type=question_type,
            options=options,
            saved_answers=saved_answers,
        )
        system = await get_match_system_prompt()
        raw_text = await _call_match_llm(prompt, system)

        # Strip markdown fences
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3].strip()

        result = json.loads(raw_text)
        matched_index = result.get("matched_index")
        confidence = float(result.get("confidence", 0))
        reasoning = result.get("reasoning", "")

        if matched_index is not None and confidence >= 70:
            idx = int(matched_index)
            if 0 <= idx < len(saved_answers):
                return MatchResult(
                    matched=True,
                    saved_answer=saved_answers[idx],
                    match_type="text_match",
                    confidence=confidence,
                    reasoning=reasoning,
                )

        # Below threshold or invalid index
        return MatchResult(
            matched=False,
            match_type="no_match",
            confidence=confidence,
            reasoning=reasoning or f"LLM confidence {confidence} below threshold 70",
        )

    except json.JSONDecodeError as e:
        logger.error(f"Match LLM returned invalid JSON: {e}")
        return MatchResult(matched=False, reasoning=f"Match LLM JSON parse error: {e}")
    except Exception as e:
        logger.error(f"Match LLM failed: {e}")
        return MatchResult(matched=False, reasoning=f"Match LLM error: {e}")
