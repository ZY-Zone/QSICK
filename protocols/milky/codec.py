"""Milky 消息段 <-> 内部部件。

Milky 的段类型是**带 tag 的对象**（`{"type": "text", "text": "..."}`），不是 OneBot 的
`{"type": ..., "data": {...}}`。文件名与 CQ 码和 v11 都不同，所以这里单独写一份，而不是去
改 v11 的 codec——两份 codec 各自对着自己的规范，改一个不会悄悄影响另一个。

字段映射：

| Milky 接收段 | 内部部件 | 备注 |
| --- | --- | --- |
| `text` | `text` | |
| `mention` | `at` | Milky 的 `name` 在内部 `at` 部件里也留着（`name` 键），有就带回来 |
| `mention_all` | `at`（qq=`all`） | |
| `face` | `emotion` | 只有 `face_id`，其余语义 Milky 也没表达 |
| `reply` | `reply` | |
| `image` / `record` / `video` / `file` | 同名媒体部件 | `resource_id` <-> `file_id`；`temp_url` <-> `url`；宽高、时长、摘要、`sub_type` 按规范名对齐 |
| `forward` | `forward` | |
| `market_face` | `mface` 透传 | 内部未建模，按 unknown 原样保留 |
| `light_app` | `json` 透传 | |
| `xml` / `markdown` | 同名透传 | |

不认识的段一律进 `unknown` 部件，绝不静默丢弃——这是 `protocols/base.py` 的约定，也是唯一能
让上层看见"有东西来了但我不认"的方式。
"""

from __future__ import annotations

from typing import Any

from ..base import (
    KIND_AT,
    KIND_EMOTION,
    KIND_REPLY,
    KIND_TEXT,
    KIND_UNKNOWN,
    MEDIA_KINDS,
    PartError,
)

#: 接收段里能直接对上内部媒体部件的类型。
_MEDIA_TAGS = {
    "image": "image",
    "record": "record",
    "video": "video",
    "file": "file",
}

#: 内部部件编码成 Milky 发送段时，段类型就是部件类型（除了下面这几个）。
_KIND_TO_TAG = {
    "text": "text",
    "at": "mention",
    "emotion": "face",
    "reply": "reply",
    "image": "image",
    "record": "record",
    "video": "video",
    "forward": "forward",
}

#: 内部 kind 与发送段名不同的那批。
_RENAMED = {"json": "light_app", "mface": "market_face"}


# --------------------------------------------------------------------- 解码


def segment_to_part(segment: Any) -> dict[str, Any]:
    """一个 Milky 接收段 -> 一个内部部件。"""
    if not isinstance(segment, dict):
        raise PartError(f"segment must be an object, got {type(segment).__name__}")
    tag = str(segment.get("type") or "").strip()
    if not tag:
        raise PartError("segment needs a 'type'")

    if tag == "text":
        return {"kind": KIND_TEXT, "text": str(segment.get("text") or "")}

    if tag == "mention":
        qq = segment.get("user_id")
        if qq is None or str(qq) == "":
            raise PartError("mention segment needs 'user_id'")
        part: dict[str, Any] = {"kind": KIND_AT, "qq": str(qq)}
        # `name` is optional in the spec (v1.2) and must not be invented; keep it when sent.
        if segment.get("name") not in (None, ""):
            part["name"] = str(segment["name"])
        return part

    if tag == "mention_all":
        return {"kind": KIND_AT, "qq": "all"}

    if tag == "face":
        face_id = segment.get("face_id", segment.get("id"))
        if face_id is None or str(face_id) == "":
            raise PartError("face segment needs 'face_id'")
        return {"kind": KIND_EMOTION, "emotion": {"id": str(face_id)}}

    if tag == "reply":
        ident = segment.get("message_id", segment.get("id"))
        if ident is None or str(ident) == "":
            raise PartError("reply segment needs 'message_id'")
        # Milky 的 message_id 是 int64，可能超出 JS 安全整数范围；内部一律按字符串存。
        return {"kind": KIND_REPLY, "id": str(ident)}

    if tag in _MEDIA_TAGS:
        payload: dict[str, Any] = {}
        # `resource_id` 是 Milky 的稳定句柄；内部的 `file_id` 与之对应。其余字段按规范名对齐：
        # 接收段叫 `temp_url` / `file_name` / `file_size`，内部叫 `url` / `file_name` / `file_size`。
        for source, target in (("resource_id", "file_id"), ("file_id", "file_id"),
                               ("temp_url", "url"), ("url", "url"),
                               ("file_name", "file_name"), ("name", "name"),
                               ("file_size", "file_size"), ("size", "size"),
                               ("width", "width"), ("height", "height"),
                               ("summary", "summary"), ("duration", "duration")):
            value = segment.get(source)
            if value not in (None, "") and target not in payload:
                payload[target] = value
        if segment.get("sub_type") not in (None, ""):
            payload["sub_type"] = _sub_type_to_internal(segment["sub_type"])
        return {"kind": _MEDIA_TAGS[tag], **payload}

    # 未建模但语义清楚的：原样保留数据和 tag，让上层能看见。
    forwarded = {key: value for key, value in segment.items() if key != "type"}
    if tag in ("light_app", "xml", "markdown", "market_face", "forward"):
        kind_map = {"light_app": "json", "market_face": "mface"}
        return {"kind": KIND_UNKNOWN, "type": kind_map.get(tag, tag), "data": forwarded}
    return {"kind": KIND_UNKNOWN, "type": tag, "data": forwarded}


