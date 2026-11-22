"""动态表情（Lottie）资源的定位。

Lottie 是纯矢量 JSON，QQ 用它驱动超级表情的动效。本项目不猜路径：导入工具
已经把每个表情的真实 Lottie 文件收进 ``assets/qq-faces/super/<id>/``，并把相对
路径记进 ``index.json`` 的 ``extras``。这里只按索引取文件。

按路径约定硬拼 ``qlottie/<pack>/<id>/<id>.json`` 的做法已经废弃：那套目录只在
完整安装的 QQ 客户端里存在，且需要 ``AniSticker*`` 三元组，而索引里的 419+
等表情根本没有这些字段。
"""

from __future__ import annotations

import json
from pathlib import Path

from .face_assets import EMBEDDED_ROOT

_INDEX_CACHE: dict[str, tuple[float, dict[str, Path]]] = {}


def _load_index(directory: Path) -> dict[str, Path]:
    """QSid/字符 -> Lottie 绝对路径。"""
    target = directory / "index.json"
    if not target.is_file():
        return {}
    try:
        stamp = target.stat().st_mtime
    except OSError:
        return {}
    cache_key = str(target)
    cached = _INDEX_CACHE.get(cache_key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    index: dict[str, Path] = {}
    if not isinstance(payload, list):
        return {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        extras = entry.get("extras") or {}
        if not isinstance(extras, dict) or "lottie" not in extras:
            continue
        identifier = str(entry.get("unicode") or entry.get("id") or "").strip()
        if not identifier:
            continue
        # 导入时把 Lottie 落在 super/<id>/ 下；索引里记的是语料侧的相对路径，
        # 所以这里按落盘位置重建，而不是回到语料目录找。
        raw_id = str(entry.get("id") or "").strip()
        bundled = directory / "super" / raw_id
        if not bundled.is_dir():
            continue
        for candidate in sorted(bundled.glob("*.lottie.json")):
            index[identifier] = candidate
            break
    _INDEX_CACHE[cache_key] = (stamp, index)
    return index


def animation_path(headless_root: str | Path, face_id: object) -> Path | None:
    """某个表情的 Lottie 文件；没有动画资源时返回 None。"""
    key = str(face_id if face_id is not None else "").strip()
    if not key:
        return None
    keys = [key]
    if key.isdigit():
        normalised = str(int(key))
        if normalised != key:
            keys.append(normalised)
    index = _load_index(EMBEDDED_ROOT)
    for name in keys:
        found = index.get(name)
        if found is not None and found.is_file() and found.stat().st_size > 0:
            return found
    return None


def animation_payload(headless_root: str | Path, face_id: object) -> dict | None:
    """返回前端播放所需的描述；没有动画资源时返回 None。"""
    path = animation_path(headless_root, face_id)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    frames = data.get("op")
    rate = data.get("fr")
    duration = None
    if isinstance(frames, (int, float)) and isinstance(rate, (int, float)) and rate:
        start = data.get("ip") if isinstance(data.get("ip"), (int, float)) else 0
        duration = round(max(0.0, (frames - start) / rate), 3)
    return {"url": "/api/chat/face/animation?id=" + str(face_id), "duration": duration}


def media_type(path: Path) -> str:
    return "application/json" if path.suffix.lower() == ".json" else "application/octet-stream"
