"""Reaper — Restate VirtualObject (single key 'main').

Periodic sweep of stale CLAIMED / PROCESSING / SUBMITTING jobs every 2
minutes, plus surfaces exhausted jobs to HOLD. Independent of
`poll_scheduler` (which used to ride-along reap inside
`sync_engine.run_sync` — too coarse at 15-min cadence and dies if polling
stops).

Why a Restate VirtualObject (not a FastAPI lifespan asyncio task):
  - **Single instance** enforced by Restate per-key exclusive serialization.
    Multiple backend-api replicas (or a stuck restart) can't run two
    reapers in parallel.
  - **Survives backend-api restarts.** Restate persists the journal, so
    `ctx.sleep` timers and per-iteration `ctx.run` results replay
    deterministically. A backend-api crash mid-iteration is recovered.
  - **Symmetric ops surface with WorkerLoop.** Visible in
    `sys_invocation_status`, killable via the Restate admin API,
    observable in the Restate UI. No new monitoring path.
  - **Independent of poll_scheduler.** If polling is disabled or its
    asyncio task crashes, the reaper keeps running.

Boot: backend-api lifespan POSTs to `/Reaper/main/start/send` on startup.
Idempotent — Restate dedupes per-key sends (subsequent sends queue behind
the running invocation, which is fine because the loop bounds itself to
720 iterations / ~24 hours so the queue can't grow unboundedly across
deploys).

Usage:
    POST /Reaper/main/start/send   {}
"""
from __future__ import annotations

import logging
from datetime import timedelta

import restate
from restate import ObjectContext
from restate.exceptions import TerminalError

logger = logging.getLogger(__name__)

reaper = restate.VirtualObject("Reaper")

# Bound iterations so each backend-api restart sends a fresh /start and
# Restate's per-key send queue stays bounded. 720 * 120s = 24 hours.
_MAX_ITERATIONS = 720
_CADENCE_SEC = 120


@reaper.handler()
async def start(ctx: ObjectContext, _: dict | None = None) -> dict:
    """Reaper main loop — sleeps `_CADENCE_SEC` between sweeps, exits after
    `_MAX_ITERATIONS` so backend-api can re-trigger cleanly on next deploy.
    """
    iteration = 0
    while iteration < _MAX_ITERATIONS:
        iteration += 1
        try:
            await ctx.sleep(timedelta(seconds=_CADENCE_SEC))
            n_claims = await ctx.run(
                f"reap_claims_{iteration}", _reap_claims, max_attempts=2
            )
            n_processing = await ctx.run(
                f"reap_processing_{iteration}", _reap_processing, max_attempts=2
            )
            n_submitting = await ctx.run(
                f"reap_submitting_{iteration}", _reap_submitting, max_attempts=2
            )
            n_failed = await ctx.run(
                f"fail_exhausted_{iteration}", _fail_exhausted, max_attempts=2
            )
            if n_claims or n_processing or n_submitting or n_failed:
                logger.info(
                    f"Reaper#{iteration}: claims={n_claims} "
                    f"processing={n_processing} submitting={n_submitting} "
                    f"failed={n_failed}"
                )
        except TerminalError:
            raise
        except Exception as e:
            # Single-iteration failure is non-fatal — continue. ctx.run
            # already retries up to max_attempts internally; getting here
            # means it exhausted those (usually a transient DB blip).
            logger.error(
                f"Reaper#{iteration}: iteration error (continuing): {e}"
            )
    logger.info(f"Reaper: completed {iteration} iterations, exiting cleanly")
    return {"iterations": iteration, "status": "completed"}


# ── Plain async wrappers (called via ctx.run for durability) ──
#
# Imports are inside functions to avoid pulling worker_loop's heavy
# dependency tree at module-load time (it imports clinical / browser
# modules that aren't needed for the reaper's narrow surface).


async def _reap_claims() -> int:
    from app.workflow.worker_loop import reap_stale_claims
    return await reap_stale_claims()


async def _reap_processing() -> int:
    from app.workflow.worker_loop import reap_stale_processing
    return await reap_stale_processing()


async def _reap_submitting() -> int:
    from app.workflow.worker_loop import reap_stale_submitting
    return await reap_stale_submitting()


async def _fail_exhausted() -> int:
    from app.workflow.worker_loop import fail_exhausted_jobs
    return await fail_exhausted_jobs()
