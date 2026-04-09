"""RAG Retrieval — pgvector similarity search on outcome patterns.

Day 1: empty table, returns nothing. LLM works without RAG examples.
RAG enriches over time as outcomes accumulate.
"""
from __future__ import annotations

import logging

from openai import AsyncOpenAI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import settings
from app.db.models import OutcomePattern

logger = logging.getLogger(__name__)

_openai_client: AsyncOpenAI | None = None


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _openai_client


async def get_embedding(text_input: str) -> list[float]:
    """Generate embedding using text-embedding-3-small (1536 dims)."""
    client = _get_openai_client()
    response = await client.embeddings.create(
        model="text-embedding-3-small",
        input=text_input,
    )
    return response.data[0].embedding


async def retrieve_similar_cases(
    question_text: str,
    cpt_code: str,
    carrier_id: str | None,
    db: AsyncSession,
    top_k: int = 5,
    min_similarity: float = 0.75,
) -> list[dict]:
    """Retrieve similar outcome patterns using pgvector ANN search.

    Pre-filters by cpt_code + carrier_id before vector search.
    Returns list of dicts with outcome details.
    """
    # Get embedding for the question
    embedding = await get_embedding(question_text)

    # Build query with pre-filter + vector similarity
    # pgvector cosine distance: 1 - (a <=> b) gives similarity
    filters = ["cpt_code = :cpt_code"]
    params = {"cpt_code": cpt_code, "top_k": top_k, "min_sim": min_similarity}

    if carrier_id:
        filters.append("carrier_id = :carrier_id")
        params["carrier_id"] = carrier_id

    filter_clause = " AND ".join(filters)

    query = text(f"""
        SELECT
            id, question_text, answer_text, evidence_text, outcome,
            denial_reason, was_rep_override,
            1 - (question_embedding <=> :embedding::vector) as similarity
        FROM outcome_patterns
        WHERE {filter_clause}
          AND 1 - (question_embedding <=> :embedding::vector) >= :min_sim
        ORDER BY question_embedding <=> :embedding::vector
        LIMIT :top_k
    """)

    params["embedding"] = str(embedding)

    result = await db.execute(query, params)
    rows = result.fetchall()

    return [
        {
            "id": row.id,
            "question_text": row.question_text,
            "answer_text": row.answer_text,
            "evidence_text": row.evidence_text,
            "outcome": row.outcome,
            "denial_reason": row.denial_reason,
            "was_rep_override": row.was_rep_override,
            "similarity": round(row.similarity, 3),
        }
        for row in rows
    ]


def format_rag_examples(cases: list[dict]) -> str:
    """Format RAG results as readable text for prompt injection."""
    if not cases:
        return ""

    lines = []
    for i, case in enumerate(cases, 1):
        outcome_label = case["outcome"]
        if case.get("denial_reason"):
            outcome_label += f" (reason: {case['denial_reason']})"

        override = " [rep override]" if case.get("was_rep_override") else ""

        lines.append(
            f"Example {i} ({case['similarity']:.0%} similar, {outcome_label}{override}):\n"
            f"  Q: {case['question_text'][:100]}\n"
            f"  A: {case['answer_text']}\n"
            f"  Evidence: {(case.get('evidence_text') or 'none')[:100]}"
        )

    return "\n\n".join(lines)
