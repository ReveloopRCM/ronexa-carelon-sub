"""ExtractionService — Restate fan-out for clinical PDF extraction.

Uses restate.gather() for true parallel fan-out with per-case journaling.
Each case extraction is individually journaled via ctx.run() — crash recovery
skips completed cases, not entire batches.

Trigger: fire-and-forget from sync_engine after inserting cases.
"""
from __future__ import annotations

import logging
import time

import restate
from restate import Service, Context

logger = logging.getLogger(__name__)

extraction_service = Service("ExtractionService")


@extraction_service.handler()
async def extract_batch(ctx: Context, batch_data: dict) -> dict:
    """Fan-out extraction for N cases with per-case journaling.

    Each case gets its own ctx.run() journal entry. restate.gather() runs
    up to max_concurrent in parallel. On replay, completed cases are skipped
    instantly (journal hit), only pending cases re-execute.

    Args:
        batch_data: {
            "case_ids": list[str],
            "max_concurrent": int (default 10),
        }

    Returns:
        {"extracted": int, "skipped": int, "failed": int, "duration_s": float}
    """
    case_ids = batch_data.get("case_ids", [])
    max_concurrent = batch_data.get("max_concurrent", 10)

    if not case_ids:
        return {"extracted": 0, "skipped": 0, "failed": 0, "duration_s": 0}

    start = time.time()

    # Step 1: Filter eligible cases (have blob key, no existing notes)
    eligible = await ctx.run(
        "filter_eligible", _filter_eligible, args=(case_ids,)
    )

    if not eligible:
        return {
            "extracted": 0,
            "skipped": len(case_ids),
            "failed": 0,
            "duration_s": round(time.time() - start, 2),
        }

    # Step 2: Fan out with per-case journaling in batches of max_concurrent
    # Each ctx.run() is individually journaled — replay skips completed cases
    batches = [
        eligible[i : i + max_concurrent]
        for i in range(0, len(eligible), max_concurrent)
    ]

    extracted = 0
    failed = 0

    for i, batch in enumerate(batches):
        logger.info(
            f"Extraction batch {i + 1}/{len(batches)}: {len(batch)} cases"
        )

        # Create per-case promises — each individually journaled
        promises = [
            ctx.run(f"extract_{cid}", _extract_single_case, args=(cid,))
            for cid in batch
        ]

        # Gather all promises in parallel — Restate manages concurrency
        results = await restate.gather(*promises)

        # Collect results
        for result in results:
            resolved = await result
            if isinstance(resolved, dict) and resolved.get("success"):
                extracted += 1
            else:
                failed += 1

    duration = round(time.time() - start, 2)
    logger.info(
        f"Extraction complete: {extracted} extracted, "
        f"{len(case_ids) - len(eligible)} skipped, "
        f"{failed} failed, {duration}s"
    )

    return {
        "extracted": extracted,
        "skipped": len(case_ids) - len(eligible),
        "failed": failed,
        "duration_s": duration,
    }


# ---------------------------------------------------------------------------
# Internal helpers (called inside ctx.run for journaling)
# ---------------------------------------------------------------------------


async def _filter_eligible(case_ids: list[str]) -> list[str]:
    """Filter to cases that have a blob key and no existing clinical notes."""
    from app.db.database import async_session_factory
    from app.db import repositories as repo

    eligible = []
    async with async_session_factory() as db:
        for case_id in case_ids:
            case = await repo.get_case(db, case_id)
            if not case:
                continue

            # Must have a blob key
            if not (case.clinical_blob_key or case.file_key):
                continue

            # Skip if already has notes
            notes = await repo.get_notes_for_case(db, case_id)
            if notes:
                continue

            eligible.append(case_id)

    logger.info(f"Eligible for extraction: {len(eligible)}/{len(case_ids)}")
    return eligible


