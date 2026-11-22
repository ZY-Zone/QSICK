"""输入框里的表情标记。

聊天栏里一个表情既可以来自选择器（插入一段标记），也可以由用户手打。这里认得
两种写法：

* ``[CQ:ext_emotion,json=<base64url>]`` —— **主路径**。EXTemotions 规范附录 B
  的兜底写法：解码后就是一个完整的 emotion 对象，表达力最强，纯 v11 消费方
  读到不认识的段会跳过而不是判废。
* ``[CQ:face,id=N]`` —— OneBot v11 的既有写法，保留兼容。它**没有**声明动态性
  与文件类型，所以这些留空由目录决定，绝不能在这里硬塞默认值——那正是动态表情
  被当成静态发出去、进而被服务端重映射的原因。

早先自造过一套 ``[EXTe:static:14:ft=png]`` 的标记，现已废弃：它与 EXTemotions
规范不一致，两套语法并存只会让人猜哪套生效。
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

SCHEMA = "EXTemotions"

# 故意放宽：只要出现 [CQ:ext_emotion, 就当作"调用方要发一个表情"。若正文里
# 出现了这个前缀却没被上面的严格模式认下，说明标记是坏的——此时必须报错，
# 不能让它原样变成可见文字发出去。
_MARKER = re.compile(r"\[CQ:ext_emotion,json=([A-Za-z0-9_\-]+)\]")
_MARKER_PREFIX = "[CQ:ext_emotion,"
_LEGACY = re.compile(r"\[CQ:face,id=([^\]]{1,16})\]")


class EmotionMarkupError(ValueError):
    """标记无法解析。调用方应拒绝整条消息，而不是悄悄丢掉这一段。"""


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise EmotionMarkupError("表情标记的 base64 无效") from exc


def decode_marker(value: str) -> dict[str, Any]:
    """``json=`` 的值 -> emotion 对象。"""
    try:
        payload = json.loads(_b64url_decode(value).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise EmotionMarkupError("表情标记不是合法的 UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise EmotionMarkupError("表情标记必须是 JSON 对象")
    if payload.get("schema") not in (None, SCHEMA):
        raise EmotionMarkupError(f"表情标记的 schema 必须是 {SCHEMA}")
    if payload.get("id") in (None, ""):
        raise EmotionMarkupError("表情标记缺少 id")
    return payload


def encode_marker(emotion: dict[str, Any]) -> str:
    """emotion 对象 -> 标记文本，供回填输入框与测试往返。"""
    raw = json.dumps(emotion, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"[CQ:ext_emotion,json={encoded}]"


def split_segments(text: str) -> list[dict[str, Any]]:
    """把正文拆成表情段与文字段，顺序保持不变。

    表情段的 ``data`` 一定带 ``declared``：``True`` 表示调用方明确声明了格式
    （来自 ext_emotion），``False`` 表示只有 id（来自旧 CQ 写法）。上层据此决定
    是采信声明还是去查目录。
    """
    if not isinstance(text, str) or not text:
        return [{"type": "text", "data": {"text": ""}}]

    events: list[tuple[int, int, dict[str, Any]]] = []
    for match in _MARKER.finditer(text):
        emotion = decode_marker(match.group(1))
        events.append((match.start(), match.end(), {"type": "emotion", "data": emotion}))
    for match in _LEGACY.finditer(text):
        face_id = match.group(1).strip()
        if not face_id.isdigit() or not (1 <= len(face_id) <= 6) or int(face_id) < 1:
            raise EmotionMarkupError(f"旧式表情 id {face_id!r} 不是合法编号")
        # 旧写法什么都没声明；标成 declared=False 让上层去查目录。
        events.append((match.start(), match.end(),
                       {"type": "emotion", "data": {"id": face_id, "declared": False}}))

    if not events:
        if _MARKER_PREFIX in text:
            raise EmotionMarkupError("表情标记格式错误（应以 [CQ:ext_emotion,json=<base64url>] 结尾）")
        return [{"type": "text", "data": {"text": text}}]

    # 有事件时也要检查有没有坏掉的同类标记：它会被当成普通文字留在文字段里。
    leftover = text
    for start, end, _ in sorted(events, key=lambda item: item[0], reverse=True):
        leftover = leftover[:start] + leftover[end:]
    if _MARKER_PREFIX in leftover:
        raise EmotionMarkupError("表情标记格式错误（应以 [CQ:ext_emotion,json=<base64url>] 结尾）")

    events.sort(key=lambda item: item[0])
    segments: list[dict[str, Any]] = []
    cursor = 0
    for start, end, segment in events:
        if start < cursor:
            raise EmotionMarkupError("表情标记互相重叠")
        if start > cursor:
            segments.append({"type": "text", "data": {"text": text[cursor:start]}})
        segments.append(segment)
        cursor = end
    if cursor < len(text):
        segments.append({"type": "text", "data": {"text": text[cursor:]}})
    return segments
