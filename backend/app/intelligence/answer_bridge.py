"""Answer Bridge — connects the intelligence layer to the clinical flow.

Converts portal question dicts ↔ PortalObservation/TypedDecision,
providing the `answer_fn` callback that ClinicalExamFlow.run_clinical_questions_loop expects.

Usage:
    from app.intelligence.answer_bridge import build_answer_fn

    answer_fn = build_answer_fn(cpt_code="74178", icd1="K80.20")
    result = await clinical.run_clinical_questions_loop(answer_fn=answer_fn)

    # For signature replay (no clinicals):
    from app.intelligence.answer_bridge import build_signature_replay_fn
    answer_fn = build_signature_replay_fn(signature)
    result = await clinical.run_clinical_questions_loop(answer_fn=answer_fn)
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from app.intelligence.evaluator import decide_answer, match_answer
from app.intelligence.models import PortalObservation, TypedDecision

logger = logging.getLogger(__name__)

# Confidence threshold — below this, log a warning (future: flag for rep review)
LOW_CONFIDENCE_THRESHOLD = 50


def build_answer_fn(
    cpt_code: str,
    icd1: str | None = None,
    carrier_id: str | None = None,
    clinical_context: dict | None = None,
    pathway_name: str = "",
) -> Callable[[list[dict]], Coroutine]:
    """Build an answer_fn callback for run_clinical_questions_loop.

    The returned async function takes portal question dicts and returns
    portal answer dicts — the exact interface clinical_flow expects.

    Args:
        cpt_code: CPT code for this case (e.g. "74178")
        icd1: Primary ICD-10 code (e.g. "K80.20")
        carrier_id: Payer/carrier ID (optional, for RAG filtering)
        clinical_context: Structured extraction from clinical notes (optional)
        pathway_name: Selected clinical pathway name (for chain-of-thought context)
    """
    ctx = clinical_context or {}
    # Accumulate previous answers for chain-of-thought context
    _previous_answers: list[dict] = []

    async def answer_fn(questions: list[dict]) -> list[dict]:
        """Convert portal questions → LLM decisions → portal answers."""
        answers = []

        for q in questions:
            # 1. Convert portal question dict → PortalObservation
            observation = PortalObservation(
                question_id=q["Id"],
                group_id=q.get("GroupId", 0),
                question_text=q.get("Text", ""),
                question_type=q.get("QuestionType", 3),
                options=[
                    {"id": opt["Id"], "text": opt["Text"]}
                    for opt in q.get("Options", [])
                ],
                cpt_code=cpt_code,
                sequence=q.get("Sequence", 0),
                icd1=icd1,
                carrier_id=carrier_id,
            )

            # 2. Retrieve RAG examples (similar past outcomes)
            rag_examples = ""
            try:
                from app.db.database import async_session_factory
                from app.intelligence.rag import retrieve_similar_cases, format_rag_examples

                async with async_session_factory() as db:
                    similar = await retrieve_similar_cases(
                        question_text=observation.question_text,
                        cpt_code=cpt_code,
                        carrier_id=carrier_id,
                        db=db,
                    )
                    rag_examples = format_rag_examples(similar)
                    if rag_examples:
                        logger.info(
                            f"RAG: {len(similar)} similar outcome(s) for: "
                            f"{observation.question_text[:50]}"
                        )
            except Exception as e:
                # Day 1: empty table or no OpenAI key — gracefully degrade
                logger.debug(f"RAG retrieval skipped: {e}")

            # 3. Call LLM evaluator with clinical context + RAG + chain-of-thought
            multi_select = observation.question_type == 4
            decision = await decide_answer(
                observation=observation,
                clinical_context=ctx,
                multi_select=multi_select,
                rag_examples=rag_examples,
                pathway_name=pathway_name,
                previous_answers=_previous_answers if _previous_answers else None,
            )

            # Accumulate for chain-of-thought context on subsequent questions
            answer_text = str(decision.answer_value)
            if isinstance(decision.answer_value, str):
                for o in observation.options:
                    if o["id"] == decision.answer_value:
                        answer_text = o["text"]
                        break
            elif isinstance(decision.answer_value, list):
                texts = []
                for av in decision.answer_value:
                    for o in observation.options:
                        if o["id"] == av:
                            texts.append(o["text"])
                            break
                if texts:
                    answer_text = "; ".join(texts)
            _previous_answers.append({
                "question_number": len(_previous_answers) + 1,
                "question_text": observation.question_text,
                "answer_text": answer_text,
                "reasoning": decision.reasoning or "",
            })

            # 3. Log decision quality
            conf_label = f"{decision.confidence:.0f}%"
            if decision.confidence < LOW_CONFIDENCE_THRESHOLD:
                logger.warning(
                    f"LOW CONFIDENCE ({conf_label}) — Q: {observation.question_text[:80]} "
                    f"| A: {decision.answer_value} | Gap: {decision.gap}"
                )
            else:
                logger.info(
                    f"Decision ({conf_label}) — Q: {observation.question_text[:60]} "
                    f"| A: {decision.answer_value} | Reasoning: {decision.reasoning}"
                )

            # 4. Enrich the original question dict with decision metadata
            #    (so all_questions carries confidence/evidence for completeness gate)
            q["ai_confidence"] = decision.confidence
            q["ai_evidence"] = decision.evidence
            q["ai_reasoning"] = decision.reasoning
            q["ai_gap"] = decision.gap
            q["ai_answer"] = decision.answer_value

            # 5. Convert TypedDecision → portal answer dict
            answers.append(decision.to_portal_answer())

        logger.info(f"Answered {len(answers)} question(s)")
        return answers

    return answer_fn


def build_signature_replay_fn(
    signature: Any,  # AlgorithmSignature model instance
) -> Callable[[list[dict]], Coroutine]:
    """Build an answer_fn that replays a known-good algorithm signature.

    Instead of calling the LLM evaluator, this replays saved answers from a
    previous algorithm-approved case with the same CPT+ICD+Pathway combo.

    Matching strategy:
      Tier 1: Exact QuestionId match (instant, no LLM)
      Tier 2: Text similarity via Gemini Flash (existing match_answer)

    The portal algorithm is deterministic — same answers → same outcome.
    Rep verifies against actual clinicals when they arrive.

    Args:
        signature: AlgorithmSignature with qa_sequence list
    """
    qa_sequence = signature.qa_sequence or []
    _unmatched_count = 0

    # Pre-build saved_answers list in the format match_answer expects
    saved_answers = []
    for qa in qa_sequence:
        saved_answers.append({
            "QuestionId": qa.get("question_id", ""),
            "question_id": qa.get("question_id", ""),
            "Text": qa.get("question_text", ""),
            "QuestionType": qa.get("question_type", 3),
            "Options": qa.get("options", []),
            "answer_value": qa.get("answer_value"),
            "answer_text": qa.get("answer_text", ""),
            "GroupId": qa.get("group_id", 0),
            "Sequence": qa.get("sequence", 0),
        })

    async def answer_fn(questions: list[dict]) -> list[dict]:
        """Replay saved answers for each portal question."""
        nonlocal _unmatched_count
        answers = []

        for q in questions:
            question_id = q["Id"]
            question_text = q.get("Text", "")
            question_type = q.get("QuestionType", 3)
            options = q.get("Options", [])

            # Use the existing match_answer (Tier 1: exact ID, Tier 2: Gemini Flash)
            result = await match_answer(
                question_id=question_id,
                question_text=question_text,
                question_type=question_type,
                options=[{"id": o["Id"], "text": o["Text"]} for o in options],
                saved_answers=saved_answers,
            )

            if result.matched and result.saved_answer:
                # Replay the saved answer
                saved = result.saved_answer
                answer_value = saved.get("answer_value")

                # Convert answer_value to Values list for portal
                if isinstance(answer_value, list):
                    values = answer_value
                elif isinstance(answer_value, dict):
                    # Dict answer — extract Value key
                    val = answer_value.get("Value", answer_value)
                    values = val if isinstance(val, list) else [str(val)] if val else []
                elif answer_value:
                    values = [str(answer_value)]
                else:
                    values = []

                portal_answer = {
                    "QuestionId": question_id,
                    "QuestionType": question_type,
                    "GroupId": q.get("GroupId", 0),
                    "Sequence": q.get("Sequence", 0),
                    "Values": values,
                }

                # Enrich question dict with replay metadata
                q["ai_confidence"] = result.confidence
                q["ai_evidence"] = f"Signature replay ({result.match_type})"
                q["ai_reasoning"] = f"Replayed from signature {signature.id[:8]} — {result.reasoning}"
                q["ai_gap"] = None
                q["ai_answer"] = answer_value
                q["signature_replay"] = True

                logger.info(
                    f"Signature replay ({result.match_type}, {result.confidence:.0f}%) — "
                    f"Q: {question_text[:60]} | A: {saved.get('answer_text', '')[:40]}"
                )
                answers.append(portal_answer)

            else:
                # No match — use first option as fallback (portal needs a value)
                _unmatched_count += 1
                fallback_value = options[0]["Id"] if options else ""

                portal_answer = {
                    "QuestionId": question_id,
                    "QuestionType": question_type,
                    "GroupId": q.get("GroupId", 0),
                    "Sequence": q.get("Sequence", 0),
                    "Values": [fallback_value] if fallback_value else [],
                }

                q["ai_confidence"] = 0
                q["ai_evidence"] = "Signature replay — NO MATCH (fallback)"
                q["ai_reasoning"] = f"No matching Q&A in signature. Reason: {result.reasoning}"
                q["ai_gap"] = "Question not in signature — needs rep review"
                q["ai_answer"] = fallback_value
                q["signature_replay"] = True
                q["signature_gap"] = True

                logger.warning(
                    f"Signature replay MISS — Q: {question_text[:60]} | "
                    f"Using fallback. Total misses: {_unmatched_count}"
                )
                answers.append(portal_answer)

        logger.info(f"Signature replay answered {len(answers)} question(s), {_unmatched_count} total misses")
        return answers

    return answer_fn
