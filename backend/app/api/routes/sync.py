"""Mongo sync endpoint — pull new cases from Cosmos DB + fetch clinical PDFs."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sync", tags=["sync"])


@router.post("")
async def sync_from_mongo(
    extract: bool = True,
    limit: int = 500,
    db: AsyncSession = Depends(get_db),
):
    """Pull new Carelon submissions from Mongo → Postgres + fetch clinical PDFs."""
    from app.ingest.sync_engine import run_sync

    return await run_sync(db, extract=extract, limit=limit)
