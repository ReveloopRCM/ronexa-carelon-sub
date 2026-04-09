"""WorkerSession — Restate Virtual Object keyed by worker_id.

THIN HANDLERS: Each handler has exactly 2 journal entries:
  1. ctx.run("get_worker_url") — look up worker HTTP URL from DB
  2. ctx.run("call_worker")    — HTTP call to worker VM

All business logic (browser, portal, DB writes) lives on the worker
HTTP server (app/worker/http_server.py). This eliminates:
  - RT0016 journal mismatches on code changes
  - Multi-worker routing issues (we control URL routing, not Restate)

Handlers:
  - run_first_pass: Login → portal → questions → save → return (Job 1)
  - run_finalize: Login → portal with approved answers → submit → return (Job 2)
  - close_browser: Close browser on worker
  - status: Health check (shared handler)
"""
from __future__ import annotations

import logging
import time
from typing import Any

import restate
from restate import ObjectContext, ObjectSharedContext
from restate.exceptions import TerminalError

logger = logging.getLogger(__name__)

worker_session = restate.VirtualObject("WorkerSession")


# ── Exclusive Handlers (serial per worker_id) ──


@worker_session.handler()
async def run_first_pass(ctx: ObjectContext, event: dict) -> dict:
    """Run portal first pass for a case via worker HTTP API."""
    worker_id = ctx.key()
    logger.info(f"WorkerSession/{worker_id}: run_first_pass → HTTP call")
    try:
        worker_url = await ctx.run("get_worker_url", _get_worker_url, max_attempts=3, args=(worker_id,))
        event["worker_id"] = worker_id
        return await ctx.run("call_worker", _http_call, max_attempts=2, args=(worker_url, "/process-case", event))
    except TerminalError:
        raise
    except Exception as e:
        logger.error(f"WorkerSession/{worker_id}: run_first_pass failed: {e}")
        raise TerminalError(f"WorkerSession/{worker_id} run_first_pass: {str(e)[:300]}")


@worker_session.handler()
async def run_finalize(ctx: ObjectContext, event: dict) -> dict:
    """Run portal finalize with approved answers via worker HTTP API."""
    worker_id = ctx.key()
    logger.info(f"WorkerSession/{worker_id}: run_finalize → HTTP call")
    try:
        worker_url = await ctx.run("get_worker_url", _get_worker_url, max_attempts=3, args=(worker_id,))
        event["worker_id"] = worker_id
        return await ctx.run("call_worker", _http_call, max_attempts=2, args=(worker_url, "/finalize-case", event))
    except TerminalError:
        raise
    except Exception as e:
        logger.error(f"WorkerSession/{worker_id}: run_finalize failed: {e}")
        raise TerminalError(f"WorkerSession/{worker_id} run_finalize: {str(e)[:300]}")


@worker_session.handler()
async def close_browser(ctx: ObjectContext, event: dict | None = None) -> dict:
    """Close the browser on the worker."""
    worker_id = ctx.key()
    logger.info(f"WorkerSession/{worker_id}: close_browser → HTTP call")
    try:
        worker_url = await ctx.run("get_worker_url", _get_worker_url, max_attempts=3, args=(worker_id,))
        payload = {"worker_id": worker_id}
        return await ctx.run("call_worker", _http_call, max_attempts=3, args=(worker_url, "/close-browser", payload))
    except TerminalError:
        raise
    except Exception as e:
        logger.error(f"WorkerSession/{worker_id}: close_browser failed: {e}")
        raise TerminalError(f"WorkerSession/{worker_id} close_browser: {str(e)[:300]}")


# ── Shared Handler (concurrent, read-only) ──


@worker_session.handler(kind="shared")
async def status(ctx: ObjectSharedContext) -> dict:
    """Query worker status without blocking the exclusive queue."""
    worker_id = ctx.key()
    try:
        worker_url = await _get_worker_url(worker_id)
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{worker_url}/status")
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.warning(f"WorkerSession/{worker_id}: status check failed: {e}")

    return {
        "worker_id": worker_id,
        "browser_alive": False,
        "error": "Worker unreachable",
        "timestamp": time.time(),
    }


# ── Private Helpers ──


async def _get_worker_url(worker_id: str) -> str:
    """Look up worker HTTP URL from WorkerAccount table.

    Matches by container_id first, then falls back to id (primary key).
    """
    from app.db.database import async_session_factory
    from app.db.models import WorkerAccount
    from sqlalchemy import select, or_

    async with async_session_factory() as db:
        result = await db.execute(
            select(WorkerAccount.worker_url)
            .where(
                or_(
                    WorkerAccount.container_id == worker_id,
                    WorkerAccount.id == worker_id,
                )
            )
            .limit(1)
        )
        url = result.scalar_one_or_none()
        if not url:
            raise ValueError(f"No worker_url configured for worker '{worker_id}'")
        return url


async def _http_call(worker_url: str, endpoint: str, payload: dict) -> dict:
    """Call worker HTTP API with timeout for portal operations.

    Portal operations can take up to 5 minutes (login + MFA + full case).
    Timeout set to 300s — generous enough for a full pass, but limits
    worst-case blocking to 10 min (300s × 2 attempts) instead of 20 min.
    """
    import httpx

    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(f"{worker_url}{endpoint}", json=payload)
        resp.raise_for_status()
        return resp.json()
