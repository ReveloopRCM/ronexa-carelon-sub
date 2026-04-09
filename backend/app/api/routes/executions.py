"""Executions API — Billing-oriented pipeline event tracking.

Every pipeline event is logged to execution_logs. This dashboard provides
summary stats for billing: total received, processed (clinical vs order),
approved, pended, submitted. Data is NEVER flushed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case as sql_case, cast, Date, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import ExecutionLog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/executions", tags=["executions"])


@router.get("/summary")
async def execution_summary(
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """Billing summary: totals + daily breakdown for the last N days."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    today = datetime.utcnow().date()
    start_date = today - timedelta(days=days - 1)

    # --- Totals for period ---
    period_filter = ExecutionLog.created_at >= cutoff

    totals = {}
    for event_type in [
        "RECEIVED", "EXTRACTION_CLINICAL", "EXTRACTION_ORDER",
        "FIRST_PASS", "ORDER_PASS", "APPROVED", "PENDED",
        "DENIED", "SUBMITTED", "NO_AUTH",
    ]:
        count = await db.scalar(
            select(func.count(ExecutionLog.id)).where(
                period_filter,
                ExecutionLog.event_type == event_type,
                ExecutionLog.status == "COMPLETED",
            )
        )
        totals[event_type.lower()] = count or 0

    # Derived totals for billing
    billing_totals = {
        "received": totals["received"],
        "processed_clinical": totals["extraction_clinical"] + totals["first_pass"],
        "processed_order": totals["extraction_order"] + totals["order_pass"],
        "approved": totals["approved"],
        "pended": totals["pended"],
        "denied": totals["denied"],
        "submitted": totals["submitted"],
        "no_auth": totals["no_auth"],
    }

    # --- Daily breakdown ---
    daily_rows = await db.execute(
        select(
            cast(ExecutionLog.created_at, Date).label("day"),
            ExecutionLog.event_type,
            func.count(ExecutionLog.id).label("cnt"),
        )
        .where(
            ExecutionLog.created_at >= cutoff,
            ExecutionLog.status == "COMPLETED",
        )
        .group_by("day", ExecutionLog.event_type)
        .order_by(text("day"))
    )

    # Build daily dict
    daily_map: dict[str, dict] = {}
    for row in daily_rows:
        day_str = str(row.day)
        if day_str not in daily_map:
            daily_map[day_str] = {
                "date": day_str,
                "received": 0, "extraction_clinical": 0, "extraction_order": 0,
                "first_pass": 0, "order_pass": 0,
                "approved": 0, "pended": 0, "denied": 0,
                "submitted": 0, "no_auth": 0,
            }
        daily_map[day_str][row.event_type.lower()] = row.cnt

    # Fill in missing days
    daily = []
    for i in range(days):
        d = str(start_date + timedelta(days=i))
        daily.append(daily_map.get(d, {
            "date": d,
            "received": 0, "extraction_clinical": 0, "extraction_order": 0,
            "first_pass": 0, "order_pass": 0,
            "approved": 0, "pended": 0, "denied": 0,
            "submitted": 0, "no_auth": 0,
        }))

    # --- All-time totals ---
    all_time_rows = await db.execute(
        select(
            ExecutionLog.event_type,
            func.count(ExecutionLog.id).label("cnt"),
        )
        .where(ExecutionLog.status == "COMPLETED")
        .group_by(ExecutionLog.event_type)
    )
    all_time = {}
    for row in all_time_rows:
        all_time[row.event_type.lower()] = row.cnt

    return {
        "period": {"from": str(start_date), "to": str(today)},
        "totals": billing_totals,
        "daily": daily,
        "all_time": all_time,
    }


@router.get("/recent")
async def recent_executions(
    limit: int = Query(50, ge=1, le=200),
    event_type: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Recent execution events with denormalized case info."""
    q = select(ExecutionLog).order_by(ExecutionLog.created_at.desc()).limit(limit)
    if event_type:
        q = q.where(ExecutionLog.event_type == event_type.upper())

    result = await db.execute(q)
    logs = result.scalars().all()

    return [
        {
            "id": log.id,
            "case_id": log.case_id,
            "exam_id": log.exam_id,
            "patient_name": log.patient_name,
            "cpt_code": log.cpt_code,
            "center_abbr": log.center_abbr,
            "event_type": log.event_type,
            "document_type": log.document_type,
            "status": log.status,
            "detail": log.detail,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]
