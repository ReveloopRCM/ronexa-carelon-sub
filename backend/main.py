from contextlib import asynccontextmanager
import logging

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import analytics, auth, availity, cases, executions, jobs, queue, sessions, settings as settings_routes, signatures, sync, uploads
from app.core.settings import settings

logger = logging.getLogger(__name__)

RESTATE_ADMIN_URL = settings.RESTATE_ADMIN_URL
RESTATE_WORKER_URL = "http://localhost:9080"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Register Restate handler deployment on startup.
    # All Restate services run on the orchestrator (localhost:9080).
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

    # Start background poll scheduler
    from app.ingest.poll_scheduler import start_poll_scheduler, stop_poll_scheduler
    await start_poll_scheduler()

    yield

    # Shutdown poll scheduler
    await stop_poll_scheduler()


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
