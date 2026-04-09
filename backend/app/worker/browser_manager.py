"""Browser management — Playwright browser lifecycle for worker VMs.

Manages browser instances, contexts, and pages per worker_id.
All browser management is centralized here — no Restate dependency.

Exports used by helpers.py and http_server.py:
  - _BROWSER_REGISTRY: dict[str, PortalSession] — active sessions per worker
  - _PLAYWRIGHT_INSTANCE: global Playwright instance (one per process)
  - _ensure_session_direct(worker_id, credentials) — create/reuse/re-login
  - _close_browser(worker_id) — close browser for a worker
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Global State ──

_PLAYWRIGHT_INSTANCE: Any = None
_BROWSER_REGISTRY: dict[str, "PortalSession"] = {}
_SESSION_TTL_SECONDS = 4 * 60 * 60  # 4 hours — force fresh login after this


@dataclass
class PortalSession:
    """A browser session connected to the Carelon portal."""
    worker_id: str
    browser: Any = None          # Playwright Browser
    context: Any = None          # Playwright BrowserContext (ephemeral)
    page: Any = None             # Playwright Page
    behavior: Any = None         # BehaviorEngine instance
    logged_in: bool = False
    provider_id: str | None = None
    client_id: str | None = None
    created_at: float = 0


async def _get_playwright():
    """Get or create the global Playwright instance."""
    global _PLAYWRIGHT_INSTANCE
    if _PLAYWRIGHT_INSTANCE is None:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        _PLAYWRIGHT_INSTANCE = pw
    return _PLAYWRIGHT_INSTANCE


async def _ensure_session_direct(
    worker_id: str,
    credentials: dict | None = None,
) -> PortalSession:
    """Ensure a logged-in portal session exists for this worker.

    Creates a new browser if needed, or reuses existing one.
    Re-logins if the session has expired.
    """
    import time

    session = _BROWSER_REGISTRY.get(worker_id)

    # Check if existing session is still alive and within TTL
    if session and session.page:
        age = time.time() - session.created_at
        if age > _SESSION_TTL_SECONDS:
            logger.info(f"Worker/{worker_id}: session TTL expired ({age/3600:.1f}h), closing")
            await _close_browser(worker_id)
            session = None
        else:
            try:
                # Quick check — try to access the page
                await session.page.evaluate("() => document.readyState")
                # Check if still logged in (not on login page)
                url = session.page.url
                if "Default.aspx" in url or "providerportal.com" in url:
                    if session.logged_in:
                        logger.info(f"Worker/{worker_id}: reusing existing session ({age/60:.0f}m old)")
                        return session
            except Exception as e:
                logger.warning(f"Worker/{worker_id}: existing session dead ({e}), creating new")
                await _close_browser(worker_id)
                session = None

    # Create new ephemeral browser — no persistent profile, always fresh
    pw = await _get_playwright()

    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )

    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        accept_downloads=True,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )

    page = await context.new_page()

    session = PortalSession(
        worker_id=worker_id,
        browser=browser,
        context=context,
        page=page,
        logged_in=False,
        created_at=time.time(),
    )
    _BROWSER_REGISTRY[worker_id] = session

    # Login
    from app.auth.okta_login import okta_login
    from app.portal.behavior_engine import BehaviorEngine

    behavior = BehaviorEngine(page)
    session.behavior = behavior

    username = (credentials or {}).get("username")
    password = (credentials or {}).get("password")
    mailbox = (credentials or {}).get("mailbox")

    provider_id, client_id = await okta_login(
        page, behavior,
        username=username,
        password=password,
        mailbox=mailbox,
    )

    session.logged_in = True
    session.provider_id = provider_id
    session.client_id = client_id

    logger.info(
        f"Worker/{worker_id}: new session created "
        f"(provider_id={provider_id}, client_id={client_id})"
    )
    return session


async def _close_browser(worker_id: str) -> None:
    """Close the browser for a worker and remove from registry."""
    session = _BROWSER_REGISTRY.pop(worker_id, None)
    if not session:
        return

    try:
        if session.context:
            await session.context.close()
    except Exception as e:
        logger.warning(f"Worker/{worker_id}: error closing context: {e}")

    try:
        if session.browser:
            await session.browser.close()
    except Exception as e:
        logger.warning(f"Worker/{worker_id}: error closing browser: {e}")

    session.logged_in = False
    session.page = None
    session.context = None
    session.browser = None
    logger.info(f"Worker/{worker_id}: browser closed")
