"""OutcomePattern indexing + pgvector queries — RAG data moat."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.db.models import AlgorithmSignature, OutcomePattern, Question
from app.db import repositories as repo
from app.intelligence.rag import get_embedding

logger = logging.getLogger(__name__)


async def index_outcome_patterns(
    case_id: str,
    result: dict,
    db: AsyncSession,
) -> None:
    """Index all questions from a completed case into OutcomePatterns.

    Called after every submission completes. Generates embeddings and writes
    records for RAG retrieval.
    """
    case = await repo.get_case(db, case_id)
    if not case:
        logger.warning(f"Cannot index outcomes: case {case_id} not found")
        return

    questions = await repo.get_questions_for_case(db, case_id)
    if not questions:
        logger.info(f"No questions to index for case {case_id}")
        return

    outcome = case.state.value  # APPROVED/DENIED/PENDED

    for question in questions:
        try:
            embedding = await get_embedding(question.question_text)
        except Exception as e:
            logger.error(f"Embedding failed for question {question.id}: {e}")
            embedding = None

        # Determine final answer (rep override or AI)
        final_answer = question.rep_answer or question.ai_answer
        answer_text = _format_answer_text(final_answer, question.options_json)

        pattern = OutcomePattern(
            case_id=case_id,
            portal_id="carelon_provider_portal",
            cpt_code=case.cpt_code,
            icd1=case.icd1,
            carrier_id=case.carrier_id,
            question_text=question.question_text,
            question_type=question.question_type,
            options_json=question.options_json,
            question_embedding=embedding,
            answer_value=final_answer,
            answer_text=answer_text,
            evidence_text=question.ai_evidence,
            was_rep_override=question.rep_answer is not None,
            outcome=outcome,
            denial_reason=case.denial_reason,
            pathway_id=case.pathway_id,
            pathway_name=case.pathway_name,
            is_gold_card=(case.gold_card_level or 0) >= 2,
        )
        db.add(pattern)

    logger.info(f"Indexed {len(questions)} outcome patterns for case {case_id}")


async def index_algorithm_signature(
    case_id: str,
    result: dict,
    db: AsyncSession,
) -> None:
    """Store the complete Q&A sequence from an algorithm-approved case as a signature.

    Only indexes cases where algorithm_recommendation=3 (Approve).
    Keyed by (cpt_code, icd1, pathway_id) — upserts if a signature already exists
    for the same combo, preferring rep-verified answers over AI-only.

    Called alongside index_outcome_patterns() after submission completion.
    """
    algo_rec = result.get("algorithm_recommendation") or result.get("auto_approved")
    # Only store signatures for algorithm-approved cases (RecommendationType=3)
    if result.get("algorithm_recommendation") != 3 and not result.get("auto_approved"):
        return

    case = await repo.get_case(db, case_id)
    if not case:
        logger.warning(f"Cannot index signature: case {case_id} not found")
        return

    questions = await repo.get_questions_for_case(db, case_id)
    if not questions:
        logger.info(f"No questions to index signature for case {case_id}")
        return

    # Build the ordered Q&A sequence
    qa_sequence = []
    for q in sorted(questions, key=lambda x: (x.group_id, x.sequence)):
        final_answer = q.rep_answer or q.ai_answer
        answer_text = _format_answer_text(final_answer, q.options_json)

        qa_sequence.append({
            "question_id": q.portal_question_id,
            "question_text": q.question_text,
            "question_type": q.question_type,
            "options": q.options_json,
            "answer_value": final_answer,
            "answer_text": answer_text,
            "group_id": q.group_id,
            "sequence": q.sequence,
            "was_rep_override": q.rep_answer is not None,
        })

    if not qa_sequence:
        return

    # Check if a signature already exists for this CPT+ICD+Pathway
    existing = await db.execute(
        select(AlgorithmSignature).where(
            AlgorithmSignature.cpt_code == case.cpt_code,
            AlgorithmSignature.icd1 == case.icd1,
            AlgorithmSignature.pathway_id == case.pathway_id,
        )
    )
    sig = existing.scalar_one_or_none()

    if sig:
        # Update if the new case has rep-verified answers (higher confidence)
        new_has_rep = any(q.get("was_rep_override") for q in qa_sequence)
        old_has_rep = any(q.get("was_rep_override") for q in (sig.qa_sequence or []))

        if new_has_rep and not old_has_rep:
            sig.qa_sequence = qa_sequence
            sig.source_case_id = case_id
            sig.carrier_id = case.carrier_id
            sig.pathway_name = case.pathway_name
            sig.algorithm_recommendation = result.get("algorithm_recommendation", 3)
            logger.info(
                f"Updated algorithm signature for {case.cpt_code}/{case.icd1}/"
                f"{case.pathway_id} with rep-verified answers from case {case_id}"
            )
        else:
            logger.info(
                f"Algorithm signature already exists for {case.cpt_code}/{case.icd1}/"
                f"{case.pathway_id}, keeping existing (source={sig.source_case_id})"
            )
    else:
        # Create new signature
        sig = AlgorithmSignature(
            cpt_code=case.cpt_code,
            icd1=case.icd1,
            carrier_id=case.carrier_id,
            pathway_id=case.pathway_id,
            pathway_name=case.pathway_name,
            qa_sequence=qa_sequence,
            source_case_id=case_id,
            algorithm_recommendation=result.get("algorithm_recommendation", 3),
        )
        db.add(sig)
        logger.info(
            f"Created algorithm signature for {case.cpt_code}/{case.icd1}/"
            f"{case.pathway_id} from case {case_id} ({len(qa_sequence)} Q&As)"
        )


async def get_signature_for_case(
    db: AsyncSession,
    cpt_code: str,
    icd1: str | None,
    pathway_id: str | None = None,
) -> AlgorithmSignature | None:
    """Look up an algorithm signature matching a case's CPT+ICD combo.

    If pathway_id is provided, tries exact match first.
    Falls back to matching just CPT+ICD (any pathway).
    """
    # Try exact match with pathway
    if pathway_id:
        result = await db.execute(
            select(AlgorithmSignature).where(
                AlgorithmSignature.cpt_code == cpt_code,
                AlgorithmSignature.icd1 == icd1,
                AlgorithmSignature.pathway_id == pathway_id,
            )
        )
        sig = result.scalar_one_or_none()
        if sig:
            return sig

    # Fallback: match CPT+ICD, any pathway
    result = await db.execute(
        select(AlgorithmSignature).where(
            AlgorithmSignature.cpt_code == cpt_code,
            AlgorithmSignature.icd1 == icd1,
        ).order_by(AlgorithmSignature.success_count.desc())
    )
    return result.scalars().first()


def _format_answer_text(answer_value, options_json) -> str:
    """Convert answer UUIDs to human-readable text."""
    if not answer_value or not options_json:
        return str(answer_value)

    options_map = {}
    if isinstance(options_json, list):
        for opt in options_json:
            if isinstance(opt, dict):
                options_map[opt.get("id", "")] = opt.get("text", "")

    if isinstance(answer_value, list):
        texts = [options_map.get(v, v) for v in answer_value]
        return "; ".join(texts)
    elif isinstance(answer_value, dict):
        val = answer_value.get("Value", answer_value)
        if isinstance(val, list):
            texts = [options_map.get(v, v) for v in val]
            return "; ".join(texts)
        return options_map.get(str(val), str(val))
    else:
        return options_map.get(str(answer_value), str(answer_value))
