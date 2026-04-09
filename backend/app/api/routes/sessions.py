"""Session pool status endpoints."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
async def list_sessions():
    """Session pool status per NPI."""
    # In production, this reads from Redis
    return {"sessions": [], "message": "Session pool status not yet implemented"}
