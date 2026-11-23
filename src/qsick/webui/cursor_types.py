"""The cursor types the WebUI can draw, and what each one means.

Kept on the Python side so the settings page renders a real table instead of a hardcoded block, and so a test can
compare this list against `static/cursor.js` -- drift between "what the UI claims" and "what the code draws" is the
kind of bug that only shows up as a missing pointer in a corner of the app.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CursorType:
    name: str
    label: str
    where: str
    #: True for the shapes a page reaches without any markup of its own.
    automatic: bool = True


#: Every type in the order the settings table lists them: the ones you meet first, then the ones that need markup.
CURSOR_TYPES: tuple[CursorType, ...] = (
    CursorType("default", "箭头", "页面空白处"),
    CursorType("pointer", "手形", "链接、按钮、下拉、开关"),
    CursorType("text", "文本 I 形", "输入框、文本域、可编辑区域"),
    CursorType("vertical-text", "竖排文本", "竖排文字区域", automatic=False),
    CursorType("progress", "进行中", "aria-busy=\"true\"、body.is-busy"),
    CursorType("wait", "等待（锁定）", "body.is-wait", automatic=False),
    CursorType("help", "帮助", "带 title 的缩写词、data-cursor-help"),
    CursorType("crosshair", "十字", "框选、取色区域", automatic=False),
    CursorType("not-allowed", "禁止", "禁用的控件"),
    CursorType("no-drop", "不可放置", "拖放非法目标", automatic=False),
    CursorType("grab", "可抓取", "draggable 元素"),
    CursorType("grabbing", "抓取中", "body.is-dragging"),
    CursorType("move", "移动", "可拖动定位的元素", automatic=False),
    CursorType("all-scroll", "全向滚动", "可平移的视图", automatic=False),
    CursorType("cell", "单元格", "表格单元格选择", automatic=False),
    CursorType("context-menu", "上下文菜单", "右键菜单区域", automatic=False),
    CursorType("alias", "别名", "快捷方式", automatic=False),
    CursorType("copy", "复制", "复制目标", automatic=False),
    CursorType("zoom-in", "放大", "可放大的图像", automatic=False),
    CursorType("zoom-out", "缩小", "可缩小的图像", automatic=False),
    CursorType("col-resize", "列宽调整", "可拖动的列边界"),
    CursorType("row-resize", "行高调整", "可拖动的行边界", automatic=False),
)

TYPE_NAMES: tuple[str, ...] = tuple(item.name for item in CURSOR_TYPES)

#: Every type is automatic except the few marked otherwise -- the built-in styles draw all of them, they just need a
#: selector that can find them. This split exists for the docs table, not for behaviour.
AUTOMATIC_TYPES: tuple[str, ...] = tuple(item.name for item in CURSOR_TYPES if item.automatic)
