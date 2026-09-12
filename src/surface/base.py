"""
Abstract Surface Interface
--------------------------
Decouples the artifact schema (what to do) from the surface implementation
(how to do it on a specific technology).

The seam: a Step's ElementTarget and action type go in; a surface-specific
call goes out. The replay engine speaks only to this interface, so swapping
from Playwright (browser) to pywinauto (Windows desktop) or appium (mobile)
requires only a new Surface subclass — the artifact schema is unchanged.

Surface types in scope:
  BrowserSurface  — Playwright, covers modern + legacy web (including iframes)
  [future] AccessibilityTreeSurface — pywinauto / AT-SPI for desktop apps
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..schema import ElementTarget, LocatorStrategy


class SurfaceError(Exception):
    """Raised when a surface operation fails unrecoverably."""
    pass

class ElementNotFoundError(SurfaceError):
    """No locator in an ElementTarget resolved to an element."""
    pass

class AmbiguousElementError(SurfaceError):
    """A locator resolved to multiple elements."""
    pass


class Surface(ABC):
    """Abstract surface. Subclasses implement the concrete mechanics."""

    @abstractmethod
    def navigate(self, url: str, wait_for: Optional[str] = None) -> None: ...

    @abstractmethod
    def click(self, target: ElementTarget) -> None: ...

    @abstractmethod
    def type_text(self, target: ElementTarget, text: str,
                  clear_first: bool = True) -> None: ...

    @abstractmethod
    def select_option(self, target: ElementTarget, option: str) -> None: ...

    @abstractmethod
    def wait_for(self, condition: str, timeout_ms: int = 10_000) -> None: ...

    @abstractmethod
    def extract_text(self, target: ElementTarget,
                     attribute: str = "text_content") -> str: ...

    @abstractmethod
    def assert_condition(self, target: Optional[ElementTarget],
                         expected_text: Optional[str],
                         expected_url_contains: Optional[str]) -> bool: ...

    @abstractmethod
    def dismiss_if_present(self, trigger_selector: str,
                            dismiss_target: ElementTarget) -> bool:
        """Returns True if a dialog was present and dismissed."""
        ...

    @abstractmethod
    def screenshot(self, path: Path) -> None: ...

    @abstractmethod
    def current_url(self) -> str: ...

    @abstractmethod
    def page_accessibility_tree(self) -> str:
        """Return a text snapshot of the accessibility tree for the LLM."""
        ...

    @abstractmethod
    def page_screenshot_b64(self) -> str:
        """Return a base64-encoded PNG screenshot for the LLM."""
        ...

    @abstractmethod
    def enter_frame(self, frame_selector: str) -> None: ...

    @abstractmethod
    def exit_frame(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...
