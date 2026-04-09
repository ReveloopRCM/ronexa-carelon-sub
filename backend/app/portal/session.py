"""PlaywrightPortalSession — browser session with in-page fetch() API calls.

ALL ClinicalFacade API calls run via fetch() inside the live Playwright page.
NO httpx. NO external HTTP from Python to the portal.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from playwright.async_api import BrowserContext, Page

from app.portal.behavior_engine import BehaviorEngine

logger = logging.getLogger(__name__)


class PlaywrightPortalSession:
    """A live Playwright browser session for a specific CenterNPI."""

    def __init__(
        self,
        context: BrowserContext,
        page: Page,
        center_npi: str,
        provider_id: str | None = None,
        client_id: str | None = None,
    ):
        self.context = context
        self.page = page
        self.center_npi = center_npi
        self.provider_id = provider_id
        self.client_id = client_id
        self.behavior = BehaviorEngine(page)

    async def api(self, endpoint: str, payload: dict[str, Any] | None = None) -> dict:
        """Execute an API call via in-page fetch().

        This runs fetch() inside the live browser page so Akamai never sees
        the API traffic — it runs in the trusted browser context.
        """
        payload_json = json.dumps(payload or {})

        result = await self.page.evaluate(f"""
            async () => {{
                const resp = await fetch('/ClinicalFacade.aspx/{endpoint}', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json',
                               'X-Requested-With': 'XMLHttpRequest'}},
                    body: JSON.stringify({payload_json}),
                }});
                return {{ ok: resp.ok, status: resp.status, body: await resp.text() }};
            }}
        """)

        if not result["ok"]:
            logger.error(
                f"API call {endpoint} failed: status={result['status']}, "
                f"body={result['body'][:200]}"
            )
            raise PortalAPIError(endpoint, result["status"], result["body"])

        try:
            body = json.loads(result["body"])
        except json.JSONDecodeError:
            body = {"raw": result["body"]}

        logger.debug(f"API {endpoint}: status={result['status']}")
        return body

    async def validate(self) -> bool:
        """Check if session is still alive by calling GetClientMessages."""
        try:
            result = await self.api("GetClientMessages", {})
            return result is not None
        except Exception:
            return False

    async def close(self) -> None:
        """Close the browser context."""
        try:
            await self.context.close()
        except Exception:
            pass


class PortalAPIError(Exception):
    def __init__(self, endpoint: str, status: int, body: str):
        self.endpoint = endpoint
        self.status = status
        self.body = body
        super().__init__(f"Portal API error: {endpoint} returned {status}")


class SessionExpiredException(Exception):
    pass
