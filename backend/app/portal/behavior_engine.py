"""Behavior Engine — gaussian timing, Bezier mouse paths, per-character typing.

Wraps ALL Playwright interactions. No raw page.click() or page.fill() anywhere else.
"""
from __future__ import annotations

import asyncio
import logging
import random

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# Action type → (mu_ms, sigma_ms) for gaussian think time
# Tuned for senior auth rep — fast but human. They know the portal
# but still glance at fields, verify data, move the mouse naturally.
# These timings avoid bot detection while maintaining throughput.
THINK_TIME_MS = {
    "pageLoad": (1200, 300),       # Page loads, rep orients, finds next action
    "formField": (600, 150),       # Glance at field, move mouse, click — ~0.5-0.8s
    "buttonClick": (500, 120),     # Find button, click — ~0.4-0.6s
    "searchResult": (800, 200),    # Scan result grid, verify, click — ~0.6-1.0s
    "betweenCases": (2500, 500),   # Breath between cases, pull up next
    # clinicalQuestion deliberately ABSENT — LLM latency IS the authentic think time
}

# Typing speed: senior rep types fast, ~75 WPM → ~80ms per char
TYPING_MU_MS = 80
TYPING_SIGMA_MS = 30

# Mouse movement — natural, direct movements (not robotic)
BEZIER_STEPS_MIN = 10
BEZIER_STEPS_MAX = 18


class BehaviorEngine:
    """Human-like browser interaction engine."""

    def __init__(self, page: Page):
        self.page = page

    async def think(self, action_type: str) -> None:
        """Async gaussian delay for navigation actions."""
        if action_type not in THINK_TIME_MS:
            return
        mu, sigma = THINK_TIME_MS[action_type]
        delay = random.gauss(mu, sigma)
        # Clamp to 2σ range to avoid extreme outliers
        delay = max(mu - 2 * sigma, min(mu + 2 * sigma, delay))
        delay = max(50, delay)  # never below 50ms
        logger.debug(f"think({action_type}): {delay:.0f}ms")
        await asyncio.sleep(delay / 1000)

    async def click(self, selector: str, action_type: str = "buttonClick") -> None:
        """Think + bezier move + click with offset."""
        await self.think(action_type)

        # Get element bounding box
        element = await self.page.wait_for_selector(selector, timeout=10000)
        box = await element.bounding_box()
        if not box:
            # Fallback to direct click
            await element.click()
            return

        # Click target offset [30%-70%] x/y — never dead-center
        tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)

        await self._bezier_move(tx, ty)
        await self.page.mouse.click(tx, ty)
        logger.debug(f"click({selector}) at ({tx:.0f}, {ty:.0f})")

    async def type_text(self, selector: str, text: str) -> None:
        """Click field + per-character typing with gaussian inter-keystroke delay."""
        await self.click(selector, action_type="formField")

        # Clear existing content — set value empty via JS, then dispatch input event
        el = await self.page.query_selector(selector)
        if el:
            current_val = await el.input_value()
            if current_val:
                await el.fill("")
                await asyncio.sleep(0.05)

        # Type per-character
        for char in text:
            await self.page.keyboard.type(char, delay=0)
            delay = random.gauss(TYPING_MU_MS, TYPING_SIGMA_MS)
            delay = max(TYPING_MU_MS - 2 * TYPING_SIGMA_MS,
                        min(TYPING_MU_MS + 2 * TYPING_SIGMA_MS, delay))
            delay = max(30, delay)
            await asyncio.sleep(delay / 1000)

        logger.debug(f"type_text({selector}): {len(text)} chars")

    async def select_option(self, selector: str, value: str) -> None:
        """Select an option from a dropdown."""
        await self.think("formField")
        await self.page.select_option(selector, value=value)
        logger.debug(f"select_option({selector}): {value}")

    async def _bezier_move(self, tx: float, ty: float) -> None:
        """Cubic bezier mouse path to target."""
        # Get current mouse position (approximate from viewport center if unknown)
        steps = random.randint(BEZIER_STEPS_MIN, BEZIER_STEPS_MAX)

        # Start from a slightly offset position
        sx = tx + random.uniform(-200, 200)
        sy = ty + random.uniform(-200, 200)

        # Random control points for cubic bezier
        cp1x = sx + (tx - sx) * random.uniform(0.2, 0.5) + random.uniform(-50, 50)
        cp1y = sy + (ty - sy) * random.uniform(0.2, 0.5) + random.uniform(-50, 50)
        cp2x = sx + (tx - sx) * random.uniform(0.5, 0.8) + random.uniform(-30, 30)
        cp2y = sy + (ty - sy) * random.uniform(0.5, 0.8) + random.uniform(-30, 30)

        for i in range(steps + 1):
            t = i / steps
            # Cubic bezier formula
            x = ((1 - t) ** 3 * sx +
                 3 * (1 - t) ** 2 * t * cp1x +
                 3 * (1 - t) * t ** 2 * cp2x +
                 t ** 3 * tx)
            y = ((1 - t) ** 3 * sy +
                 3 * (1 - t) ** 2 * t * cp1y +
                 3 * (1 - t) * t ** 2 * cp2y +
                 t ** 3 * ty)
            await self.page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.005, 0.015))
