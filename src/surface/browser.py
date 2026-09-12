"""
Playwright Browser Surface
--------------------------
Implements the Surface interface for web apps — both modern and legacy.

Locator strategy priority (most → least stable):
  1. aria_label   — stable even if layout changes
  2. aria_role    — stable for semantic elements
  3. text_content — stable for visible labels
  4. placeholder  — stable for form fields
  5. css_selector — fragile but necessary for legacy apps
  6. xpath        — last resort for deeply nested tables
  7. screenshot_coord — absolute fallback (coordinates from discovery)

Design decisions:
- Uses Playwright's async API for non-blocking I/O
- Iframe handling: enter/exit frame context explicitly per step
- Wait strategy: explicit waits > networkidle (network-idle is unreliable on legacy apps)
- Screenshot on every hard failure for observability
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import (Browser, BrowserContext, Frame, Page,
                                   async_playwright)

from ..schema import ElementTarget, LocatorStrategy
from .base import (AmbiguousElementError, ElementNotFoundError,
                   Surface, SurfaceError)


class BrowserSurface(Surface):
    """
    Playwright-backed surface.

    Usage (via context manager):
        async with BrowserSurface.create(headless=False) as surf:
            surf.navigate("http://localhost:5001")
            ...
    """

    def __init__(self, page: Page, context: BrowserContext,
                 browser: Browser, playwright_instance):
        self._page = page
        self._context = context
        self._browser = browser
        self._pw = playwright_instance
        self._current_frame: Frame = page  # frame context for locating elements

    # ---- factory ----

    @classmethod
    async def create(cls, headless: bool = True,
                     slow_mo: int = 0) -> "BrowserSurface":
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=headless, slow_mo=slow_mo)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        return cls(page, context, browser, pw)

    # ---- locator resolution ----

    async def _resolve(self, target: ElementTarget):
        """Try locators in order, return the first that finds exactly one element."""
        for loc in target.locators:
            try:
                element = await self._resolve_one(loc)
                if element:
                    return element
            except Exception:
                continue
        raise ElementNotFoundError(
            f"No locator resolved for target. Tried: "
            f"{[l.strategy for l in target.locators]}\n"
            f"Snapshot: text='{target.snapshot_text}' role='{target.snapshot_role}'"
        )

    async def _resolve_one(self, loc):
        frame = self._current_frame

        # Enter iframe if specified
        if loc.frame:
            frame = await self._get_frame(loc.frame)

        if loc.strategy == LocatorStrategy.ARIA_LABEL:
            el = frame.locator(f'[aria-label="{loc.value}"]')
        elif loc.strategy == LocatorStrategy.ARIA_ROLE:
            parts = loc.value.split(":", 1)
            role, name = parts[0], parts[1] if len(parts) > 1 else None
            kwargs = {"name": name} if name else {}
            el = frame.get_by_role(role, **kwargs)
        elif loc.strategy == LocatorStrategy.TEXT_CONTENT:
            el = frame.get_by_text(loc.value, exact=False)
        elif loc.strategy == LocatorStrategy.PLACEHOLDER:
            el = frame.get_by_placeholder(loc.value)
        elif loc.strategy == LocatorStrategy.CSS_SELECTOR:
            el = frame.locator(loc.value)
        elif loc.strategy == LocatorStrategy.XPATH:
            el = frame.locator(f"xpath={loc.value}")
        elif loc.strategy == LocatorStrategy.SCREENSHOT_COORD:
            # Coordinate-based fallback — used directly, not via locator API
            return _CoordTarget(loc.x, loc.y)
        else:
            return None

        count = await el.count()
        if count == 0:
            return None
        if count > 1:
            # For text matches, take first visible
            el = el.first
        return el

    async def _get_frame(self, frame_selector: str) -> Frame:
        """Locate and return a child frame by CSS selector."""
        frame_element = await self._page.query_selector(frame_selector)
        if not frame_element:
            raise SurfaceError(f"Frame element not found: {frame_selector}")
        return await frame_element.content_frame()

    # ---- Surface interface ----

    def navigate(self, url: str, wait_for: Optional[str] = None) -> None:
        asyncio.get_event_loop().run_until_complete(
            self._navigate(url, wait_for)
        )

    async def _navigate(self, url: str, wait_for: Optional[str] = None):
        await self._page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        if wait_for:
            await self._page.wait_for_selector(wait_for, timeout=10_000)
        self._current_frame = self._page

    def click(self, target: ElementTarget) -> None:
        asyncio.get_event_loop().run_until_complete(self._click(target))

    async def _click(self, target: ElementTarget):
        el = await self._resolve(target)
        if isinstance(el, _CoordTarget):
            await self._page.mouse.click(el.x, el.y)
        else:
            await el.scroll_into_view_if_needed()
            await el.click()

    def type_text(self, target: ElementTarget, text: str,
                  clear_first: bool = True) -> None:
        asyncio.get_event_loop().run_until_complete(
            self._type_text(target, text, clear_first)
        )

    async def _type_text(self, target: ElementTarget, text: str, clear_first: bool):
        el = await self._resolve(target)
        if clear_first:
            await el.fill(text)
        else:
            await el.type(text)

    def select_option(self, target: ElementTarget, option: str) -> None:
        asyncio.get_event_loop().run_until_complete(
            self._select_option(target, option)
        )

    async def _select_option(self, target: ElementTarget, option: str):
        el = await self._resolve(target)
        await el.select_option(label=option)

    def wait_for(self, condition: str, timeout_ms: int = 10_000) -> None:
        asyncio.get_event_loop().run_until_complete(
            self._wait_for(condition, timeout_ms)
        )

    async def _wait_for(self, condition: str, timeout_ms: int):
        if condition == "network_idle":
            await self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
        elif condition.startswith("timeout:"):
            ms = int(condition.split(":")[1])
            await self._page.wait_for_timeout(ms)
        else:
            await self._page.wait_for_selector(condition, timeout=timeout_ms)

    def extract_text(self, target: ElementTarget,
                     attribute: str = "text_content") -> str:
        return asyncio.get_event_loop().run_until_complete(
            self._extract_text(target, attribute)
        )

    async def _extract_text(self, target: ElementTarget, attribute: str) -> str:
        el = await self._resolve(target)
        if attribute == "text_content":
            return (await el.text_content() or "").strip()
        elif attribute == "value":
            return await el.input_value()
        elif attribute == "href":
            return await el.get_attribute("href") or ""
        else:
            return await el.get_attribute(attribute) or ""

    def assert_condition(self, target: Optional[ElementTarget],
                         expected_text: Optional[str],
                         expected_url_contains: Optional[str]) -> bool:
        return asyncio.get_event_loop().run_until_complete(
            self._assert_condition(target, expected_text, expected_url_contains)
        )

    async def _assert_condition(self, target, expected_text, expected_url_contains) -> bool:
        if expected_url_contains:
            if expected_url_contains not in self._page.url:
                return False
        if target and expected_text:
            el = await self._resolve(target)
            text = (await el.text_content() or "").strip()
            if expected_text not in text:
                return False
        elif target:
            # Just check it's visible
            el = await self._resolve(target)
            if not await el.is_visible():
                return False
        return True

    def dismiss_if_present(self, trigger_selector: str,
                            dismiss_target: ElementTarget) -> bool:
        return asyncio.get_event_loop().run_until_complete(
            self._dismiss_if_present(trigger_selector, dismiss_target)
        )

    async def _dismiss_if_present(self, trigger_selector, dismiss_target) -> bool:
        el = self._page.locator(trigger_selector)
        if await el.count() > 0 and await el.first.is_visible():
            dismiss_el = await self._resolve(dismiss_target)
            await dismiss_el.click()
            return True
        return False

    def screenshot(self, path: Path) -> None:
        asyncio.get_event_loop().run_until_complete(
            self._page.screenshot(path=str(path), full_page=False)
        )

    def current_url(self) -> str:
        return self._page.url

    def page_accessibility_tree(self) -> str:
        tree = asyncio.get_event_loop().run_until_complete(
            self._page.accessibility.snapshot()
        )
        return _format_ax_tree(tree or {}, depth=0)

    def page_screenshot_b64(self) -> str:
        data = asyncio.get_event_loop().run_until_complete(
            self._page.screenshot(full_page=False)
        )
        return base64.b64encode(data).decode()

    def enter_frame(self, frame_selector: str) -> None:
        self._current_frame = asyncio.get_event_loop().run_until_complete(
            self._get_frame(frame_selector)
        )

    def exit_frame(self) -> None:
        self._current_frame = self._page

    def close(self) -> None:
        asyncio.get_event_loop().run_until_complete(self._cleanup())

    async def _cleanup(self):
        await self._browser.close()
        await self._pw.stop()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _CoordTarget:
    """Lightweight wrapper for coordinate-based click fallback."""
    def __init__(self, x: int, y: int):
        self.x = x
        self.y = y


def _format_ax_tree(node: dict, depth: int) -> str:
    """Format accessibility tree snapshot as compact text for LLM context."""
    if not node:
        return ""
    indent = "  " * depth
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")
    line = f"{indent}[{role}] {name}"
    if value:
        line += f" = {value!r}"
    lines = [line]
    for child in node.get("children", []):
        lines.append(_format_ax_tree(child, depth + 1))
    return "\n".join(lines)