async def _extract_single_case(case_id: str) -> dict:
    """Extract clinical notes or order form for a single case.

    Fetch PDF → Document Intelligence OCR → store ClinicalNote → update state.
    This function is wrapped in ctx.run() — its result is journaled per case.

    Two extraction paths:
      - clinical_blob_key present → Clinical notes → NOTES_UPLOADED
      - file_key only (no clinical_blob_key) → Order form → ORDER_READY
    """
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    from app.db.models import CaseState, SubmissionJob, JobStatus
    from app.ingest.blob_fetcher import fetch_clinical_pdf, fetch_blob
    from app.intelligence.extractor import extract_clinical_context
    from sqlalchemy import update

    try:
        async with async_session_factory() as db:
            case = await repo.get_case(db, case_id)
            if not case:
                return {"success": False, "error": "Case not found"}

            blob_key = case.clinical_blob_key or case.file_key
            if not blob_key:
                return {"success": False, "error": "No blob key"}

            # Determine extraction type: clinical notes vs order form
            is_order_only = not case.clinical_blob_key and bool(case.file_key)

            # Fetch PDF from Azure Blob
            # Clinical cases use clinical_blob_key; order-only use file_key
            try:
                if is_order_only:
                    pdf_bytes = fetch_blob(case.file_key)
                else:
                    pdf_bytes = fetch_clinical_pdf({
                        "clinical_blob_key": case.clinical_blob_key,
                        "file_key": case.file_key,
                    })
            except Exception as e:
                return {"success": False, "error": f"Blob fetch: {e}"}

            if not pdf_bytes:
                return {"success": False, "error": "Empty PDF"}

            # OCR via Azure Document Intelligence
            try:
                case_context = {"cpt_code": case.cpt_code, "icd1": case.icd1}
                structured = await extract_clinical_context(pdf_bytes, case_context)
            except Exception as e:
                return {"success": False, "error": f"OCR: {e}"}

            # Determine page count from structured metadata
            page_count = structured.get("_meta", {}).get("page_count", 1)

            # Tag the document type based on extraction path
            if is_order_only:
                doc_type = "ORDER_FORM"
            else:
                doc_type = structured.get("document_type", "CLINICAL")

            # Store ClinicalNote
            await repo.create_clinical_note(
                db,
                case_id=case_id,
                filename=blob_key,
                page_count=page_count,
                document_type=doc_type,
                document_quality=structured.get("document_quality", "CLEAN"),
                extraction_method="azure_document_intelligence",
                structured=structured,
            )

            # Update case state based on extraction type
            if is_order_only:
                # Order-only case → ORDER_READY + set job_type to ORDER
                if case.state in (CaseState.PENDING_NOTES, CaseState.PENDING_STAT):
                    case.state = CaseState.ORDER_READY

                    # Update the submission job to ORDER type so workers pick it up
                    await db.execute(
                        update(SubmissionJob)
                        .where(SubmissionJob.case_id == case_id)
                        .values(job_type="ORDER")
                    )
            else:
                # Clinical notes → NOTES_UPLOADED (existing behavior)
                if case.state in (CaseState.PENDING_NOTES, CaseState.PENDING_STAT):
                    case.state = CaseState.NOTES_UPLOADED

            await repo.create_audit_event(
                db,
                case_id=case_id,
                actor="system",
                action="notes_extracted_fanout",
                data={
                    "blob_key": blob_key,
                    "page_count": page_count,
                    "text_length": len(structured.get("text", "")),
                    "document_type": doc_type,
                    "is_order_only": is_order_only,
                },
            )

            # Log billing event
            await repo.create_execution_log(
                db,
                event_type="EXTRACTION_ORDER" if is_order_only else "EXTRACTION_CLINICAL",
                status="COMPLETED",
                case=case,
                document_type="ORDER_FORM" if is_order_only else "CLINICAL",
                detail={"page_count": page_count, "blob_key": blob_key},
            )

            await db.commit()
            logger.info(
                f"Case {case_id}: extraction complete ({page_count} pages, "
                f"type={doc_type}, target_state={'ORDER_READY' if is_order_only else 'NOTES_UPLOADED'})"
            )
            return {"success": True, "document_type": doc_type}

    except Exception as e:
        logger.error(f"Case {case_id}: unexpected error: {e}")
        return {"success": False, "error": str(e)}