def segments_to_parts(segments: Any) -> list[dict[str, Any]]:
    """整条消息的段数组 -> 内部部件数组。单个段坏了只丢那一段。"""
    if not isinstance(segments, list):
        raise PartError("message segments must be an array")
    parts: list[dict[str, Any]] = []
    for segment in segments:
        try:
            parts.append(segment_to_part(segment))
        except PartError:
            # 一个坏段不该让整条消息被判废：把它留成 unknown，上层至少知道有这么一段。
            parts.append({"kind": KIND_UNKNOWN, "type": "malformed",
                          "data": {"raw": segment}})
    return parts


# --------------------------------------------------------------------- 编码


def part_to_segment(part: dict[str, Any]) -> dict[str, Any]:
    """一个内部部件 -> 一个 Milky 发送段。"""
    kind = str(part.get("kind") or "")

    if kind == KIND_TEXT:
        return {"type": "text", "text": str(part.get("text") or "")}

    if kind == KIND_AT:
        qq = str(part.get("qq") or "")
        if qq == "all":
            return {"type": "mention_all"}
        segment: dict[str, Any] = {"type": "mention", "user_id": _number_or_text(qq)}
        if part.get("name") not in (None, ""):
            segment["name"] = str(part["name"])
        return segment

    if kind == KIND_EMOTION:
        emotion = part.get("emotion") or {}
        return {"type": "face", "face_id": str(emotion.get("id") or "")}

    if kind == KIND_REPLY:
        return {"type": "reply", "message_id": _number_or_text(part.get("id"))}

    if kind in MEDIA_KINDS:
        return _media_segment(kind, part)

    if kind == KIND_UNKNOWN:
        tag = str(part.get("type") or "")
        data = part.get("data") if isinstance(part.get("data"), dict) else {}
        if tag in ("json", "xml", "markdown", "forward", "mface", "market_face", "light_app"):
            out_tag = {"json": "light_app", "mface": "market_face"}.get(tag, tag)
            return {"type": out_tag, **data}
        # 内部认不出、Milky 也没有对应发送段：整个段透传，标签保持原样。
        return {"type": tag or "unknown", **data}

    # 其余按 rename 表处理（forward 之类）。
    tag = _RENAMED.get(kind, _KIND_TO_TAG.get(kind, kind))
    return {"type": tag}


def parts_to_segments(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """内部部件数组 -> Milky 发送段数组。"""
    return [part_to_segment(part) for part in parts if isinstance(part, dict)]


#: 规范里每个媒体段带了哪些字段。只输出这一组里**真有值**的那些，不补默认值：
#: 编出来的 `width: 0` 会被客户端当成真实宽高显示。
_MEDIA_FIELDS = {
    "image": ("width", "height", "summary"),
    "record": ("duration",),
    "video": ("width", "height", "duration"),
    "file": ("file_name", "file_size"),
}


def _media_segment(kind: str, part: dict[str, Any]) -> dict[str, Any]:
    """内部媒体部件 -> 规范形状的 Milky 媒体段。

    文件段在规范里用 `file_id` / `file_name` / `file_size`，其余媒体段用 `resource_id` +
    `temp_url`。以前所有媒体都只发 `resource_id` 和 `url`，于是客户端拿不到宽高、时长、
    文件名——这些值在内部部件里本来就有。
    """
    tag = _KIND_TO_TAG.get(kind, "file")
    segment: dict[str, Any] = {"type": tag}
    if tag == "file":
        for target, sources in (("file_id", ("file_id", "file", "resource_id")),
                                ("file_name", ("file_name", "name")),
                                ("file_size", ("file_size", "size"))):
            value = next((part.get(key) for key in sources if part.get(key) not in (None, "")), None)
            if value is not None:
                segment[target] = value
        return segment
    # 内部用 `file` / `file_id` 两种拼法都见过，发送统一成 `resource_id`。
    for source in ("file_id", "file", "resource_id"):
        value = part.get(source)
        if isinstance(value, str) and value:
            segment["resource_id"] = value
            break
    if part.get("url"):
        segment["temp_url"] = str(part["url"])
    for field in _MEDIA_FIELDS.get(tag, ()):
        if part.get(field) is not None:
            segment[field] = part[field]
    if tag == "image" and part.get("sub_type") is not None:
        sub_type = _sub_type_to_milky(part["sub_type"])
        if sub_type:
            segment["sub_type"] = sub_type
    return segment


def _sub_type_to_milky(value: Any) -> str:
    """内部图片子类型 -> 规范的 `normal` / `sticker`。

    宿主给的是数字（`picSubType`，1 = 收藏表情/贴纸）。认不出来就给空串，让调用方省略这个
    字段——写一个猜的 `normal` 会把贴纸说成普通图片。
    """
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("normal", "sticker"):
            return lowered
        if not lowered.isdigit():
            return ""
        value = int(lowered)
    if isinstance(value, bool) or not isinstance(value, int):
        return ""
    if value == 1:
        return "sticker"
    return "normal" if value == 0 else ""


def _sub_type_to_internal(value: Any) -> Any:
    """规范的 `normal` / `sticker` -> 内部数字，其余原样返回。"""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "sticker":
            return 1
        if lowered == "normal":
            return 0
    return value


def _number_or_text(value: Any) -> Any:
    """QQ 号与消息 id：能当整数就用整数，否则原样给字符串。

    Milky 的 `uin`/`int64` 在规范里是数字。但消息 id 是 64 位，超出 JSON 数字的精确范围
    时转成数字会静默改值——那种错在接收端表现为"引用了不存在的消息"。所以只有确实能精确
    表示时才转。
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return 0
    if text.lstrip("-").isdigit():
        number = int(text)
        if -(2 ** 53) < number < 2 ** 53:
            return number
    return text
