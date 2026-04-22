"""Backfill: re-embed all outcome_patterns rows with the current embedding
provider (from system_settings.llm_embed_provider / llm_embed_model).

Run this after switching embedding providers so that historical rows
match the semantic space of new queries. Safe to re-run and safe to
interrupt — commits every ~50 rows.

Usage:
    docker exec -w /app backend-api python3 scripts/reembed_outcome_patterns.py

Options (env vars):
    REEMBED_BATCH_SIZE      — commit frequency (default 50)
    REEMBED_DRY_RUN=1       — skip DB writes, just log what would change
    REEMBED_INCLUDE_NULL=1  — also embed rows whose question_embedding is NULL
                              (default: only rows that already have an
                              embedding, i.e. we're REPLACING stale vectors)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

# Make /app/app importable when run from /app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from app.db.database import async_session_factory
from app.db.models import OutcomePattern
from app.intelligence.rag import get_embedding


BATCH_SIZE = int(os.environ.get("REEMBED_BATCH_SIZE", "50"))
DRY_RUN = os.environ.get("REEMBED_DRY_RUN") == "1"
INCLUDE_NULL = os.environ.get("REEMBED_INCLUDE_NULL") == "1"


async def main() -> None:
    async with async_session_factory() as db:
        query = select(OutcomePattern)
        if not INCLUDE_NULL:
            query = query.where(OutcomePattern.question_embedding.isnot(None))
        query = query.order_by(OutcomePattern.id)

        result = await db.execute(query)
        rows = result.scalars().all()
        total = len(rows)

        print(f"[reembed] {total} outcome_pattern rows to process "
              f"(dry_run={DRY_RUN}, include_null={INCLUDE_NULL})")
        if not total:
            return

        done = 0
        skipped = 0
        failed = 0
        started = time.time()

        for i, row in enumerate(rows, start=1):
            qt = (row.question_text or "").strip()
            if not qt:
                skipped += 1
                continue

            try:
                vec = await get_embedding(qt)
            except Exception as e:
                failed += 1
                print(f"  [FAIL] row={row.id} error={type(e).__name__}: {str(e)[:140]}")
                continue

            if not DRY_RUN:
                row.question_embedding = vec
            done += 1

            if i % BATCH_SIZE == 0:
                if not DRY_RUN:
                    await db.commit()
                elapsed = time.time() - started
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / rate if rate > 0 else 0
                print(f"  progress: {i}/{total} "
                      f"(done={done} skipped={skipped} failed={failed}) "
                      f"rate={rate:.1f}/s eta={eta:.0f}s")

        if not DRY_RUN:
            await db.commit()

        elapsed = time.time() - started
        print(f"[reembed] complete in {elapsed:.1f}s — "
              f"done={done} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    asyncio.run(main())
