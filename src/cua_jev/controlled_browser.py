"""Local Chromium acceptance adapter, not a Windows UIA or VLM implementation.

DOM semantics stand in for UIA observations. GUI execution sends real Chromium
mouse/keyboard input at freshly measured control bounds. No network is needed.
"""
from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from .vision import WindowImage


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int


class BrowserControl:
    def __init__(self, locator: Any, index: int) -> None:
        self.locator = locator
        self.index = index
        tag = locator.evaluate("e => e.tagName.toLowerCase()")
        self.element_info = SimpleNamespace(
            control_type={"input": "Edit", "textarea": "Edit", "button": "Button"}.get(tag, "Text"),
            automation_id=locator.get_attribute("id") or "", is_password=False,
        )

    def window_text(self) -> str:
        return self.locator.get_attribute("aria-label") or self.locator.inner_text()

    def is_visible(self) -> bool:
        return self.locator.is_visible()

    def is_enabled(self) -> bool:
        return self.locator.is_enabled()

    def rectangle(self) -> Bounds:
        box = self.locator.bounding_box()
        if box is None:
            return Bounds(0, 0, 0, 0)
        return Bounds(round(box["x"]), round(box["y"]),
                      round(box["x"] + box["width"]), round(box["y"] + box["height"]))

    def set_edit_text(self, value: str) -> None:
        self.locator.fill(value)

    def get_value(self) -> str:
        return self.locator.input_value() if self.element_info.control_type == "Edit" else ""


class BrowserWindow:
    """UIA-shaped read-only wrappers; all normal actions use GUI input."""
    handle = 1

    def __init__(self, page: Any) -> None:
        self.page = page

    def window_text(self) -> str:
        return self.page.title()

    def rectangle(self) -> Bounds:
        size = self.page.viewport_size
        return Bounds(0, 0, size["width"], size["height"])

    def descendants(self) -> list[BrowserControl]:
        # Generic semantic controls, never fixture IDs or a vendor-specific script.
        items = self.page.locator('button, input:not([type=password]), textarea, [role=status], h1, h2')
        return [BrowserControl(items.nth(index), index) for index in range(items.count())]


class BrowserScreen:
    def __init__(self, page: Any, *, before_input: Callable[[], None] | None = None) -> None:
        self.page = page
        self._before_input = before_input or (lambda: None)
        self.focused = True
        self.inputs: list[dict[str, Any]] = []

    def ensure_foreground(self, handle: int) -> None:
        if handle != 1 or not self.focused or self.page.is_closed():
            raise RuntimeError("controlled browser lost focus or identity")

    def click_point(self, x: float, y: float) -> None:
        self.ensure_foreground(1)
        self._before_input()
        self.page.mouse.click(x, y)
        self.inputs.append({"operation": "click", "x": x, "y": y})

    def hotkey(self, *keys: str) -> None:
        self.ensure_foreground(1)
        self._before_input()
        self.page.keyboard.press("+".join("Control" if key == "ctrl" else key for key in keys))
        self.inputs.append({"operation": "hotkey", "keys": list(keys)})

    def paste_text(self, value: str) -> None:
        self.ensure_foreground(1)
        self._before_input()
        self.page.keyboard.insert_text(value)
        self.inputs.append({"operation": "text", "length": len(value)})

    def capture_region(self, region: tuple[int, int, int, int]) -> WindowImage:
        from PIL import Image
        left, top, width, height = region
        jpeg = self.page.screenshot(type="jpeg", clip={"x": left, "y": top, "width": width, "height": height})
        sample = Image.open(io.BytesIO(jpeg)).convert("L").resize((64, 64)).tobytes()
        return WindowImage(jpeg, sample, region)
