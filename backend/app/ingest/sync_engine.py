"""Shared sync engine — used by both the /api/sync route and poll_scheduler."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repositories as repo
from app.db.models import Case, CaseState

logger = logging.getLogger(__name__)


async def run_sync(
    db: AsyncSession,
    extract: bool = True,
    limit: int = 500,
) -> dict:
    """Pull new Carelon submissions from Mongo → Postgres + optional PDF extraction.

    Returns summary dict with counts.
    """
    from app.ingest.mongo_poller import fetch_carelon_submissions, mark_mongo_records_synced
    from app.ingest.blob_fetcher import fetch_clinical_pdf
    from app.ingest.pdf_parser import extract_page_images
    from app.intelligence.extractor import extract_clinical_context

    # Step 1: Fetch from Mongo
    sync_result = fetch_carelon_submissions(limit=limit)

    if not sync_result.cases:
        return {
            "total_fetched": sync_result.total_fetched,
            "new_cases": 0,
            "duplicates_skipped": 0,
            "notes_extracted": 0,
            "extraction_errors": 0,
            "errors": sync_result.errors,
            "enqueued": 0,
        }

    # Step 2: Dedup against Postgres
    new_cases = []
    skipped = 0
    for case_dict in sync_result.cases:
        existing = await repo.get_case_by_exam_id(db, case_dict["exam_id"])
        if existing:
            skipped += 1
            continue
        new_cases.append(case_dict)

    logger.info(
        f"Sync: {sync_result.total_fetched} fetched, "
        f"{len(new_cases)} new, {skipped} duplicates"
    )

    # Step 3: Insert new cases
    inserted_exam_ids = []
    inserted_case_ids = []
    cases_with_blobs = []

    for case_dict in new_cases:
        clinical_blob = case_dict.pop("clinical_blob_key", None)
        referring_fax = case_dict.pop("referring_fax", None)
        patient_phone = case_dict.pop("patient_phone", None)
        external_ref = case_dict.pop("external_reference_id", None)

        case = Case(
            id=case_dict["id"],
            exam_id=case_dict["exam_id"],
            first_name=case_dict.get("first_name") or "",
            last_name=case_dict.get("last_name") or "",
            dob=case_dict.get("dob") or "",
            policy_num=case_dict.get("policy_num") or "",
            patient_zip=case_dict.get("patient_zip"),
            center_npi=case_dict.get("center_npi") or "",
            center_abbr=case_dict.get("center_abbr"),
            cpt_code=case_dict.get("cpt_code") or "",
            icd1=case_dict.get("icd1"),
            icd2=case_dict.get("icd2"),
            icd3=case_dict.get("icd3"),
            icd4=case_dict.get("icd4"),
            icd5=case_dict.get("icd5"),
            referring_npi=case_dict.get("referring_npi"),
            carrier_id=case_dict.get("carrier_id"),
            file_key=case_dict.get("file_key"),
            attachment_id=case_dict.get("attachment_id"),
            clinical_blob_key=clinical_blob,
            referring_fax=referring_fax,
            patient_phone=patient_phone,
            external_reference_id=external_ref,
            is_stat=case_dict.get("is_stat", False),
            scheduled_dt=case_dict.get("scheduled_dt"),
            auth_exam_id=case_dict.get("auth_exam_id"),
            order_request_id=case_dict.get("order_request_id"),
            state=CaseState(case_dict.get("state", "PENDING_NOTES")),
            sort_priority=case_dict.get("sort_priority", 3),
            hold_reason=case_dict.get("hold_reason"),
            raw_data=case_dict.get("raw_data", {}),
            batch_id=None,
        )
        db.add(case)
        await db.flush()

        await repo.create_audit_event(
            db,
            case_id=case.id,
            actor="system",
            action="ingested",
            data={"source": "mongo_sync", "exam_id": case.exam_id},
        )

        # Log billing event: RECEIVED
        await repo.create_execution_log(
            db,
            event_type="RECEIVED",
            case=case,
            document_type="CLINICAL" if clinical_blob else ("ORDER_FORM" if case.file_key else None),
        )

        inserted_exam_ids.append(case.exam_id)
        inserted_case_ids.append(case.id)

        # Track cases with blob keys for extraction fan-out
        if clinical_blob or case.file_key:
            cases_with_blobs.append(case.id)

    # Step 4: Auto-enqueue ALL new cases into submission queue.
    from app.db.queue import enqueue_cases_bulk

    enqueued_jobs = []
    if inserted_case_ids:
        enqueued_jobs = await enqueue_cases_bulk(db, inserted_case_ids)
        logger.info(f"Auto-enqueued {len(enqueued_jobs)} cases into submission queue")

    await db.commit()

    # Step 5: Mark Mongo records as synced (prevents re-fetch on next poll)
    if inserted_exam_ids:
        mark_mongo_records_synced(inserted_exam_ids)

    # Step 6: Fire-and-forget extraction for cases with blob keys
    # ExtractionService runs parallel OCR via Restate (non-blocking)
    if extract and cases_with_blobs:
        try:
            import httpx
            from app.core.settings import settings

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{settings.RESTATE_URL}/ExtractionService/extract_batch/send",
                    json={
                        "case_ids": cases_with_blobs,
                        "max_concurrent": 10,
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=10.0,
                )
                if resp.status_code in (200, 202):
                    logger.info(
                        f"Fired extraction for {len(cases_with_blobs)} cases "
                        f"via ExtractionService"
                    )
                else:
                    logger.warning(
                        f"ExtractionService fire-and-forget returned {resp.status_code}"
                    )
        except Exception as e:
            logger.error(f"Failed to fire ExtractionService: {e}")
            # Non-fatal — cases are enqueued, extraction can be triggered manually

    # Step 7: Auto re-run WAITING_CLINICALS cases that now have clinical notes.
    # When an order-only case was parked awaiting clinicals and the clinical
    # blob key has since appeared (from a new Cosmos sync), transition it to
    # NOTES_UPLOADED and re-queue as FIRST_PASS so CaseWorkflow picks it up
    # with full clinical context.
    rerun_count = 0
    try:
        from sqlalchemy import select as sa_select, update as sa_update
        from app.db.models import SubmissionJob, JobStatus, JobType

        awaiting_result = await db.execute(
            sa_select(Case).where(Case.state == CaseState.WAITING_CLINICALS)
        )
        for wc_case in awaiting_result.scalars():
            if wc_case.clinical_blob_key:
                # Clinicals have arrived — transition to NOTES_UPLOADED
                wc_case.state = CaseState.NOTES_UPLOADED

                # Re-queue the submission job as FIRST_PASS
                await db.execute(
                    sa_update(SubmissionJob)
                    .where(SubmissionJob.case_id == wc_case.id)
                    .values(
                        job_type="FIRST_PASS",
                        status=JobStatus.QUEUED,
                        claimed_by=None,
                        claimed_at=None,
                        last_error=None,
                        started_at=None,
                        completed_at=None,
                    )
                )

                await repo.create_audit_event(
                    db,
                    case_id=wc_case.id,
                    actor="system",
                    action="clinicals_arrived_rerun",
                    data={
                        "clinical_blob_key": wc_case.clinical_blob_key,
                        "previous_state": "WAITING_CLINICALS",
                    },
                )
                rerun_count += 1
                logger.info(
                    f"Case {wc_case.id}: clinicals arrived — "
                    f"WAITING_CLINICALS → NOTES_UPLOADED (re-queued as FIRST_PASS)"
                )

        if rerun_count:
            await db.commit()
            logger.info(f"Auto re-run: {rerun_count} cases transitioned from WAITING_CLINICALS")
    except Exception as e:
        logger.error(f"WAITING_CLINICALS auto-rerun check failed: {e}")

    # Step 8: Wake sleeping workers + reap stale claims
    # Workers are pull-based (WorkerLoop) — no batch dispatch needed.
    # Just signal sleeping workers that new cases are available.
    workers_woken = 0
    stale_reaped = 0
    processing_reaped = 0
    try:
        from app.workflow.worker_loop import wake_sleeping_workers, reap_stale_claims, reap_stale_processing

        if inserted_case_ids or rerun_count:
            workers_woken = await wake_sleeping_workers()
            logger.info(f"Woke {workers_woken} sleeping workers after sync")

        # Reap CLAIMED jobs that crashed between claim and dispatch (>10 min old)
        stale_reaped = await reap_stale_claims()
        # Reap PROCESSING cases with stale QUEUED/RUNNING jobs
        processing_reaped = await reap_stale_processing()
    except Exception as e:
        logger.error(f"Worker wake/reap failed: {e}")

    return {
        "total_fetched": sync_result.total_fetched,
        "new_cases": len(inserted_exam_ids),
        "duplicates_skipped": skipped,
        "notes_extracted": 0,  # Extraction is now async via Restate
        "extraction_errors": 0,
        "errors": sync_result.errors,
        "enqueued": len(enqueued_jobs),
        "extraction_triggered": len(cases_with_blobs),
        "workers_woken": workers_woken,
        "stale_reaped": stale_reaped,
        "processing_reaped": processing_reaped,
        "clinicals_rerun": rerun_count,
    }
