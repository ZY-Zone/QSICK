"""The cursor states the WebUI can actually show.

This list is deliberately short. A cursor pack is work for whoever authors it -- an image plus a hotspot per state --
and every state that the interface can never display is work spent on something nobody will see, followed by the
suspicion that the feature is broken. So the list holds only states that a real element in this app can put on
screen without anyone adding markup:

  * `default`     -- the page itself
  * `pointer`     -- a, button, select, summary, label, checkboxes
  * `text`        -- input, textarea
  * `not-allowed` -- :disabled

Everything else that a browser supports (`grab`, `progress`, `zoom-in`, `col-resize`, ...) is reachable in HTML, but
nothing in QSICK produces it today. Adding one back is a two-line change in `static/cursor.js` plus an entry here --
worth doing the moment there is an element that needs it, not before.

`webui/static/cursor.js` carries the same four names; `tests/test_webui_cursor.py` keeps the two in step.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CursorState:
    name: str
    label: str
    where: str


CURSOR_STATES: tuple[CursorState, ...] = (
    CursorState("default", "箭头", "页面空白处"),
    CursorState("pointer", "手形", "链接、按钮、下拉、开关"),
    CursorState("text", "文本 I 形", "输入框、文本域"),
    CursorState("not-allowed", "禁止", "禁用的控件"),
)

STATE_NAMES: tuple[str, ...] = tuple(item.name for item in CURSOR_STATES)
