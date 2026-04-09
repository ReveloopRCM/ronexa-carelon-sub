#!/usr/bin/env python3
"""One-shot login test for a specific worker.

Usage:
    python scripts/test_login.py vm-worker-b
    python scripts/test_login.py vm-worker-c

Pulls credentials from the DB, launches a headless browser, attempts
ONE login to the Carelon portal, and reports success or failure.
Does NOT retry on failure — exits immediately with a clear message.
"""
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger("test_login")


async def main(worker_id: str):
    # ── 1. Load creds from DB ──
    from sqlalchemy import select
    from app.db.database import async_session_factory
    from app.db.models import WorkerAccount

    async with async_session_factory() as session:
        row = (await session.execute(
            select(WorkerAccount).where(WorkerAccount.container_id == worker_id)
        )).scalar_one_or_none()

    if not row:
        logger.error(f"Worker '{worker_id}' not found in DB")
        sys.exit(1)

    username = row.username
    password = row.password
    mailbox = row.mailbox_address
    logger.info(f"Worker:   {worker_id}")
    logger.info(f"Username: {username}")
    logger.info(f"Password: {'*' * (len(password) - 2) + password[-2:] if password else '(empty — will use env)'}")
    logger.info(f"Mailbox:  {mailbox}")

    if not password:
        from app.core.settings import settings
        password = settings.CARELON_PASSWORD
        logger.info("Using CARELON_PASSWORD from env (DB password empty)")

    # ── 2. Launch browser ──
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        page = await context.new_page()

        # ── 3. Single login attempt ──
        try:
            from app.auth.okta_login import okta_login
            from app.portal.behavior_engine import BehaviorEngine

            behavior = BehaviorEngine()
            provider_id, client_id = await okta_login(
                page, behavior,
                username=username,
                password=password,
                mailbox=mailbox,
            )

            # ── 4. Check result ──
            content = await page.content()
            indicators = ["Start Your Order", "Find This Member", "Logout", "My Homepage"]
            found = [i for i in indicators if i.lower() in content.lower()]

            if found:
                logger.info(f"✅ LOGIN SUCCESS — Dashboard indicators: {found}")
                logger.info(f"   URL: {page.url}")
                logger.info(f"   Provider ID: {provider_id}")
                logger.info(f"   Client ID: {client_id}")
            else:
                await page.screenshot(path=f"/tmp/test_login_{worker_id}.png")
                logger.error(f"❌ LOGIN FAILED — No dashboard indicators found")
                logger.error(f"   URL: {page.url}")
                logger.error(f"   Screenshot: /tmp/test_login_{worker_id}.png")
                # Dump visible text for diagnosis
                text = await page.evaluate("() => document.body?.innerText?.substring(0, 500) || ''")
                logger.error(f"   Page text: {text[:300]}")

        except Exception as e:
            await page.screenshot(path=f"/tmp/test_login_{worker_id}.png")
            logger.error(f"❌ LOGIN EXCEPTION — {type(e).__name__}: {e}")
            logger.error(f"   Screenshot: /tmp/test_login_{worker_id}.png")

        finally:
            await browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_login.py <worker-id>")
        print("  e.g. python scripts/test_login.py vm-worker-b")
        sys.exit(1)

    asyncio.run(main(sys.argv[1]))
