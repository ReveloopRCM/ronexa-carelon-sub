from contextlib import asynccontextmanager
import logging
import traceback

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import analytics, auth, availity, cases, executions, jobs, queue, sessions, settings as settings_routes, signatures, sync, uploads
from app.core.settings import settings

logger = logging.getLogger(__name__)

RESTATE_ADMIN_URL = settings.RESTATE_ADMIN_URL
# Use the docker-compose service name so the URI is reachable from inside
# Restate's container. `localhost:9080` was wrong here — from Restate's own
# container, `localhost:9080` is its own loopback (Restate listens on 8080
# admin / 9070-9071), so deployment discovery hit "Connection refused" on
# every backend-api startup and flooded the runtime logs with META0003.
# `restate-handler:9080` is the same service the deploy script registers
# against, so they agree.
RESTATE_WORKER_URL = "http://restate-handler:9080"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Wrap the whole startup in a try/except so the underlying exception
    # surfaces in the logs. Hypercorn otherwise just emits a generic
    # "ASGI Framework Lifespan error, continuing without Lifespan support"
    # warning, which hides whatever actually threw.
    try:
        # Register Restate handler deployment on startup.
        # All Restate services run on the orchestrator's restate-handler
        # container at 9080 within the ronexa_ronexa_net bridge network.
        # Worker VMs are plain HTTP servers reached via WorkerSession handlers.
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    f"{RESTATE_ADMIN_URL}/deployments",
                    json={"uri": RESTATE_WORKER_URL, "force": True},
                    timeout=10.0,
                )
                if resp.status_code in (200, 201):
                    svcs = [s["name"] for s in resp.json().get("services", [])]
                    logger.info("Restate deployment registered (force=true): %s → %s", RESTATE_WORKER_URL, svcs)
                else:
                    logger.warning("Restate registration returned %s: %s", resp.status_code, resp.text[:200])
            except Exception as e:
                logger.warning("Could not register Restate deployment: %s", e)

            # Boot the Reaper VO — single-instance periodic claim/processing
            # sweeper, runs in Restate (not as a lifespan asyncio task) so it
            # survives backend-api restarts and is observable / killable like
            # any other Restate VO. Idempotent: Restate dedupes per-key sends.
            # The handler bounds itself to ~24h then exits cleanly so the
            # next backend-api startup re-triggers without unbounded queue.
            try:
                resp = await client.post(
                    f"{settings.RESTATE_URL}/Reaper/main/start/send",
                    json={},
                    timeout=10.0,
                )
                if resp.status_code in (200, 201, 202):
                    logger.info("Reaper started (POST /Reaper/main/start/send → %s)", resp.status_code)
                else:
                    logger.warning("Reaper start returned %s: %s", resp.status_code, resp.text[:200])
            except Exception as e:
                logger.warning("Could not start Reaper (non-fatal): %s", e)

        # Start background poll scheduler
        from app.ingest.poll_scheduler import start_poll_scheduler, stop_poll_scheduler
        await start_poll_scheduler()
    except Exception as e:
        # Log the full traceback so future startup failures aren't hidden
        # behind hypercorn's generic Lifespan-error warning.
        logger.error(
            "Lifespan startup failed: %s\n%s", e, traceback.format_exc()
        )
        raise

    yield

    # Shutdown poll scheduler
    try:
        await stop_poll_scheduler()
    except Exception as e:
        logger.error("Lifespan shutdown failed: %s\n%s", e, traceback.format_exc())


app = FastAPI(title="Ronexa", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://ronexa.centralus.cloudapp.azure.com",
        "http://carelon.ronexa.com",
        "https://carelon.ronexa.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth router — no auth required (login/logout/me)
app.include_router(auth.router)

# Protected routes — require valid session
from fastapi import Depends
from app.api.routes.auth import require_auth

protected = [
    uploads.router,
    analytics.router,
    availity.router,
    cases.router,
    executions.router,
    queue.router,
    jobs.router,
    sessions.router,
    settings_routes.router,
    signatures.router,
    sync.router,
]
for r in protected:
    app.include_router(r, dependencies=[Depends(require_auth)])


@app.get("/api/health")
async def health():
    return {"status": "ok", "environment": settings.ENVIRONMENT}


@app.get("/api/batches")
async def list_batches():
    from app.db.database import async_session_factory
    from app.db import repositories as repo
    async with async_session_factory() as db:
        batches = await repo.list_batches(db)
        return [
            {
                "id": b.id,
                "filename": b.filename,
                "uploaded_at": b.uploaded_at.isoformat() if b.uploaded_at else None,
                "uploaded_by": b.uploaded_by,
                "total_rows": b.total_rows,
                "duplicate_rows": b.duplicate_rows,
                "unique_cases": b.unique_cases,
                "stat_count": b.stat_count,
                "hold_count": b.hold_count,
            }
            for b in batches
        ]
