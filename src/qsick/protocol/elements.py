"""Message elements: the neutral shape between the payload and OneBot.

The payload emits a JSON element list. Everything here is pure and synchronous;
URL enrichment is a separate awaitable step (:func:`resolve_media`) so that a
slow rkey fetch cannot stall a message that already has a URL.

Unknown element types are **kept**, not dropped: they become ``unknown``
segments carrying the original type name and the raw JSON, so a consumer can
still see that something arrived.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

# OneBot-facing segment names we can produce.
ONE_BOT_TYPES = {
    "text",
    "at",
    "face",
    "reply",
    "image",
    "record",
    "video",
    "file",
    "json",
    "xml",
    "forward",
    "mface",
    "poke",
    "markdown",
    "dice",
    "rps",
    "contact",
    "location",
    "music",
    "miniapp",
    "flashtransfer",
    "flash_file",
    "onlinefile",
    "inline_keyboard",
    "share",
    "node",
    "shake",
    "anonymous",
    "unknown",
}

MEDIA_TYPES = {"image", "record", "video", "file", "flashtransfer", "flash_file", "onlinefile"}

#: Spellings that mean the same segment but that this project used to emit under its own name.
#:
#: `flash_file` was QSICK's own word for QQ's flash transfer. NapCat calls the segment `flashtransfer`
#: (`OB11MessageDataType.flashtransfer`), and a consumer written against NapCat switches on that string --
#: so emitting `flash_file` reached it as an unrecognised segment. Old payloads are still accepted.
_ELEMENT_ALIASES = {"flash_file": "flashtransfer"}


@dataclass(slots=True)
class Element:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "data": self.data}
        if self.raw is not None:
            out["raw"] = self.raw
        return out


def normalize(item: dict[str, Any]) -> Element:
    """Payload element -> :class:`Element`, preserving anything we do not know."""
    if not isinstance(item, dict):
        return Element("unknown", {"value": repr(item)})
    etype = str(item.get("type") or "unknown")
    etype = _ELEMENT_ALIASES.get(etype, etype)
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    if etype == "unknown":
        # Round-trip of an already-normalized unknown element: keep it flat.
        return Element("unknown", dict(data), item.get("raw") if isinstance(item.get("raw"), dict) else None)
    if etype in ONE_BOT_TYPES:
        return Element(etype, dict(data), item.get("raw") if isinstance(item.get("raw"), dict) else None)
    return Element("unknown", {"original_type": etype, "data": dict(data)}, item if isinstance(item, dict) else None)


def normalize_all(items: Iterable[dict[str, Any]] | None) -> list[Element]:
    return [normalize(item) for item in (items or [])]


# --------------------------------------------------------------------------- url


UrlResolver = Callable[[Element], Awaitable[str | None]]


async def resolve_media(elements: list[Element], resolver: UrlResolver | None) -> list[Element]:
    """Fill ``data['url']`` for media elements that only carry an id."""
    if resolver is None:
        return elements
    for element in elements:
        if element.type not in MEDIA_TYPES:
            continue
        if element.data.get("url"):
            continue
        try:
            url = await resolver(element)
        except Exception:  # a broken resolver must not lose the message
            url = None
        if url:
            element.data["url"] = url
    return elements


# ---------------------------------------------------------------------- onebot


def to_onebot(element: Element) -> dict[str, Any]:
    """Element -> OneBot segment.

    Shape follows OneBot v11 where the standard defines one; QSICK extensions
    (``onlinefile``, ``inline_keyboard``) keep explicit, namespaced keys instead of pretending to be ``json``.

    ``flashtransfer`` uses NapCat's name rather than a QSICK invention, because a consumer switches on the
    segment type string and anything unrecognised is dropped silently.
    """
    t = _ELEMENT_ALIASES.get(element.type, element.type)
    d = element.data

    if t == "text":
        return {"type": "text", "data": {"text": str(d.get("text", ""))}}
    if t == "at":
        qq = d.get("user_id", d.get("uin", "all"))
        data = {"qq": str(qq)}
        if d.get("name"):
            data["name"] = str(d["name"])
        return {"type": "at", "data": data}
    if t == "face":
        data = {"id": str(d.get("id", d.get("face_id", 0)))}
        if d.get("result_id") is not None:
            data["resultId"] = str(d["result_id"])
        return {"type": "face", "data": data}
    if t == "reply":
        return {"type": "reply", "data": {"id": str(d.get("message_id", d.get("id", "")))}} if d.get(
            "message_id", d.get("id")
        ) is not None else {"type": "reply", "data": {}}
    if t == "image":
        return {"type": "image", "data": _media_data(d, summary_key="summary", extra=("sub_type", "flash"))}
    if t == "record":
        return {"type": "record", "data": _media_data(d, extra=("duration",))}
    if t == "video":
        return {"type": "video", "data": _media_data(d, extra=("duration", "thumb"))}
    if t == "file":
        return {"type": "file", "data": _media_data(d, extra=("name", "file_name", "size", "file_size"))}
    if t == "flashtransfer":
        return {
            "type": "flashtransfer",
            "data": {
                "title": str(d.get("title", "")),
                "file_set_id": str(d.get("file_set_id", d.get("fileset_id", ""))),
                "scene_type": d.get("scene_type", 0),
                "thumb": str(d.get("thumb", "")),
            },
        }
    if t == "onlinefile":
        return {
            "type": "onlinefile",
            "data": {
                "msg_id": str(d.get("msg_id", "")),
                "element_id": str(d.get("element_id", "")),
                "file_name": str(d.get("file_name", d.get("name", ""))),
                "file_size": str(d.get("file_size", d.get("size", ""))),
                "is_dir": bool(d.get("is_dir", False)),
                "url": str(d.get("url", "")),
            },
        }
    if t == "mface":
        return {
            "type": "mface",
            "data": {
                "emoji_id": str(d.get("emoji_id", "")),
                "emoji_package_id": d.get("emoji_package_id", 0),
                "key": str(d.get("key", "")),
                "summary": str(d.get("summary", "")),
            },
        }
    if t == "poke":
        return {"type": "poke", "data": {"type": str(d.get("type", 0)), "id": str(d.get("id", 0))}}
    if t == "json":
        return {"type": "json", "data": {"data": _as_json_text(d.get("data", d.get("text", "")))}}
    if t == "xml":
        return {"type": "xml", "data": {"data": str(d.get("data", d.get("text", "")))}}
    if t == "markdown":
        return {"type": "markdown", "data": {"content": str(d.get("content", d.get("text", "")))}}
    if t == "forward":
        return {
            "type": "forward",
            "data": {
                "id": str(d.get("res_id", d.get("id", ""))),
                "summary": str(d.get("summary", "")),
                "content": d.get("content") or [],
            },
        }
    if t == "dice":
        return {"type": "dice", "data": {"result": d.get("result", 0)}}
    if t == "rps":
        return {"type": "rps", "data": {"result": d.get("result", 0)}}
    if t == "contact":
        return {
            "type": "contact",
            "data": {
                "type": str(d.get("contact_type", d.get("type", "qq"))),
                "id": str(d.get("id", d.get("user_id", ""))),
            },
        }
    if t == "location":
        return {
            "type": "location",
            "data": {
                "lat": d.get("lat", 0),
                "lon": d.get("lon", 0),
                "title": str(d.get("title", "")),
                "content": str(d.get("content", "")),
            },
        }
    if t == "music":
        data: dict[str, Any] = {"type": str(d.get("platform", d.get("type", "custom")))}
        for key in ("id", "url", "audio", "title", "image", "content"):
            if d.get(key) is not None:
                data[key] = d[key]
        return {"type": "music", "data": data}
    if t == "miniapp":
        return {"type": "miniapp", "data": {"data": _as_json_text(d.get("data", ""))}}
    if t == "inline_keyboard":
        return {
            "type": "inline_keyboard",
            "data": {
                "bot_appid": d.get("bot_appid", 0),
                "rows": d.get("rows", []),
            },
        }
    if t == "share":
        return {
            "type": "share",
            "data": {
                "url": str(d.get("url", "")),
                "title": str(d.get("title", "")),
                "content": str(d.get("content", "")),
                "image": str(d.get("image", "")),
            },
        }
    if t == "node":
        return {"type": "node", "data": dict(d)}
    if t == "shake":
        return {"type": "shake", "data": {}}
    if t == "anonymous":
        return {"type": "anonymous", "data": dict(d)}
    # unknown: hand back everything, including the original type name.
    payload: dict[str, Any] = {"original_type": d.get("original_type", "unknown")}
    payload.update({k: v for k, v in d.items() if k != "original_type"})
    if element.raw is not None:
        payload["raw"] = element.raw
    return {"type": "unknown", "data": payload}


def _media_data(d: dict[str, Any], *, summary_key: str | None = None, extra: tuple[str, ...] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"file": str(d.get("file_id", d.get("file", "")))}
    if d.get("url"):
        out["url"] = str(d["url"])
    if d.get("file_name") or d.get("name"):
        out["name"] = str(d.get("file_name", d.get("name")))
    size = d.get("file_size", d.get("size"))
    if size is not None:
        out["file_size"] = size
    if summary_key and d.get(summary_key):
        out[summary_key] = str(d[summary_key])
    for key in extra:
        if d.get(key) is not None:
            out[key] = d[key]
    return out


def _as_json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


# ------------------------------------------------------------------ from OneBot


def from_onebot(segment: dict[str, Any]) -> Element:
    """OneBot segment -> Element, used by the send path."""
    etype = str(segment.get("type", "unknown"))
    data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
    if etype == "text":
        return Element("text", {"text": str(data.get("text", ""))})
    if etype == "at":
        qq = str(data.get("qq", "all"))
        return Element("at", {"user_id": qq if qq == "all" else int(qq)})
    if etype == "face":
        return Element("face", {"id": int(data.get("id", 0))})
    if etype == "reply":
        return Element("reply", {"message_id": data.get("id")})
    if etype == "image":
        return Element("image", _media_in(data))
    if etype == "record":
        return Element("record", _media_in(data))
    if etype == "video":
        return Element("video", _media_in(data))
    if etype == "file":
        return Element("file", _media_in(data))
    if etype == "json":
        return Element("json", {"data": data.get("data", "")})
    if etype == "xml":
        return Element("xml", {"data": data.get("data", "")})
    if etype == "markdown":
        return Element("markdown", {"content": data.get("content", "")})
    if etype == "forward":
        return Element("forward", {"res_id": str(data.get("id", "")), "content": data.get("content") or []})
    if etype in ("dice", "rps"):
        return Element(etype, {"result": data.get("result", 0)})
    # poke and inline_keyboard were produced by to_onebot but had no branch here, so a segment that
    # came out of our own encoder came back as "unknown". Measured: to_onebot({'poke',...}) yields
    # {'type':'poke','data':{...}} and from_onebot of that same segment returned type 'unknown'
    # (with the payload preserved under original_type). A caller that reads a message, forwards it
    # and re-sends would silently downgrade these.
    if etype == "poke":
        return Element("poke", {"type": str(data.get("type", "")), "id": str(data.get("id", "")),
                                **({"name": data["name"]} if data.get("name") else {})})
    if etype == "inline_keyboard":
        return Element("inline_keyboard", {"bot_appid": data.get("bot_appid"),
                                           "rows": data.get("rows") or []})
    # The two file-transfer variants are media with their own type, and are named in ONE_BOT_TYPES.
    # Both spellings are accepted here: `normalize` already aliases the old one, but a caller that builds an
    # Element by hand should not have to know which name is canonical.
    if etype in ("flashtransfer", "flash_file", "onlinefile"):
        return Element(etype, _media_in(data))
    if etype == "contact":
        return Element("contact", {"contact_type": str(data.get("type", "qq")), "id": str(data.get("id", ""))})
    if etype == "location":
        return Element(
            "location",
            {
                "lat": data.get("lat", 0),
                "lon": data.get("lon", 0),
                "title": data.get("title", ""),
                "content": data.get("content", ""),
            },
        )
    if etype == "music":
        mapped = {"platform": str(data.get("type", "custom"))}
        for key in ("id", "url", "audio", "title", "image", "content"):
            if data.get(key) is not None:
                mapped[key] = data[key]
        return Element("music", mapped)
    if etype == "miniapp":
        return Element("miniapp", {"data": data.get("data", "")})
    if etype == "mface":
        return Element(
            "mface",
            {
                "emoji_id": str(data.get("emoji_id", "")),
                "emoji_package_id": data.get("emoji_package_id", 0),
                "key": str(data.get("key", "")),
                "summary": str(data.get("summary", "")),
            },
        )
    if etype == "share":
        return Element(
            "share",
            {
                "url": str(data.get("url", "")),
                "title": str(data.get("title", "")),
                "content": str(data.get("content", "")),
                "image": str(data.get("image", "")),
            },
        )
    if etype == "node":
        return Element("node", dict(data))
    if etype == "shake":
        return Element("shake", {})
    if etype == "anonymous":
        return Element("anonymous", dict(data))
    return Element("unknown", {"original_type": etype, "data": data})


def _media_in(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("file", "url", "name", "file_id", "file_size", "path"):
        if data.get(key) is not None:
            out["file_id" if key == "file" else key] = data[key]
    # 图片子类型必须原样带过这一层。QQ 用 picSubType 区分"图片"与"表情"，宿主
    # 的 prepareImage 读不到就按 NORMAL(0) 发——收藏表情因此变成一张普通图片，
    # 动图也不再被当表情渲染。OneBot 客户端写 `sub_type`，WebUI 上传表单写
    # `pic_sub_type`，这里统一收敛成 `sub_type`（to_onebot 也是这个名字）。
    # 以前这份白名单只有 6 个键，两个名字一起被丢在了纯 Python 层。
    sub_type = data.get("sub_type", data.get("pic_sub_type"))
    if sub_type is not None:
        try:
            out["sub_type"] = int(sub_type)
        except (TypeError, ValueError):
            raise ValueError(f"sub_type must be an integer, got {sub_type!r}") from None
    return out


# ------------------------------------------------------------------------- text


def to_cq_string(elements: Iterable[Element]) -> str:
    """Best-effort CQ-ish rendering for ``raw_message`` / logs."""
    parts: list[str] = []
    for element in elements:
        if element.type == "text":
            parts.append(_escape(str(element.data.get("text", ""))))
        elif element.type == "at":
            qq = element.data.get("user_id", "all")
            parts.append(f"[CQ:at,qq={qq}]")
        elif element.type == "face":
            parts.append(f"[CQ:face,id={element.data.get('id', 0)}]")
        elif element.type == "image":
            parts.append(f"[CQ:image,file={element.data.get('file_id', '')}]")
        elif element.type == "record":
            parts.append(f"[CQ:record,file={element.data.get('file_id', '')}]")
        elif element.type == "video":
            parts.append(f"[CQ:video,file={element.data.get('file_id', '')}]")
        elif element.type == "file":
            parts.append(f"[CQ:file,file={element.data.get('file_id', '')}]")
        elif element.type == "forward":
            # OneBot v11 has a CQ form for a merged forward. Emitting it keeps `raw_message`
            # readable and round-trippable; the previous fallback collapsed a whole chat record
            # into an opaque `[QSICK:forward]` with no ID.
            parts.append(f"[CQ:forward,id={_cq_param(element.data.get('res_id', ''))}]")
        elif element.type == "markdown":
            # Same shape NapCat emits (`markdown` -> `{type:'markdown',data:{content}}`), so a
            # consumer that shows the card can read the text instead of an empty placeholder.
            parts.append(f"[CQ:markdown,content={_cq_param(element.data.get('content', ''))}]")
        elif element.type == "reply":
            parts.append(f"[CQ:reply,id={element.data.get('message_id', '')}]")
        elif element.type == "dice":
            parts.append(f"[CQ:dice,result={element.data.get('result', 0)}]")
        elif element.type == "rps":
            parts.append(f"[CQ:rps,result={element.data.get('result', 0)}]")
        elif element.type == "contact":
            parts.append(f"[CQ:contact,type={element.data.get('contact_type', 'qq')},id={element.data.get('id', '')}]")
        elif element.type == "location":
            parts.append(f"[CQ:location,lat={element.data.get('lat', 0)},lon={element.data.get('lon', 0)}]")
        elif element.type == "music":
            parts.append(f"[CQ:music,type={element.data.get('platform', 'custom')},id={element.data.get('id', '')}]")
        elif element.type == "miniapp":
            parts.append("[CQ:miniapp]")
        elif element.type in ("flashtransfer", "flash_file"):
            parts.append(f"[QSICK:flashtransfer,id={element.data.get('file_set_id', '')}]")
        elif element.type == "onlinefile":
            parts.append(f"[QSICK:onlinefile,id={element.data.get('element_id', '')}]")
        elif element.type == "unknown":
            parts.append(f"[QSICK:unknown,type={element.data.get('original_type', '?')}]")
        else:
            parts.append(f"[QSICK:{element.type}]")
    return "".join(parts)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")


def _cq_param(value: Any) -> str:
    """Escape a CQ attribute value.

    A CQ attribute ends at `,` or `]`, so both need escaping inside a value; `_escape` only
    handles brackets and the ampersand because it renders plain text, not attributes.
    Comma escaping matches NapCat's `CQCodeEscape`, so the same content parses back the same way.
    """
    return _escape(str(value if value is not None else "")).replace(",", "&#44;")


def preview(elements: Iterable[Element], limit: int = 120) -> str:
    text = to_cq_string(elements)
    return text if len(text) <= limit else text[: limit - 1] + "…"
