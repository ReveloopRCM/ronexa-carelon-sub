"""Okta IDX Login — PKCE OAuth2 + MFA flow for Carelon portal.

Handles the full login flow:
  1. Carelon "User Confirmation" page → enter username → click Next
  2. Okta Sign-In Widget loads → click Next (username pre-filled)
  3. Enter password → click Login
  4. Handle MFA — click "Send Email" → get OTP via Graph → enter code → verify
  5. Wait for exchange redirect
  6. Handle HIPAA disclaimer
  7. Verify dashboard

Reference: carelon-sub/apps/worker/auth/playwright_login.py (proven working)
"""
from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Page

from app.auth.mfa_resolver import MFAResolver
from app.core.settings import settings
from app.portal.behavior_engine import BehaviorEngine

logger = logging.getLogger(__name__)

PORTAL_URL = "https://www.providerportal.com"


async def okta_login(
    page: Page,
    behavior: BehaviorEngine,
    username: str | None = None,
    password: str | None = None,
    mailbox: str | None = None,
) -> tuple[str | None, str | None]:
    """Perform full Carelon + Okta IDX login with MFA.

    Returns (provider_id, client_id) from the authenticated portal session.
    """
    username = username or settings.CARELON_USERNAME
    password = password or settings.CARELON_PASSWORD
    _mailbox = mailbox  # Passed to MFA resolver

    logger.info("Starting login flow")

    # ── Step 1: Load login page ──
    await page.goto(
        f"{PORTAL_URL}/Default.aspx",
        wait_until="domcontentloaded",
        timeout=30000,
    )
    await page.wait_for_selector("#asPrimary_ctl00_txtLoginId", timeout=15000)
    logger.info("Carelon login page loaded")

    # ── Step 2: Username + first Next ──
    await page.fill("#asPrimary_ctl00_txtLoginId", username)
    await _try_click(page, [
        "#asPrimary_ctl00_btnLookup",
        'input[name="asPrimary$ctl00$btnLookup"]',
        'input[value="Next"]',
    ])
    logger.info("Username entered, clicked Next")

    # ── Step 3: Okta widget — second Next ──
    # Senior rep: we know Okta is coming. Wait for it, then go.
    try:
        await page.wait_for_selector("#okta-sign-in", timeout=15000)
        logger.info("Okta Sign-In Widget loaded")
    except Exception:
        logger.info("Okta widget container not found, continuing")

    try:
        await page.wait_for_selector(
            'input[name="identifier"][readonly]', timeout=10000
        )
        logger.info("Okta identifier field found (readonly, pre-filled)")
    except Exception:
        logger.info("Okta identifier field not readonly, continuing")

    await asyncio.sleep(0.5)
    await _try_click(page, [
        'input[type="submit"][value="Next"]',
        '.button.button-primary[value="Next"]',
        'input.button-primary[data-type="save"][value="Next"]',
    ])
    logger.info("Clicked Okta Next")

    # ── Step 4: Password + Login ──
    # Wait for password field to appear — no fixed sleep
    filled = False
    for attempt in range(3):
        if attempt > 0:
            logger.info(f"Password field retry attempt {attempt + 1}")
            await page.reload(wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
        # Wait for password field to render
        try:
            await page.wait_for_selector(
                'input[name="credentials.passcode"], input[type="password"]',
                timeout=10000,
            )
        except Exception:
            logger.info("Password field not yet visible, retrying")
            continue
        filled = await _try_fill(page, [
            'input[name="credentials.passcode"]',
            'input[type="password"]',
        ], password)
        if filled:
            break

    if not filled:
        await page.screenshot(path="/tmp/password_field_not_found.png")
        raise RuntimeError("Could not find password field after 3 attempts")

    logger.info("Password entered")
    await _try_click(page, [
        'input[type="submit"][value="Login"]',
        '.button.button-primary[value="Login"]',
        'input[type="submit"]',
    ])
    await asyncio.sleep(1)
    logger.info("Clicked Login")

    # ── Step 5: Handle MFA ──
    await _handle_mfa(page, mailbox=_mailbox)

    # ── Step 6: Wait for exchange ──
    # After MFA, portal shows "Login Verification" spinner briefly.
    logger.info("Waiting for exchange redirect...")
    try:
        await page.wait_for_function(
            """() => {
                if (!document.body) return false;
                const text = document.body.innerText.toLowerCase();
                return !text.includes('login verification') &&
                       !document.querySelector('.waitHeader');
            }""",
            timeout=30000,
        )
    except Exception as e:
        logger.warning(f"Exchange wait issue: {e}")
    await asyncio.sleep(0.5)

    # ── Step 7: HIPAA disclaimer ──
    content = await page.content()
    if "hipaa" in content.lower() and "disclaimer" in content.lower():
        logger.info("HIPAA disclaimer detected, clicking I Agree")
        await _try_click(page, [
            "#asPrimary_ctl00_cmdAgreeContinue",
            "input[value='I Agree']",
            "text=I Agree",
        ])
        await asyncio.sleep(1)

    # ── Step 8: Verify dashboard ──
    content = await page.content()
    indicators = ["Start Your Order", "Find This Member", "Logout", "My Homepage"]
    found = [i for i in indicators if i.lower() in content.lower()]

    if not found:
        await page.screenshot(path="/tmp/dashboard_not_reached.png")
        logger.warning(f"Dashboard not reached. URL: {page.url}")
    else:
        logger.info(f"Dashboard verified! Indicators: {found}")

    await page.screenshot(path="/tmp/login_success.png")
    logger.info(f"Login complete. URL: {page.url}")

    # ── Extract provider/client IDs ──
    provider_id = None
    client_id = None

    try:
        session_data = await page.evaluate("""
            () => {
                if (window.__SESSION__) return window.__SESSION__;
                if (window.sessionData) return window.sessionData;
                const provEl = document.querySelector('[id*="ProviderId"], [id*="providerId"]');
                const clientEl = document.querySelector('[id*="ClientId"], [id*="clientId"]');
                return {
                    providerId: provEl ? provEl.value || provEl.textContent : null,
                    clientId: clientEl ? clientEl.value || clientEl.textContent : null,
                };
            }
        """)
        if session_data:
            provider_id = session_data.get("providerId")
            client_id = session_data.get("clientId")
    except Exception:
        logger.warning("Could not extract session context from page")

    return provider_id, client_id


async def _handle_mfa(page: Page, mailbox: str | None = None):
    """Handle email MFA if the portal shows the Send Email Verification page.

    Uses prepare/wait pattern: flush stale OTP emails BEFORE clicking
    'Send Email', then poll for the fresh OTP after.
    """
    logger.info("Checking for MFA...")
    await asyncio.sleep(1)

    # Check for MFA page indicators
    mfa_found = False
    for indicator in ['text=Send Email', 'text=Email Verification']:
        try:
            el = await page.query_selector(indicator)
            if el:
                mfa_found = True
                break
        except Exception:
            continue

    if not mfa_found:
        logger.info("No MFA page detected, continuing")
        return

    logger.info("MFA page detected")

    # IMPORTANT: Flush stale OTP emails BEFORE triggering "Send Email"
    # This prevents picking up old codes from previous login attempts
    resolver = MFAResolver(mailbox=mailbox)
    await resolver.prepare()
    logger.info("Stale OTP emails flushed, cutoff set")

    # NOW click "Send Email" to trigger fresh OTP delivery
    await _try_click(page, [
        'input[type="submit"][value="Send Email"]',
        '.button.button-primary[value="Send Email"]',
        'text=Send Email',
    ])
    logger.info("Clicked 'Send Email' — OTP email triggered")
    await asyncio.sleep(5)

    # Wait for OTP input field to appear
    code_selectors = [
        'input[name="credentials.passcode"]',
        '#input103',
        'input[type="text"][autocomplete="off"]',
    ]
    otp_field_found = False
    for _ in range(6):
        for sel in code_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    otp_field_found = True
                    break
            except Exception:
                continue
        if otp_field_found:
            break
        await asyncio.sleep(5)

    if not otp_field_found:
        await page.screenshot(path="/tmp/mfa_otp_field_not_found.png")
        logger.warning("OTP input field not found after waiting")

    # Get fresh OTP (only emails received after prepare() cutoff)
    try:
        otp = await resolver.wait_for_otp()
        logger.info(f"Fresh OTP resolved: ***{otp[-2:]}")

        # Enter OTP
        await _try_fill(page, code_selectors, otp)

        # Click Verify
        await _try_click(page, [
            'input[type="submit"][value="Verify Code"]',
            'input[type="submit"]',
            '.button.button-primary',
        ])
        await asyncio.sleep(1)
        logger.info("OTP submitted")

    except Exception as e:
        logger.error(f"OTP auto-resolve failed: {e}")
        # Wait for manual MFA completion as fallback
        logger.info("Waiting for manual MFA completion...")
        for _ in range(120):
            content = await page.content()
            lower = content.lower()
            if any(x in lower for x in [
                "start your order", "my homepage",
                "hipaa", "login verification",
            ]):
                break
            await asyncio.sleep(2)
        logger.info("MFA appears resolved (manual or auto)")


async def _try_click(page: Page, selectors: list[str]) -> bool:
    """Try clicking with multiple selector fallbacks."""
    for sel in selectors:
        try:
            if sel.startswith("text="):
                await page.click(sel, timeout=3000)
                return True
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                return True
        except Exception:
            continue
    return False


async def _try_fill(page: Page, selectors: list[str], value: str) -> bool:
    """Try filling with multiple selector fallbacks."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(value)
                return True
        except Exception:
            continue
    return False
