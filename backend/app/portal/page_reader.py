"""PageReader — reads what the portal says after every action.

Simple DOM reader. No complex state machine. Just read messages,
detect results, and capture what the portal tells us.

Intelligence grows from volume — every message captured is data
the system learns from.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# Common ASP.NET error/info message selectors observed in Carelon portal
ERROR_SELECTORS = [
    "[id*='lblError']",
    "[id*='ErrorMessage']",
    "[id*='lblMessage']",
    ".ValidationSummary li",
    "[id*='pnlError']",
]

INFO_SELECTORS = [
    "[id*='lblInfo']",
    "[id*='lblSuccess']",
    "[id*='pnlInfo']",
]

# Grid selectors for search results
GRID_SELECTORS = {
    "member": "#asPrimary_ctl00_gvSearchMembers",
    "provider": "#asPrimary_ctl00_gvSearchProviders",
    "exams": "#asPrimary_ctl00_gvMemberExams",
}


@dataclass
class PageMessages:
    """What the portal is telling us right now."""
    error: str | None = None
    info: str | None = None
    raw_texts: list[str] = field(default_factory=list)


class PageReader:
    """Reads the portal DOM after every postback.

    Does NOT perform actions — only reads. Used by WebFormsClient
    to understand what happened after clicking/typing.
    """

    def __init__(self, page: Page):
        self.page = page

    async def read_messages(self) -> PageMessages:
        """Read any error or info messages currently visible on the page.

        Uses a single page.evaluate() call to batch all DOM reads.
        Retries once if navigation destroys the execution context.
        """
        import asyncio
        for attempt in range(2):
            try:
                return await self._read_messages_impl()
            except Exception as e:
                if "navigation" in str(e).lower() and attempt == 0:
                    logger.debug("Page navigated during read, retrying...")
                    await asyncio.sleep(2)
                    continue
                raise

    async def _read_messages_impl(self) -> PageMessages:
        """Internal: read messages from DOM."""
        result = await self.page.evaluate("""
            () => {
                const errors = [];
                const infos = [];
                const raw = [];

                // Error selectors
                const errorSels = %s;
                for (const sel of errorSels) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const text = (el.textContent || '').trim();
                        if (text && el.offsetParent !== null) {
                            errors.push(text);
                            raw.push(text);
                        }
                    }
                }

                // Info selectors
                const infoSels = %s;
                for (const sel of infoSels) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const text = (el.textContent || '').trim();
                        if (text && el.offsetParent !== null) {
                            infos.push(text);
                            raw.push(text);
                        }
                    }
                }

                return {
                    error: errors.length > 0 ? errors.join('; ') : null,
                    info: infos.length > 0 ? infos.join('; ') : null,
                    raw: raw
                };
            }
        """ % (str(ERROR_SELECTORS), str(INFO_SELECTORS)))

        messages = PageMessages(
            error=result.get("error"),
            info=result.get("info"),
            raw_texts=result.get("raw", []),
        )

        if messages.error:
            logger.warning(f"Portal error message: {messages.error}")
        if messages.info:
            logger.info(f"Portal info message: {messages.info}")

        return messages

    async def has_results(self, grid_name: str = "member") -> bool:
        """Check if a search results grid is visible and has rows.

        Args:
            grid_name: One of "member", "provider", "exams"
        """
        selector = GRID_SELECTORS.get(grid_name, grid_name)
        try:
            grid = await self.page.query_selector(selector)
            if not grid:
                return False
            # Check if grid has data rows (skip header row)
            rows = await grid.query_selector_all("tr")
            return len(rows) > 1  # header + at least 1 data row
        except Exception:
            return False

    async def results_count(self, grid_name: str = "member") -> int:
        """Count data rows in a search results grid.

        Returns 0 if grid not found or empty.
        """
        selector = GRID_SELECTORS.get(grid_name, grid_name)
        try:
            count = await self.page.evaluate("""
                (selector) => {
                    const grid = document.querySelector(selector);
                    if (!grid) return 0;
                    const rows = grid.querySelectorAll('tr');
                    return Math.max(0, rows.length - 1);  // exclude header
                }
            """, selector)
            return count
        except Exception:
            return 0

    async def read_text(self, selector: str) -> str | None:
        """Read text content of a single element.

        Returns None if element not found or not visible.
        """
        try:
            element = await self.page.query_selector(selector)
            if element:
                visible = await element.is_visible()
                if visible:
                    text = await element.text_content()
                    return (text or "").strip() or None
        except Exception:
            pass
        return None

    async def read_multiple(self, selectors: dict[str, str]) -> dict[str, str | None]:
        """Read text from multiple selectors in a single call.

        Args:
            selectors: dict of {key: css_selector}

        Returns dict of {key: text_or_none}
        """
        result = await self.page.evaluate("""
            (sels) => {
                const result = {};
                for (const [key, sel] of Object.entries(sels)) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) {
                        result[key] = (el.textContent || '').trim() || null;
                    } else {
                        result[key] = null;
                    }
                }
                return result;
            }
        """, selectors)
        return result

    async def wait_for_postback(self, timeout_ms: int = 45000) -> None:
        """Wait for ASP.NET full-page postback to complete.

        WebForms postbacks are full page reloads (form submit → server render → HTML).
        We wait for the page's 'load' event, then verify ViewState is present.
        """
        import asyncio
        try:
            # Wait for page load event (full page reload from postback)
            await self.page.wait_for_load_state("load", timeout=timeout_ms)
        except Exception:
            pass

        # Wait for ViewState — confirms ASP.NET rendered the page
        # Note: __VIEWSTATE is type="hidden", so use state="attached" not "visible"
        try:
            await self.page.wait_for_selector(
                "[name='__VIEWSTATE']", state="attached", timeout=timeout_ms
            )
        except Exception as e:
            err_name = type(e).__name__
            if "TargetClosedError" in err_name or "closed" in str(e).lower():
                logger.warning("Postback wait: page/browser closed during navigation")
                raise
            logger.warning("Postback wait: ViewState not found")

        # Brief settle time for client-side JS initialization
        await asyncio.sleep(0.3)

    async def capture_page_context(self) -> dict:
        """Capture full page context for debugging/learning.

        Returns page title, URL, visible text summary, and any messages.
        Useful when hitting an unexpected state — log this to learn from it.
        """
        result = await self.page.evaluate("""
            () => {
                return {
                    title: document.title || '',
                    url: window.location.href,
                    visibleText: document.body ? document.body.innerText.substring(0, 2000) : '',
                };
            }
        """)
        messages = await self.read_messages()
        result["error"] = messages.error
        result["info"] = messages.info
        return result
