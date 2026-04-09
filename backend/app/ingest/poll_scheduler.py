"""Mongo Poll Scheduler — background asyncio task.

Reads polling config from system_settings, runs sync on interval.
Launched in FastAPI lifespan, stops when polling_enabled=false or app shuts down.
"""
from __future__ import annotations

import asyncio
import logging

from app.db.database import async_session_factory
from app.db.models import SystemSetting

logger = logging.getLogger(__name__)

_poll_task: asyncio.Task | None = None


async def start_poll_scheduler() -> None:
    """Start the background polling loop. Called from FastAPI lifespan."""
    global _poll_task
    if _poll_task and not _poll_task.done():
        logger.info("Poll scheduler already running")
        return
    _poll_task = asyncio.create_task(_poll_loop())
    logger.info("Poll scheduler started")


async def stop_poll_scheduler() -> None:
    """Stop the background polling loop. Called from FastAPI shutdown."""
    global _poll_task
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
    _poll_task = None
    logger.info("Poll scheduler stopped")


async def _poll_loop() -> None:
    """Main loop — checks settings each iteration, syncs if enabled."""
    while True:
        try:
            async with async_session_factory() as db:
                enabled = await _get(db, "polling_enabled", False)
                interval = await _get(db, "polling_interval_minutes", 15)

            if not enabled:
                # Check again in 30s
                await asyncio.sleep(30)
                continue

            # Run sync using the shared engine
            logger.info("Poll scheduler: running sync...")
            try:
                async with async_session_factory() as db:
                    extract = await _get(db, "polling_extract", True)
                    limit = await _get(db, "polling_limit", 500)

                async with async_session_factory() as db:
                    from app.ingest.sync_engine import run_sync
                    from app.api.routes.settings import record_sync_result

                    result = await run_sync(db, extract=extract, limit=limit)
                    await record_sync_result(db, result)
                    await db.commit()

                logger.info(
                    f"Poll sync: {result.get('new_cases', 0)} new, "
                    f"{result.get('duplicates_skipped', 0)} dupes, "
                    f"{result.get('enqueued', 0)} enqueued"
                )
            except Exception as e:
                logger.error(f"Poll scheduler: sync failed: {e}")

            # Sleep for interval
            await asyncio.sleep(max(interval, 1) * 60)

        except asyncio.CancelledError:
            logger.info("Poll scheduler: cancelled")
            raise
        except Exception as e:
            logger.error(f"Poll scheduler: unexpected error: {e}")
            await asyncio.sleep(60)  # Back off on error


async def _get(db, key: str, default=None):
    """Read a setting value."""
    setting = await db.get(SystemSetting, key)
    return setting.value if setting else default
