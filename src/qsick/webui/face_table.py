"""QQ 系统表情索引。

QQ 的系统表情（face id）是一份历史稳定、公开的固定集合。这里内置索引本身，
不依赖网络或 OIDB 包通道：

- ``name`` 是 QQ 客户端显示的表情名；
- ``emoji`` 是最接近的 Unicode 表情，用于在网页里直接呈现图形，
  而不是退化成 ``[表情]`` 这样的纯文字占位。

图片资源（例如 ``gchat.qpic.cn`` 的贴图接口）需要 QQ 的 OIDB 0x9154 目录
与 rkey 才能取到，无头桥接没有包通道，因此不做臆测性的 URL 拼接。
"""

from __future__ import annotations

import json
from pathlib import Path

# 名称一律以 assets/qq-faces/face_config.json 为准（来自 QQ 客户端本身），
# 不再手写：手工表曾把 id 41 写成「抠鼻」而真实是「发抖」，导致 85 处错位。
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "assets" / "qq-faces" / "face_config.json"
_TABLE: dict[object, tuple[str, str]] = {}
_LOADED = False


def _load() -> dict[object, tuple[str, str]]:
    global _TABLE, _LOADED
    if _LOADED:
        return _TABLE
    table: dict[object, tuple[str, str]] = {}
    try:
        payload = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    for entry in payload.get("sysface") or []:
        qsid = str(entry.get("QSid") or "").strip()
        if not qsid.isdigit():
            continue
        name = str(entry.get("QDes") or "").lstrip("/").strip()
        if name:
            table[int(qsid)] = (name, "")
    # Emoji-character faces use the glyph itself as the id.
    for entry in payload.get("emoji") or []:
        qsid = str(entry.get("QSid") or "").strip()
        name = str(entry.get("QDes") or "").lstrip("/").strip()
        if qsid and not qsid.isdigit() and name:
            table[qsid] = (name, qsid)
    _TABLE = table
    _LOADED = True
    return _TABLE


def __getattr__(name: str):
    # FACE_TABLE stays available for callers, populated from QQ's own config.
    if name == "FACE_TABLE":
        return _load()
    raise AttributeError(name)


_UNKNOWN_FACE = ("表情", "")


def face_descriptor(face_id: object) -> dict[str, str]:
    """按 QQ 官方配置返回表情名称。

    未知 id 只退化为通用「表情」，不猜测含义。
    """
    text = str(face_id).strip()
    table = _load()
    try:
        key: object = int(text)
    except (TypeError, ValueError):
        key = text
    name, emoji = table.get(key, _UNKNOWN_FACE)
    return {"name": name, "emoji": emoji}
