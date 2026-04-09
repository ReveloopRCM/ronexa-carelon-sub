"""Worker HTTP server entry point.

Runs on each worker VM (port 9081). Receives HTTP calls from the
thin WorkerSession Restate handlers on the orchestrator.

Usage:
    python worker_http.py

The worker needs these environment variables:
    DATABASE_URL    — PostgreSQL connection (same DB as orchestrator)
    REDIS_URL       — Redis connection (optional, for session pool)
    HEADLESS        — "true" for headless Chrome, "false" for visible (RDP)
    BROWSER_PROFILE_DIR — Path for persistent browser profiles
"""
import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


if __name__ == "__main__":
    import uvicorn
    from app.worker.http_server import app

    logger.info("Starting Worker HTTP server on port 9081")
    uvicorn.run(app, host="0.0.0.0", port=9081)
