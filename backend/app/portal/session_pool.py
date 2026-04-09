"""Session Pool — per-NPI Playwright browser management with Redis.

One live browser per CenterNPI. Persistent Chromium profile ensures same
Akamai identity. Redis stores session metadata.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from playwright.async_api import async_playwright

from app.core.settings import settings
from app.portal.session import PlaywrightPortalSession, SessionExpiredException

logger = logging.getLogger(__name__)

# Session limits
MAX_SESSION_AGE_HOURS = 8
MAX_CASES_PER_SESSION = 80


class SessionPool:
    """Manages per-NPI Playwright browser sessions."""

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, PlaywrightPortalSession] = {}
        self._playwright = None

    async def _get_lock(self, center_npi: str) -> asyncio.Lock:
        """Per-NPI lock prevents concurrent launches for same NPI."""
        if center_npi not in self._locks:
            self._locks[center_npi] = asyncio.Lock()
        return self._locks[center_npi]

    async def get_session(
        self,
        center_npi: str,
        login_fn=None,
    ) -> PlaywrightPortalSession:
        """Get or create a session for a CenterNPI.

        Args:
            center_npi: The center NPI to get a session for.
            login_fn: Async callable(context, page) → (provider_id, client_id)
                      that performs full portal login including MFA.
        """
        lock = await self._get_lock(center_npi)
        async with lock:
            # Check for existing valid session
            if center_npi in self._sessions:
                session = self._sessions[center_npi]
                meta = await self._get_meta(center_npi)

                if meta and self._is_valid(meta):
                    # Validate session is alive
                    if await session.validate():
                        logger.info(f"Reusing session for NPI {center_npi}")
                        return session

                # Session expired or dead
                logger.info(f"Session for NPI {center_npi} expired, creating fresh")
                await session.close()
                del self._sessions[center_npi]

            # Create fresh session
            session = await self._create_session(center_npi, login_fn)
            self._sessions[center_npi] = session
            await self._set_meta(center_npi, {
                "created_at": time.time(),
                "case_count": 0,
            })
            return session

    async def increment(self, center_npi: str) -> None:
        """Increment case count after each case completes."""
        meta = await self._get_meta(center_npi)
        if meta:
            meta["case_count"] = meta.get("case_count", 0) + 1
            await self._set_meta(center_npi, meta)

    async def invalidate(self, center_npi: str) -> None:
        """Invalidate and close a session (on SessionExpiredException)."""
        if center_npi in self._sessions:
            await self._sessions[center_npi].close()
            del self._sessions[center_npi]
        if self._redis:
            await self._redis.delete(f"session:{center_npi}")

    def _is_valid(self, meta: dict) -> bool:
        """Check if session metadata indicates a valid session."""
        age_hours = (time.time() - meta.get("created_at", 0)) / 3600
        case_count = meta.get("case_count", 0)
        return age_hours < MAX_SESSION_AGE_HOURS and case_count < MAX_CASES_PER_SESSION

    async def _create_session(
        self,
        center_npi: str,
        login_fn=None,
    ) -> PlaywrightPortalSession:
        """Create a fresh Playwright session with persistent profile."""
        if not self._playwright:
            self._playwright = await async_playwright().start()

        profile_dir = Path(settings.BROWSER_PROFILES_DIR) / center_npi
        profile_dir.mkdir(parents=True, exist_ok=True)

        context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=settings.ENVIRONMENT != "local",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/Chicago",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        provider_id = None
        client_id = None

        if login_fn:
            provider_id, client_id = await login_fn(context, page)

        session = PlaywrightPortalSession(
            context=context,
            page=page,
            center_npi=center_npi,
            provider_id=provider_id,
            client_id=client_id,
        )

        logger.info(f"Created fresh session for NPI {center_npi}")
        return session

    async def _get_meta(self, center_npi: str) -> dict | None:
        if self._redis:
            data = await self._redis.get(f"session:{center_npi}")
            return json.loads(data) if data else None
        return None

    async def _set_meta(self, center_npi: str, meta: dict) -> None:
        if self._redis:
            await self._redis.set(
                f"session:{center_npi}",
                json.dumps(meta),
                ex=MAX_SESSION_AGE_HOURS * 3600,
            )

    async def close_all(self) -> None:
        """Shutdown: close all sessions and Playwright."""
        for npi, session in self._sessions.items():
            await session.close()
        self._sessions.clear()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
