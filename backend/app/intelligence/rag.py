"""RAG Retrieval — pgvector similarity search on outcome patterns.

Day 1: empty table, returns nothing. LLM works without RAG examples.
RAG enriches over time as outcomes accumulate.

Embeddings are provider-agnostic: configured via `llm_embed_provider`
and `llm_embed_model` in system_settings. Default is Google
`gemini-embedding-001` at `output_dimensionality=1536` so the existing
`outcome_patterns.question_embedding Vector(1536)` column stays valid.
OpenAI is available as a fallback via the Settings UI.
"""
from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OutcomePattern

logger = logging.getLogger(__name__)

# Target dimension for stored embeddings. MUST match the Vector(...) dim
# on outcome_patterns.question_embedding (currently 1536). If you ever
# change the column, update this too and run a full backfill.
EMBED_DIMS = 1536


async def get_embedding(text_input: str) -> list[float]:
    """Generate an embedding vector for `text_input`.

    Provider + model resolved from system_settings via get_llm_config('embed').
    Returns a list of floats of length EMBED_DIMS.
    """
    # Lazy import to avoid a circular import at module load
    from app.intelligence.llm_config import get_llm_config

    cfg = await get_llm_config("embed")
    provider = cfg["provider"]
    model = cfg["model"]
    api_key = cfg["api_key"]

    if not api_key:
        raise RuntimeError(f"No API key configured for embedding provider '{provider}'")

    if provider == "google":
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        resp = await client.aio.models.embed_content(
            model=model,
            contents=text_input,
            config=types.EmbedContentConfig(output_dimensionality=EMBED_DIMS),
        )
        # google-genai returns a list of ContentEmbedding objects
        vec = list(resp.embeddings[0].values)
    elif provider == "openai":
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key)
        resp = await client.embeddings.create(model=model, input=text_input)
        vec = list(resp.data[0].embedding)
    else:
        raise RuntimeError(f"Unknown embedding provider: {provider}")

    if len(vec) != EMBED_DIMS:
        # Defensive: if a future model emits a different dimension, fail loud
        # rather than writing a wrong-shape vector into the DB column.
        raise RuntimeError(
            f"Embedding from {provider}:{model} returned {len(vec)} dims, "
            f"expected {EMBED_DIMS}"
        )
    return vec


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
