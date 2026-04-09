"""Excel + PDF upload endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.db.database import get_db
from app.db import repositories as repo
from app.db.models import Case, CaseState
from app.ingest.excel_parser import parse_excel

router = APIRouter(prefix="/api/upload", tags=["upload"])


def _serialize_case(c: dict) -> dict:
    """Make case dict JSON-serializable (convert datetimes to ISO strings)."""
    out = {}
    for k, v in c.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


@router.post("/excel")
async def upload_excel(file: UploadFile = File(...)):
    """Parse Excel → return preview (no DB write)."""
    contents = await file.read()
    try:
        result = parse_excel(contents, file.filename or "upload.xlsx")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Return preview — first 50 cases, sorted
    cases_preview = []
    for c in result.cases[:50]:
        cases_preview.append({
            "exam_id": c.get("exam_id"),
            "first_name": c.get("first_name"),
            "last_name": c.get("last_name"),
            "cpt_code": c.get("cpt_code"),
            "icd1": c.get("icd1"),
            "center_abbr": c.get("center_abbr"),
            "state": c.get("state"),
            "sort_priority": c.get("sort_priority"),
            "is_stat": c.get("is_stat"),
            "has_attachment": bool(c.get("attachment_id") and c.get("attachment_id") != "0"),
            "flags": (c.get("raw_data") or {}).get("_flags", []),
        })

    return {
        "filename": result.filename,
        "total_rows": result.total_rows,
        "duplicate_rows": result.duplicate_rows,
        "unique_cases": result.unique_cases,
        "stat_count": result.stat_count,
        "hold_count": result.hold_count,
        "cases_preview": cases_preview,
        "_parsed_cases": [_serialize_case(c) for c in result.cases],
    }


@router.post("/confirm")
async def confirm_upload(
    data: dict,
    db: AsyncSession = Depends(get_db),
):
    """Bulk insert Batch + all Case records after staff confirms preview."""
    cases = data.get("cases", [])
    filename = data.get("filename", "unknown.xlsx")
    uploaded_by = data.get("uploaded_by", "system")

    if not cases:
        return {"error": "No cases to insert"}

    # Create batch
    stat_count = sum(1 for c in cases if c.get("state") == "PENDING_STAT")
    hold_count = sum(1 for c in cases if c.get("state") == "HOLD")

    batch = await repo.create_batch(
        db,
        id=str(uuid.uuid4()),
        filename=filename,
        uploaded_by=uploaded_by,
        total_rows=data.get("total_rows", len(cases)),
        duplicate_rows=data.get("duplicate_rows", 0),
        unique_cases=len(cases),
        stat_count=stat_count,
        hold_count=hold_count,
    )

    # Filter out cases whose exam_id already exists in DB
    exam_ids = [c["exam_id"] for c in cases if c.get("exam_id")]
    existing = set()
    if exam_ids:
        result = await db.execute(
            select(Case.exam_id).where(Case.exam_id.in_(exam_ids))
        )
        existing = {row[0] for row in result.all()}

    new_cases = [c for c in cases if c.get("exam_id") not in existing]
    skipped = len(cases) - len(new_cases)

    # Assign batch_id and convert state strings to enum, parse datetime strings
    for case_dict in new_cases:
        case_dict["batch_id"] = batch.id
        state_str = case_dict.get("state", "PENDING_NOTES")
        case_dict["state"] = CaseState(state_str)

        sched = case_dict.get("scheduled_dt")
        if isinstance(sched, str):
            try:
                case_dict["scheduled_dt"] = datetime.fromisoformat(sched)
            except (ValueError, TypeError):
                case_dict["scheduled_dt"] = None

    if new_cases:
        await repo.create_cases_bulk(db, new_cases)

    # Create audit events for ingestion
    for case_dict in new_cases:
        await repo.create_audit_event(
            db,
            case_id=case_dict["id"],
            actor="system",
            action="ingested",
            data={"batch_id": batch.id, "state": case_dict["state"].value},
        )

    await db.commit()

    return {
        "batch_id": batch.id,
        "unique_cases": len(new_cases),
        "skipped_duplicates": skipped,
        "stat_count": stat_count,
        "hold_count": hold_count,
    }
