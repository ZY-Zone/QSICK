"""纯 OneBot v11 的编解码：内部部件 <-> 消息段 / CQ 码。

这里放的是**格式转换**本身，不含任何发送或业务逻辑。动作执行留在
``src/qsick/onebot/``，本模块只回答“这段消息在线上长什么样”。

两条规则贯穿全文：

1. 纯 OneBot v11 只认基础黄脸，所以 ``emotion`` 部件在这里被降级成
   ``face`` 段。降级只丢协议表达不了的信息（是否动态、包/贴纸编号），
   不丢 id 本身 —— 上层仍然能看出发的是哪个表情。
2. 不认识的段一律原样透传，**绝不静默丢弃**。丢弃会让上游误以为消息发全了，
   透传至少让调用方看到“有东西我没认出来”。
"""

from __future__ import annotations

import re
from typing import Any

from ..base import (
    KIND_AT,
    KIND_EMOTION,
    KIND_REPLY,
    KIND_TEXT,
    KIND_UNKNOWN,
    MEDIA_KINDS,
    PartError,
    normalize_part,
)

# CQ 码：``[CQ:type,k=v,k=v]``。type 允许带点和横线，照顾扩展类型。
CQ_RE = re.compile(r"\[CQ:([A-Za-z0-9_.-]+)((?:,[^,\]]*)*)\]")

# CQ 规定正文里这三个字符必须转义；参数值额外要转义逗号（否则会被当成分隔符）。
_TEXT_ESCAPES = (("&", "&amp;"), ("[", "&#91;"), ("]", "&#93;"))
_PARAM_ESCAPES = (*_TEXT_ESCAPES, (",", "&#44;"))


# --------------------------------------------------------------------- 转义


def escape_text(text: str) -> str:
    """正文转义。``&`` 必须最先替换，否则后面替换出来的实体又会被二次转义。"""
    for source, target in _TEXT_ESCAPES:
        text = text.replace(source, target)
    return text


def escape_param(value: str) -> str:
    for source, target in _PARAM_ESCAPES:
        value = value.replace(source, target)
    return value


def unescape(text: str) -> str:
    """反转义。顺序与转义相反，避免 ``&amp;#91;`` 这种被解成 ``[``。"""
    for source, target in reversed(_PARAM_ESCAPES):
        text = text.replace(target, source)
    return text


# ---------------------------------------------------------------- 段 -> 部件


def segment_to_part(segment: Any) -> dict[str, Any]:
    """OneBot v11 消息段 -> 内部部件。

    只解释协议里真实存在的信息：``face`` 段没有“是否动态”这种字段，所以解码
    出来的 emotion 也不编造 ``format``/``animated``。默认值属于更上层的目录
    知识（谁发、发什么），不该在这一层假装是协议本身携带的。
    """
    if not isinstance(segment, dict):
        raise PartError(f"segment must be an object, got {type(segment).__name__}")

    stype = str(segment.get("type") or "").strip()
    if not stype:
        raise PartError("segment needs a 'type' field")
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}

    if stype == "text":
        return {"kind": KIND_TEXT, "text": str(data.get("text", ""))}
    if stype == "face":
        return {"kind": KIND_EMOTION, "emotion": {"id": str(data.get("id", ""))}}
    if stype == "at":
        return {"kind": KIND_AT, "qq": str(data.get("qq", "all"))}
    if stype == "reply":
        return {"kind": KIND_REPLY, "id": str(data.get("id", ""))}
    if stype in MEDIA_KINDS:
        return {"kind": stype, **_media_fields(data)}
    # 其余类型一律透传：至少让调用方知道来了一个 poke / mface / markdown。
    return {"kind": KIND_UNKNOWN, "type": stype, "data": dict(data)}


def segments_to_parts(segments: Any) -> list[dict[str, Any]]:
    if isinstance(segments, dict):
        segments = [segments]
    if not isinstance(segments, list):
        raise PartError("message must be a segment object or an array of segments")
    return [segment_to_part(segment) for segment in segments]


# ---------------------------------------------------------------- 部件 -> 段


def part_to_segment(part: Any) -> dict[str, Any]:
    """内部部件 -> OneBot v11 消息段。"""
    part = normalize_part(part)
    kind = part["kind"]

    if kind == KIND_TEXT:
        return {"type": "text", "data": {"text": part["text"]}}
    if kind == KIND_EMOTION:
        # 降级点：v11 的 face 段只有一个 id 字段，动态表情与贴纸信息在这里
        # 表达不了。id 原样保留，字形 id（单个表情字符）也照发 —— QSICK 的
        # 发送路径本来就接受它，QQ 客户端会把字形渲染成对应表情。
        return {"type": "face", "data": {"id": str(part["emotion"].get("id", ""))}}
    if kind == KIND_AT:
        return {"type": "at", "data": {"qq": part["qq"]}}
    if kind == KIND_REPLY:
        return {"type": "reply", "data": {"id": part["id"]}}
    if kind in MEDIA_KINDS:
        return {"type": kind, "data": _media_fields(part)}
    # unknown：把原始类型名和数据完整还原，包括我们没建模的字段。
    return {"type": str(part.get("type") or "unknown"), "data": dict(part.get("data") or {})}


def parts_to_segments(parts: Any) -> list[dict[str, Any]]:
    if isinstance(parts, dict):
        parts = [parts]
    if not isinstance(parts, list):
        raise PartError("parts must be a part object or an array of parts")
    return [part_to_segment(part) for part in parts]


def _media_fields(source: dict[str, Any]) -> dict[str, Any]:
    """媒体字段原样带过去，只去掉内部标记，不补协议里没有的默认值。"""
    return {key: value for key, value in source.items() if key != "kind" and value is not None}


# ------------------------------------------------------------------- CQ 码


def parse_cq_string(text: str) -> list[dict[str, Any]]:
    """CQ 字符串 -> 消息段数组。

    未识别的 CQ 类型保留原始类型名（不是统一成 ``unknown``）：这样再渲染回去
    仍是 ``[CQ:poke,...]``，扩展段不会因为经过一次解析就变成另一段。
    """
    if not isinstance(text, str):
        raise PartError("CQ message must be a string")
    if not text:
        return []

    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in CQ_RE.finditer(text):
        if match.start() > cursor:
            segments.append({"type": "text", "data": {"text": unescape(text[cursor : match.start()])}})
        segments.append({"type": match.group(1), "data": _parse_cq_params(match.group(2))})
        cursor = match.end()
    if cursor < len(text):
        segments.append({"type": "text", "data": {"text": unescape(text[cursor:])}})
    return segments


def _parse_cq_params(raw: str) -> dict[str, str]:
    params: dict[str, str] = {}
    # 去掉开头的空串（紧跟 type 的那个逗号造成的）。
    for chunk in raw.split(",")[1:]:
        key, _, value = chunk.partition("=")
        if key:
            params[key] = unescape(value)
    return params


def render_cq_string(parts: Any) -> str:
    """部件 -> CQ 字符串，用于 ``raw_message`` 之类的纯文本出口。"""
    rendered: list[str] = []
    for part in parts_to_segments(parts):
        stype = part["type"]
        data = part["data"]
        if stype == "text":
            rendered.append(escape_text(str(data.get("text", ""))))
            continue
        fields = ",".join(f"{key}={escape_param(str(value))}" for key, value in data.items())
        rendered.append(f"[CQ:{stype},{fields}]" if fields else f"[CQ:{stype}]")
    return "".join(rendered)
