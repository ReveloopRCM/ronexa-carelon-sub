"""Settings API — runtime config, DB flush, sync control."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import (
    AuditEvent,
    Case,
    CaseState,
    ClinicalNote,
    Question,
    SubmissionJob,
    SystemSetting,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Terminal states — NEVER deleted, these are the analytics data moat
ANALYTICS_STATES = {
    CaseState.APPROVED,
    CaseState.DENIED,
    CaseState.PENDED,
    CaseState.PENDED_FAX_REVIEW,
    CaseState.NO_AUTH_REQUIRED,
}

# Active workflow states — protected from normal flush, but force flush can clear them
WORKFLOW_STATES = {
    CaseState.IN_REVIEW,
    CaseState.L1_REVIEW,
    CaseState.L2_REVIEW,
    CaseState.CLINICAL_REVIEW,
    CaseState.PROCESSING,
    CaseState.SUBMITTING,
}

# Normal flush protects both; force flush only protects analytics
PROTECTED_STATES = ANALYTICS_STATES | WORKFLOW_STATES


# ── Settings CRUD ──


@router.get("")
async def get_all_settings(db: AsyncSession = Depends(get_db)):
    """Get all settings as a flat dict. API keys are masked."""
    from app.intelligence.llm_config import mask_key, DEFAULT_TEMPLATES

    result = await db.execute(select(SystemSetting))
    rows = result.scalars().all()
    out = {}
    for row in rows:
        if row.key.startswith("api_key_"):
            # Mask API keys
            out[row.key] = mask_key(row.value) if row.value else ""
        elif row.key.startswith("prompt_") and (row.value is None or row.value == "null"):
            # Return default template text when DB has null
            out[row.key] = DEFAULT_TEMPLATES.get(row.key, "")
        else:
            out[row.key] = row.value
    return out


@router.put("/{key}")
async def update_setting(
    key: str, body: dict, db: AsyncSession = Depends(get_db)
):
    """Update a single setting. Body: {"value": <any JSON value>}"""
    from app.intelligence.llm_config import encrypt_value, clear_cache

    setting = await db.get(SystemSetting, key)
    if not setting:
        raise HTTPException(404, f"Setting '{key}' not found")

    value = body.get("value", setting.value)

    # Encrypt API keys before storing
    if key.startswith("api_key_") and value and not value.startswith("gAAAA"):
        value = encrypt_value(value)

    setting.value = value
    setting.updated_at = datetime.utcnow()
    setting.updated_by = body.get("updated_by", "ui")
    await db.commit()

    # Clear LLM config cache so changes take effect immediately
    clear_cache()

    return {"key": key, "value": "updated" if key.startswith("api_key_") else value}


@router.post("/prompt-reset/{prompt_key}")
async def reset_prompt(prompt_key: str, db: AsyncSession = Depends(get_db)):
    """Reset a prompt template to its hardcoded default."""
    from app.intelligence.llm_config import DEFAULT_TEMPLATES, clear_cache

    if prompt_key not in DEFAULT_TEMPLATES:
        raise HTTPException(404, f"Unknown prompt key: {prompt_key}")

    setting = await db.get(SystemSetting, prompt_key)
    if setting:
        setting.value = None  # null = use default
        setting.updated_at = datetime.utcnow()
        await db.commit()

    clear_cache()
    return {"key": prompt_key, "value": DEFAULT_TEMPLATES[prompt_key]}


# ── DB Flush ──


@router.get("/flush/preview")
async def flush_preview(db: AsyncSession = Depends(get_db)):
    """Preview how many cases would be flushed (without deleting)."""
    counts = await _count_flushable(db)
    return counts


@router.post("/flush")
async def flush_cases(force: bool = False, db: AsyncSession = Depends(get_db)):
    """Delete cases and their related records.

    By default, protected states (IN_REVIEW, PROCESSING, SUBMITTING) are kept.
    With force=true, ALL cases are flushed (start-of-day reset).
    """
    # Count before
    counts = await _count_flushable(db)

    if force:
        # Force flush: clear everything EXCEPT analytics (terminal) states
        # This preserves APPROVED/DENIED/PENDED cases for the analytics data moat
        result = await db.execute(
            select(Case.id).where(Case.state.notin_(ANALYTICS_STATES))
        )
        case_ids = [r[0] for r in result.all()]
        if not case_ids:
            return {"flushed": 0, "protected": counts["analytics_protected"], "message": "Nothing to flush — only analytics cases remain", "force": True}
    else:
        if counts["total"] == 0:
            return {"flushed": 0, "protected": counts["protected"], "message": "Nothing to flush"}
        flushable_states = [s for s in CaseState if s not in PROTECTED_STATES]
        result = await db.execute(
            select(Case.id).where(Case.state.in_(flushable_states))
        )
        case_ids = [r[0] for r in result.all()]

    if not case_ids:
        return {"flushed": 0, "protected": counts["protected"]}

    # Cascade delete in order (FK constraints)
    # NOTE: execution_logs is NEVER flushed — it's billing data.
    # Its FK uses ondelete=SET NULL, so case deletion just nulls the reference.
    await db.execute(delete(SubmissionJob).where(SubmissionJob.case_id.in_(case_ids)))
    await db.execute(delete(Question).where(Question.case_id.in_(case_ids)))
    await db.execute(delete(ClinicalNote).where(ClinicalNote.case_id.in_(case_ids)))
    await db.execute(delete(AuditEvent).where(AuditEvent.case_id.in_(case_ids)))
    await db.execute(delete(Case).where(Case.id.in_(case_ids)))

    # Update last_flush setting
    flush_result = {
        "flushed": len(case_ids),
        "protected": counts["analytics_protected"] if force else counts["protected"],
        "analytics_preserved": counts["analytics_protected"],
        "force": force,
        "timestamp": datetime.utcnow().isoformat(),
    }
    await _set_setting(db, "last_flush_at", datetime.utcnow().isoformat())
    await _set_setting(db, "last_flush_result", flush_result)

    await db.commit()
    logger.info(f"Flushed {len(case_ids)} cases, analytics preserved: {counts['analytics_protected']}")
    return flush_result


# ── Sync Now ──


@router.post("/sync-now")
async def sync_now(db: AsyncSession = Depends(get_db)):
    """Trigger an immediate Mongo sync using current settings."""
    from app.ingest.sync_engine import run_sync

    extract = await _get_setting(db, "polling_extract", True)
    limit = await _get_setting(db, "polling_limit", 500)

    result = await run_sync(db, extract=extract, limit=limit)

    # Save sync result to history
    await _record_sync(db, result)
    await db.commit()

    return result


# ── Extract Now ──


@router.post("/extract-now")
async def extract_now(db: AsyncSession = Depends(get_db)):
    """Trigger extraction for all cases needing clinical notes (PENDING_NOTES + PENDING_STAT)."""
    from app.core.settings import settings as env_settings

    # Read batch size from settings
    batch_size = await _get_setting(db, "extraction_batch_size", 10)
    if isinstance(batch_size, str):
        batch_size = int(batch_size)

    # Find cases needing extraction — both standard and STAT
    result = await db.execute(
        select(Case.id).where(
            Case.state.in_([CaseState.PENDING_NOTES, CaseState.PENDING_STAT]),
        )
    )
    case_ids = [r[0] for r in result.all()]

    if not case_ids:
        return {"message": "No cases need extraction", "count": 0}

    # Fire-and-forget to ExtractionService via Restate
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{env_settings.RESTATE_URL}/ExtractionService/extract_batch/send",
                json={"case_ids": case_ids, "max_concurrent": batch_size},
                headers={"Content-Type": "application/json"},
                timeout=10.0,
            )
            if resp.status_code in (200, 202):
                return {"message": f"Extraction triggered for {len(case_ids)} cases", "count": len(case_ids)}
            else:
                return {"error": f"Restate returned {resp.status_code}", "count": 0}
    except Exception as e:
        raise HTTPException(502, f"Failed to trigger extraction: {e}")


# ── Start Worker ──


@router.post("/start-worker")
async def start_worker(db: AsyncSession = Depends(get_db)):
    """Start WorkerLoop for all active workers — pull-based streaming dispatch."""
    from app.core.settings import settings as env_settings
    from app.db.models import WorkerAccount

    # Get all active workers
    result = await db.execute(
        select(WorkerAccount).where(WorkerAccount.is_active == True)
    )
    workers = result.scalars().all()

    if not workers:
        return {"error": "No active workers configured", "started": 0}

    import httpx
    started = []
    errors = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        for worker in workers:
            wid = worker.container_id
            try:
                # Clear any lingering stop flag before starting
                worker.stop_requested = False
                await db.flush()

                resp = await client.post(
                    f"{env_settings.RESTATE_URL}/WorkerLoop/{wid}/start/send",
                    json={
                        "max_cases": None,
                        "job_type": worker.job_type or "FIRST_PASS",
                    },
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code in (200, 202):
                    started.append(wid)
                else:
                    errors.append(f"{wid}: Restate returned {resp.status_code}")
            except Exception as e:
                errors.append(f"{wid}: {str(e)[:200]}")

    await db.commit()

    return {
        "message": f"Started {len(started)} worker loops",
        "status": "running",
        "started": started,
        "errors": errors if errors else None,
    }


@router.post("/stop-worker")
async def stop_worker(db: AsyncSession = Depends(get_db)):
    """Signal all active worker loops to stop after their current case."""
    from app.core.settings import settings as env_settings
    from app.db.models import WorkerAccount

    result = await db.execute(
        select(WorkerAccount).where(WorkerAccount.is_active == True)
    )
    workers = result.scalars().all()

    if not workers:
        return {"stopped": 0}

    import httpx
    stopped = []
    errors = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        for worker in workers:
            wid = worker.container_id
            try:
                resp = await client.post(
                    f"{env_settings.RESTATE_URL}/WorkerLoop/{wid}/request_stop",
                    json=None,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code in (200, 202):
                    stopped.append(wid)
                else:
                    errors.append(f"{wid}: Restate returned {resp.status_code}")
            except Exception as e:
                errors.append(f"{wid}: {str(e)[:200]}")

    return {
        "message": f"Stop requested for {len(stopped)} workers",
        "stopped": stopped,
        "errors": errors if errors else None,
    }


@router.get("/worker-status")
async def worker_status(db: AsyncSession = Depends(get_db)):
    """Get live status of all worker loops."""
    from app.core.settings import settings as env_settings
    from app.db.models import WorkerAccount

    result = await db.execute(
        select(WorkerAccount).where(WorkerAccount.is_active == True)
    )
    workers = result.scalars().all()

    import httpx
    statuses = []

    async with httpx.AsyncClient(timeout=5.0) as client:
        for worker in workers:
            wid = worker.container_id
            try:
                resp = await client.post(
                    f"{env_settings.RESTATE_URL}/WorkerLoop/{wid}/status",
                    json=None,
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code == 200:
                    statuses.append(resp.json())
                else:
                    statuses.append({
                        "worker_id": wid,
                        "error": f"Restate returned {resp.status_code}",
                    })
            except Exception as e:
                statuses.append({
                    "worker_id": wid,
                    "error": str(e)[:200],
                })

    return {"workers": statuses}


# ── Reaper Service Control ──
#
# Reaper is a Restate VirtualObject (single key 'main') that periodically
# sweeps stale CLAIMED / PROCESSING / SUBMITTING jobs and surfaces exhausted
# jobs to HOLD. backend-api lifespan auto-triggers it on startup, but ops
# also need to start/stop/check it on demand. Endpoints mirror the
# WorkerLoop start/stop/status pattern.


@router.post("/reaper/start")
async def start_reaper():
    """Manually (re-)trigger the Reaper VO. Idempotent — Restate dedupes
    per-key, so calling this when one is already running just queues a
    second invocation behind it (which serially runs after the first
    completes its bounded 720 iterations / ~24h).
    """
    from app.core.settings import settings as env_settings
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{env_settings.RESTATE_URL}/Reaper/main/start/send",
                json={},
                headers={"Content-Type": "application/json"},
            )
            ok = resp.status_code in (200, 201, 202)
            body = resp.text[:300]
            logger.info(
                f"Reaper start: http={resp.status_code} body={body!r}"
            )
            return {
                "status": "started" if ok else "error",
                "http_code": resp.status_code,
                "response": body,
            }
    except Exception as e:
        logger.error(f"Reaper start failed: {e}")
        raise HTTPException(502, f"Failed to send Reaper start: {e}")


@router.post("/reaper/stop")
async def stop_reaper():
    """Cancel all active Reaper invocations.

    Queries sys_invocation_status for any Reaper invocations not in a
    terminal state (invoked / suspended / inboxed) and sends a graceful
    cancel via Restate's admin API. backend-api lifespan re-sends
    /Reaper/main/start/send on next restart, so this is reversible.
    """
    from app.core.settings import settings as env_settings
    import httpx
    cancelled = []
    errors = []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Query active Reaper invocations. The SQL endpoint returns
            # Arrow IPC by default; force JSON via Accept header (newer
            # Restate versions support this).
            q_resp = await client.post(
                f"{env_settings.RESTATE_ADMIN_URL}/query",
                json={
                    "query": (
                        "SELECT id, status FROM sys_invocation_status "
                        "WHERE target_service_name = 'Reaper' "
                        "AND status IN ('invoked', 'suspended', 'inboxed')"
                    ),
                },
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
            inv_ids = _extract_reaper_ids_from_query_response(q_resp)
            for inv_id in inv_ids:
                try:
                    r = await client.patch(
                        f"{env_settings.RESTATE_ADMIN_URL}/invocations/{inv_id}/cancel",
                        timeout=10.0,
                    )
                    cancelled.append({"id": inv_id, "http": r.status_code})
                except Exception as ce:
                    errors.append({"id": inv_id, "error": str(ce)[:200]})
        logger.info(
            f"Reaper stop: cancelled {len(cancelled)} invocation(s) "
            f"({len(errors)} errors)"
        )
        return {
            "cancelled": cancelled,
            "errors": errors if errors else None,
        }
    except Exception as e:
        logger.error(f"Reaper stop failed: {e}")
        raise HTTPException(502, f"Failed to stop Reaper: {e}")


@router.get("/reaper/status")
async def reaper_status():
    """Current Reaper VO state from sys_invocation_status."""
    from app.core.settings import settings as env_settings
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            q_resp = await client.post(
                f"{env_settings.RESTATE_ADMIN_URL}/query",
                json={
                    "query": (
                        "SELECT id, status, created_at FROM sys_invocation_status "
                        "WHERE target_service_name = 'Reaper' "
                        "ORDER BY created_at DESC LIMIT 10"
                    ),
                },
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
            invocations = _extract_reaper_invocations_from_query_response(q_resp)
        active_count = sum(
            1 for inv in invocations
            if (inv.get("status") or "").lower() in ("invoked", "suspended", "inboxed")
        )
        return {
            "running": active_count > 0,
            "active_count": active_count,
            "invocations": invocations,
        }
    except Exception as e:
        logger.error(f"Reaper status failed: {e}")
        raise HTTPException(502, f"Failed to query Reaper status: {e}")


def _extract_reaper_ids_from_query_response(resp) -> list[str]:
    """Best-effort: parse Restate /query response (JSON or Arrow IPC binary)
    and return the `id` column values. Falls back to a strings-style scan
    if neither structured path works."""
    try:
        data = resp.json()
        rows = data.get("rows") or data.get("data") or []
        if rows and isinstance(rows[0], dict):
            return [r["id"] for r in rows if r.get("id")]
        if rows and isinstance(rows[0], list):
            # First column is `id` per the SELECT statement
            return [r[0] for r in rows if r and r[0]]
    except Exception:
        pass
    # Fallback: scan raw bytes for invocation id pattern. Restate ids start
    # with `inv_` followed by base32-ish alphanumerics.
    import re
    raw = resp.content if hasattr(resp, "content") else b""
    text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
    return list(set(re.findall(r"inv_[A-Za-z0-9]+", text)))


def _extract_reaper_invocations_from_query_response(resp) -> list[dict]:
    """Extract `[{id, status, created_at}, ...]` from Restate /query response."""
    try:
        data = resp.json()
        rows = data.get("rows") or data.get("data") or []
        out = []
        if rows and isinstance(rows[0], dict):
            for r in rows:
                out.append({
                    "id": r.get("id"),
                    "status": r.get("status"),
                    "created_at": r.get("created_at"),
                })
            return out
        if rows and isinstance(rows[0], list):
            for r in rows:
                out.append({
                    "id": r[0] if len(r) > 0 else None,
                    "status": r[1] if len(r) > 1 else None,
                    "created_at": r[2] if len(r) > 2 else None,
                })
            return out
    except Exception:
        pass
    # Fallback: extract just the ids if structured parsing fails
    ids = _extract_reaper_ids_from_query_response(resp)
    return [{"id": i, "status": "unknown", "created_at": None} for i in ids]


# ── Mongo Sync Source (UAT / Prod toggle) ──
#
# v151: the case-sync source is dual-environment. UAT and Prod use different
# Cosmos-Mongo databases / collections / filter envelopes (the inner
# document shape is identical). The active source is selected at runtime
# via a SystemSetting `active_mongo_environment` (default "uat"). This
# endpoint pair lets the Settings UI read + flip the toggle. Connection
# strings stay in env vars (secrets); only the env-name selector lives
# in the DB.


@router.get("/mongo-environment")
async def get_mongo_env(db: AsyncSession = Depends(get_db)):
    """Return current active Mongo environment + connection metadata.

    Connection strings are NOT exposed — only the env name and
    non-secret metadata (DB name, collection name, whether the URI is
    configured) so the UI can show ✓ / ✗ for each side and reps verify
    before flipping.
    """
    from app.core.settings import settings as env_settings
    val = await _get_setting(db, "active_mongo_environment", "uat")
    if not isinstance(val, str):
        # Setting was stored as {"value": "..."} — accept that shape too
        val = (val or {}).get("value", "uat") if isinstance(val, dict) else "uat"
    if val not in ("uat", "prod"):
        val = "uat"

    return {
        "active": val,
        "uat": {
            "db": env_settings.MONGO_DB,
            "collection": env_settings.MONGO_COLLECTION,
            "uri_set": bool(env_settings.MONGO_URI),
        },
        "prod": {
            "db": env_settings.MONGO_DB_PROD,
            "collection": env_settings.MONGO_COLLECTION_PROD,
            "uri_set": bool(env_settings.MONGO_URI_PROD),
        },
    }


@router.post("/mongo-environment")
async def set_mongo_env(body: dict, db: AsyncSession = Depends(get_db)):
    """Switch the active Mongo environment. Body: {"value": "uat"|"prod"}.

    The next sync iteration (poll_scheduler tick or manual /api/sync)
    picks up the new value. Reversible — flip back at any time.
    """
    val = (body.get("value") or "").strip().lower()
    if val not in ("uat", "prod"):
        raise HTTPException(400, "value must be 'uat' or 'prod'")
    await _set_setting(db, "active_mongo_environment", val)
    logger.info(f"Mongo environment switched → {val}")
    return {"active": val, "status": "updated"}


# ── Sync History ──


@router.get("/sync-history")
async def sync_history(db: AsyncSession = Depends(get_db)):
    """Get the last 10 sync results."""
    history = await _get_setting(db, "sync_history", [])
    return history


# ── Helpers ──


async def _count_flushable(db: AsyncSession) -> dict:
    """Count cases by flush eligibility."""
    result = await db.execute(
        text("""
            SELECT state, COUNT(*) as cnt
            FROM cases
            GROUP BY state
        """)
    )
    by_state = {row[0]: row[1] for row in result.all()}

    analytics_protected = sum(
        by_state.get(s.value, 0)
        for s in ANALYTICS_STATES
    )
    workflow_protected = sum(
        by_state.get(s.value, 0)
        for s in WORKFLOW_STATES
    )
    protected = analytics_protected + workflow_protected
    total = sum(by_state.values()) - protected

    return {
        "total": total,
        "protected": protected,
        "analytics_protected": analytics_protected,
        "workflow_protected": workflow_protected,
        "by_state": by_state,
    }


async def _get_setting(db: AsyncSession, key: str, default=None):
    """Get a single setting value."""
    setting = await db.get(SystemSetting, key)
    if not setting:
        return default
    return setting.value


async def _set_setting(db: AsyncSession, key: str, value) -> None:
    """Set a single setting value."""
    setting = await db.get(SystemSetting, key)
    if setting:
        setting.value = value
        setting.updated_at = datetime.utcnow()
    else:
        db.add(SystemSetting(key=key, value=value))
    await db.flush()


# ── Bypass Rules CRUD ──


@router.get("/bypass-rules")
async def list_bypass_rules(db: AsyncSession = Depends(get_db)):
    """List all bypass rules."""
    from app.db.models import BypassRule
    result = await db.execute(select(BypassRule).order_by(BypassRule.cpt_code))
    rules = result.scalars().all()
    return [
        {
            "id": r.id,
            "cpt_code": r.cpt_code,
            "icd_code": r.icd_code,
            "min_confidence": r.min_confidence,
            "bypass_l1": r.bypass_l1,
            "bypass_l2": r.bypass_l2,
            "approved_count": r.approved_count,
            "last_approved_at": r.last_approved_at.isoformat() if r.last_approved_at else None,
            "enabled": r.enabled,
            "created_by": r.created_by,
        }
        for r in rules
    ]


@router.post("/bypass-rules")
async def create_bypass_rule(body: dict, db: AsyncSession = Depends(get_db)):
    """Create a new bypass rule for a CPT+ICD combination."""
    from app.db.models import BypassRule

    rule = BypassRule(
        cpt_code=body.get("cpt_code", ""),
        icd_code=body.get("icd_code", ""),
        min_confidence=body.get("min_confidence", 85.0),
        bypass_l1=body.get("bypass_l1", True),
        bypass_l2=body.get("bypass_l2", False),
        enabled=body.get("enabled", False),
        created_by=body.get("created_by", "ui"),
    )
    db.add(rule)
    await db.commit()
    return {"id": rule.id, "status": "created"}


@router.put("/bypass-rules/{rule_id}")
async def update_bypass_rule(rule_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    """Update a bypass rule."""
    from app.db.models import BypassRule

    rule = await db.get(BypassRule, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")

    for field in ("cpt_code", "icd_code", "min_confidence", "bypass_l1", "bypass_l2", "enabled"):
        if field in body:
            setattr(rule, field, body[field])
    await db.commit()
    return {"id": rule.id, "status": "updated"}


@router.delete("/bypass-rules/{rule_id}")
async def delete_bypass_rule(rule_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a bypass rule."""
    from app.db.models import BypassRule

    rule = await db.get(BypassRule, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    await db.delete(rule)
    await db.commit()
    return {"status": "deleted"}


# ── Automation Rules (Full CPT+ICD Automation) ──


@router.get("/automation-rules")
async def list_automation_rules(db: AsyncSession = Depends(get_db)):
    """List all automation rules with submission stats."""
    from app.db.models import AutomationRule

    result = await db.execute(select(AutomationRule).order_by(AutomationRule.created_at.desc()))
    rules = result.scalars().all()

    # Get submission stats per CPT+ICD
    stats_query = await db.execute(
        select(
            Case.cpt_code,
            Case.icd1,
            func.count().label("total"),
            func.count().filter(Case.state == CaseState.APPROVED).label("approved"),
            func.count().filter(Case.state == CaseState.DENIED).label("denied"),
            func.count().filter(Case.state == CaseState.PENDED).label("pended"),
        )
        .where(Case.state.in_([CaseState.APPROVED, CaseState.DENIED, CaseState.PENDED]))
        .group_by(Case.cpt_code, Case.icd1)
    )
    stats = {(r.cpt_code, r.icd1): r for r in stats_query.all()}

    return [
        {
            "id": r.id,
            "cpt_code": r.cpt_code,
            "icd_code": r.icd_code,
            "enabled": r.enabled,
            "enabled_by": r.enabled_by,
            "enabled_at": r.enabled_at.isoformat() if r.enabled_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "stats": {
                "total": getattr(stats.get((r.cpt_code, r.icd_code)), "total", 0),
                "approved": getattr(stats.get((r.cpt_code, r.icd_code)), "approved", 0),
                "denied": getattr(stats.get((r.cpt_code, r.icd_code)), "denied", 0),
                "pended": getattr(stats.get((r.cpt_code, r.icd_code)), "pended", 0),
            },
        }
        for r in rules
    ]


@router.post("/automation-rules")
async def create_automation_rule(body: dict, db: AsyncSession = Depends(get_db)):
    """Create a new automation rule for a CPT+ICD combination."""
    from app.db.models import AutomationRule

    rule = AutomationRule(
        cpt_code=body["cpt_code"],
        icd_code=body["icd_code"],
        enabled=body.get("enabled", False),
        enabled_by=body.get("enabled_by"),
        enabled_at=datetime.utcnow() if body.get("enabled") else None,
    )
    db.add(rule)
    await db.commit()
    return {"id": rule.id, "status": "created"}


@router.put("/automation-rules/{rule_id}")
async def update_automation_rule(rule_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    """Toggle or update an automation rule."""
    from app.db.models import AutomationRule

    rule = await db.get(AutomationRule, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")

    if "enabled" in body:
        rule.enabled = body["enabled"]
        if body["enabled"]:
            rule.enabled_by = body.get("enabled_by", "operator")
            rule.enabled_at = datetime.utcnow()
        else:
            rule.enabled_at = None

    for field in ("cpt_code", "icd_code"):
        if field in body:
            setattr(rule, field, body[field])

    await db.commit()
    return {"status": "updated"}


@router.delete("/automation-rules/{rule_id}")
async def delete_automation_rule(rule_id: str, db: AsyncSession = Depends(get_db)):
    """Delete an automation rule."""
    from app.db.models import AutomationRule

    rule = await db.get(AutomationRule, rule_id)
    if not rule:
        raise HTTPException(404, "Rule not found")
    await db.delete(rule)
    await db.commit()
    return {"status": "deleted"}


# ── Worker Account Management ──


@router.get("/workers")
async def list_workers(db: AsyncSession = Depends(get_db)):
    """List all worker accounts (passwords never exposed)."""
    from app.db.models import WorkerAccount

    result = await db.execute(select(WorkerAccount).order_by(WorkerAccount.container_id))
    workers = result.scalars().all()
    return [
        {
            "id": w.id,
            "container_id": w.container_id,
            "username": w.username,
            "worker_url": w.worker_url,
            "mailbox_address": w.mailbox_address,
            "shift": w.shift.value if w.shift else "DAY",
            "is_active": w.is_active,
            "is_logged_in": w.is_logged_in,
            "cases_today": w.cases_today,
            "total_cases": w.total_cases,
            "last_login_at": w.last_login_at.isoformat() if w.last_login_at else None,
            "current_case_id": w.current_case_id,
            "job_type": w.job_type or "FIRST_PASS",
        }
        for w in workers
    ]


@router.post("/workers")
async def create_worker(body: dict, db: AsyncSession = Depends(get_db)):
    """Create a new worker account."""
    from app.db.models import WorkerAccount, AccountShift

    worker = WorkerAccount(
        container_id=body["container_id"],
        username=body["username"],
        password=body.get("password", ""),
        worker_url=body.get("worker_url"),
        mailbox_address=body.get("mailbox_address", ""),
        shift=AccountShift(body.get("shift", "DAY")),
        job_type=body.get("job_type", "FIRST_PASS"),
        is_active=body.get("is_active", True),
    )
    db.add(worker)
    await db.commit()
    return {"id": worker.id, "status": "created"}


@router.put("/workers/{worker_id}")
async def update_worker(worker_id: str, body: dict, db: AsyncSession = Depends(get_db)):
    """Update a worker account."""
    from app.db.models import WorkerAccount, AccountShift

    worker = await db.get(WorkerAccount, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")

    for field in ("container_id", "username", "password", "worker_url", "mailbox_address", "is_active", "job_type"):
        if field in body:
            setattr(worker, field, body[field])
    if "shift" in body:
        worker.shift = AccountShift(body["shift"])
    await db.commit()
    return {"id": worker.id, "status": "updated"}


@router.delete("/workers/{worker_id}")
async def delete_worker(worker_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a worker account."""
    from app.db.models import WorkerAccount

    worker = await db.get(WorkerAccount, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    if worker.is_logged_in:
        raise HTTPException(409, "Cannot delete a worker that is currently logged in")
    await db.delete(worker)
    await db.commit()
    return {"status": "deleted"}


@router.post("/workers/{worker_id}/toggle")
async def toggle_worker(worker_id: str, db: AsyncSession = Depends(get_db)):
    """Toggle a worker's active state. Sends stop signal when deactivating."""
    from app.db.models import WorkerAccount

    worker = await db.get(WorkerAccount, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")

    was_active = worker.is_active
    worker.is_active = not worker.is_active

    # When deactivating, also signal the WorkerLoop to stop
    stop_sent = False
    if was_active and not worker.is_active:
        worker.stop_requested = True
        wid = worker.container_id
        try:
            from app.core.settings import settings as env_settings
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{env_settings.RESTATE_URL}/WorkerLoop/{wid}/request_stop",
                    json=None,
                    headers={"Content-Type": "application/json"},
                )
                stop_sent = resp.status_code in (200, 202)
        except Exception:
            pass  # DB flag is the primary mechanism; Restate call is best-effort

    await db.commit()
    return {"id": worker.id, "is_active": worker.is_active, "stop_sent": stop_sent}


async def record_sync_result(db: AsyncSession, result: dict) -> None:
    """Public interface for poll_scheduler to record sync results."""
    await _record_sync(db, result)


async def _record_sync(db: AsyncSession, result: dict) -> None:
    """Append a sync result to the history (keep last 10)."""
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "total_fetched": result.get("total_fetched", 0),
        "new_cases": result.get("new_cases", 0),
        "duplicates_skipped": result.get("duplicates_skipped", 0),
        "notes_extracted": result.get("notes_extracted", 0),
        "enqueued": result.get("enqueued", 0),
        "errors": result.get("errors", 0) + result.get("extraction_errors", 0),
        "env": result.get("env", "uat"),  # v151: track which Mongo source this run hit
    }

    await _set_setting(db, "last_sync_at", entry["timestamp"])
    await _set_setting(db, "last_sync_result", entry)

    # Append to history, keep last 10
    history = await _get_setting(db, "sync_history", [])
    if not isinstance(history, list):
        history = []
    history.insert(0, entry)
    history = history[:10]
    await _set_setting(db, "sync_history", history)


# ── RingCentral Integration Settings ──


RC_KEYS = [
    "rc_client_id", "rc_client_secret", "rc_jwt_token",
    "rc_server_url", "rc_fax_enabled", "carelon_fax_number",
]
RC_ENCRYPTED_KEYS = {"rc_client_secret", "rc_jwt_token"}


@router.get("/integrations/rc")
async def get_rc_settings(db: AsyncSession = Depends(get_db)):
    """Get RingCentral fax configuration. Secrets are masked."""
    result = {}
    for key in RC_KEYS:
        val = await _get_setting(db, key, None)
        if val is None:
            # Defaults
            if key == "rc_server_url":
                val = "https://platform.ringcentral.com"
            elif key == "carelon_fax_number":
                val = "+18007982068"
            elif key == "rc_fax_enabled":
                val = False
            else:
                val = ""
        # Mask encrypted values for display
        if key in RC_ENCRYPTED_KEYS and val and isinstance(val, str) and len(val) > 8:
            result[key] = val[:4] + "•" * 20 + val[-4:]
        else:
            result[key] = val
    return result


@router.put("/integrations/rc")
async def update_rc_settings(body: dict, db: AsyncSession = Depends(get_db)):
    """Update RingCentral fax configuration. Encrypts secrets."""
    from app.intelligence.llm_config import encrypt_value

    updated = []
    for key in RC_KEYS:
        if key not in body:
            continue
        value = body[key]
        # Don't overwrite with masked values
        if key in RC_ENCRYPTED_KEYS and "•" in str(value):
            continue
        # Encrypt secrets
        if key in RC_ENCRYPTED_KEYS and value and not str(value).startswith("gAAAA"):
            value = encrypt_value(str(value))
        await _set_setting(db, key, value)
        updated.append(key)

    return {"updated": updated}


@router.post("/integrations/rc/test")
async def test_rc_connection(db: AsyncSession = Depends(get_db)):
    """Test RingCentral authentication by requesting an access token."""
    from app.intelligence.llm_config import decrypt_value

    client_id = await _get_setting(db, "rc_client_id", "")
    client_secret_enc = await _get_setting(db, "rc_client_secret", "")
    jwt_token_enc = await _get_setting(db, "rc_jwt_token", "")
    server_url = await _get_setting(db, "rc_server_url", "https://platform.ringcentral.com")

    # Decrypt
    client_secret = decrypt_value(client_secret_enc) if client_secret_enc else ""
    jwt_token = decrypt_value(jwt_token_enc) if jwt_token_enc else ""

    # Fall back to env vars if DB is empty
    if not client_id:
        from app.core.settings import settings as app_settings
        client_id = app_settings.RC_CLIENT_ID
        client_secret = app_settings.RC_CLIENT_SECRET
        jwt_token = app_settings.RC_JWT_TOKEN
        server_url = app_settings.RC_SERVER_URL or server_url

    if not all([client_id, client_secret, jwt_token]):
        return {"ok": False, "error": "Missing RC credentials"}

    import aiohttp
    token_url = f"{server_url}/restapi/oauth/token"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                token_url,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": jwt_token,
                },
                auth=aiohttp.BasicAuth(client_id, client_secret),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        "ok": True,
                        "message": f"Authenticated. Token expires in {data.get('expires_in', '?')}s",
                        "owner_id": data.get("owner_id"),
                    }
                else:
                    body = await resp.text()
                    return {"ok": False, "error": f"Auth failed ({resp.status}): {body[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
