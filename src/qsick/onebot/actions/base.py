"""Shared action primitives.

Every action lives in its own module and subclasses :class:`Action`. The JSON
catalog (``actions.json``) owns names, categories, parameters and status; modules
own behaviour. That split means adding an action is "one file + one JSON entry",
and a plugin can contribute either half.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ...protocol import elements as E
from .. import models
from ..context import ActionContext
from ..registry import ActionError

CQ_RE = re.compile(r"\[CQ:([A-Za-z0-9_.-]+)((?:,[^,\]]+)*)\]")
CQ_UNESCAPE = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))


class Action:
    """A single OneBot action."""

    #: Filled by the loader from ``actions.json`` so a module can inspect itself.
    name: str = ""
    category: str = ""
    status: str = "implemented"
    hint: str = ""

    async def run(self, ctx: ActionContext, **params: Any) -> Any:
        raise NotImplementedError


class PayloadAction(Action):
    """Adapter action: validate params in the core, execute the payload op.

    The payload op is named after the action and receives the same parameter
    object, so the wire implementation lives in the native layer while the
    OneBot contract (names, validation, retcodes) stays here.

    Field-name folding happens in ``ActionRegistry.dispatch`` before this runs, so every action shape
    gets it -- including the several that are not PayloadActions and read params directly. Doing it
    here as well would fold twice; the second pass is harmless but the duplication invites drift.
    """

    op: str = ""

    async def run(self, ctx: ActionContext, **params: Any) -> Any:
        return await call(ctx, self.op or self.name, params, self.name or self.op)


class PlannedAction(Action):
    """A registered-but-unwired action: the loader attaches hint/status.

    It raises :class:`ActionError` with the catalog hint, so callers always get a
    useful message instead of a silent no-op.
    """

    async def run(self, ctx: ActionContext, **params: Any) -> Any:
        raise ActionError(self.hint or "not implemented yet", models.NOT_IMPLEMENTED)


# --------------------------------------------------------------------- helpers


#: How long to wait for a message that carries media, in seconds.
#:
#: A text send answers in well under a second, but a picture has to be staged, hashed and uploaded inside QQ
#: before the receipt comes back. The native sender budgets 15 s plus a 60 s media grace for exactly that, so a
#: fixed 20 s here cut it off mid-upload: the consumer got a failed send and the reply never appeared, which
#: reads as "the bot ignored me". Comfortably above the sender's own ceiling, so its error wins when there is
#: a real one.
MEDIA_SEND_TIMEOUT = 90.0

#: Segment types that make a send slow enough to need `MEDIA_SEND_TIMEOUT`.
SLOW_SEND_TYPES = frozenset({"image", "record", "video", "file", "node", "forward", "multi_forward"})


async def call(ctx: ActionContext, op: str, params: dict[str, Any], what: str,
               *, timeout: float | None = None) -> dict[str, Any]:
    from ...transport.base import TransportError

    try:
        result = await ctx.call(op, params, timeout=timeout)
    except TransportError as exc:
        raise ActionError(f"{what}: {exc}", models.UPSTREAM) from exc
    if not result.ok:
        raise ActionError(f"{what}: {result.message or 'payload error'}", models.UPSTREAM)
    return result.data or {}


def chat(kind: str, ident: int) -> dict[str, Any]:
    if ident <= 0:
        raise ActionError(f"invalid {kind} id {ident}")
    return {"type": kind, "id": ident}


def parse_cq(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in CQ_RE.finditer(text):
        if match.start() > cursor:
            segments.append({"type": "text", "data": {"text": unescape(text[cursor : match.start()])}})
        cq_type = match.group(1)
        params: dict[str, Any] = {}
        for pair in match.group(2).split(",")[1:]:
            key, _, value = pair.partition("=")
            if key:
                params[key] = unescape(value)
        segments.append(cq_to_segment(cq_type, params))
        cursor = match.end()
    if cursor < len(text):
        segments.append({"type": "text", "data": {"text": unescape(text[cursor:])}})
    return segments


def unescape(text: str) -> str:
    for source, target in CQ_UNESCAPE:
        text = text.replace(source, target)
    return text


def cq_to_segment(cq_type: str, params: dict[str, Any]) -> dict[str, Any]:
    if cq_type == "text":
        return {"type": "text", "data": {"text": params.get("text", "")}}
    if cq_type == "at":
        return {"type": "at", "data": {"qq": params.get("qq", "all")}}
    if cq_type == "face":
        return {"type": "face", "data": {"id": params.get("id", "0")}}
    if cq_type == "reply":
        return {"type": "reply", "data": {"id": params.get("id", "")}}
    if cq_type in ("image", "record", "video", "file"):
        # sub_type 是 OneBot 表达图片子类型的地方（1 = 收藏表情）。漏掉它，
        # 用 CQ 码发表情同样会退化成普通图片。
        data = {k: v for k, v in params.items() if k in ("file", "url", "name", "file_id", "sub_type")}
        return {"type": cq_type, "data": data}
    if cq_type in ("json", "xml", "markdown", "miniapp"):
        return {"type": cq_type, "data": {"data": params.get("data", "")}}
    if cq_type in ("dice", "rps"):
        return {"type": cq_type, "data": {"result": params.get("result", 0)}}
    if cq_type == "mface":
        return {
            "type": "mface",
            "data": {
                "emoji_id": params.get("emoji_id", ""),
                "emoji_package_id": params.get("emoji_package_id", 0),
                "key": params.get("key", ""),
                "summary": params.get("summary", ""),
            },
        }
    if cq_type == "contact":
        return {"type": "contact", "data": {"type": params.get("type", "qq"), "id": params.get("id", "")}}
    if cq_type == "location":
        return {
            "type": "location",
            "data": {
                "lat": params.get("lat", 0),
                "lon": params.get("lon", 0),
                "title": params.get("title", ""),
                "content": params.get("content", ""),
            },
        }
    if cq_type == "music":
        data = {k: v for k, v in params.items() if k in ("type", "id", "url", "audio", "title", "image", "content")}
        return {"type": "music", "data": data}
    return {"type": "unknown", "data": {"original_type": cq_type, **params}}


async def send_message(ctx: ActionContext, chat: dict[str, Any], message: Any, auto_escape: bool) -> dict[str, Any]:
    payload = {"chat": chat, "elements": message_to_elements(message, auto_escape)}
    # Media gets the longer budget; a plain text send keeps the short one so a wedged session is still reported
    # quickly instead of being waited on for a minute and a half.
    slow = any(str(element.get("type") or "") in SLOW_SEND_TYPES for element in payload["elements"])
    data = await call(ctx, "message.send", payload, "send message",
                      timeout=MEDIA_SEND_TIMEOUT if slow else None)
    message_id = data.get("message_id", 0)
    return {"message_id": message_id, "message_id_str": str(message_id)}


def message_to_elements(message: Any, auto_escape: bool = False) -> list[dict[str, Any]]:
    if isinstance(message, str):
        segments = parse_cq(message) if not auto_escape else [{"type": "text", "data": {"text": message}}]
    elif isinstance(message, dict):
        segments = [message]
    elif isinstance(message, list):
        segments = message
    else:
        raise ActionError("message must be a string, object or array")

    out: list[dict[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise ActionError("message segment must be an object")
        try:
            element = E.from_onebot(segment)
        except ValueError as exc:
            # 字段值不合法是调用方的问题，不该落到 1500（内部错误）。
            raise ActionError(f"{segment.get('type')}: {exc}", models.BAD_REQUEST) from exc
        if element.type == "unknown":
            raise ActionError(f"unsupported message segment type {segment.get('type')!r}")
        out.append(element.to_dict())
    if not out:
        raise ActionError("message is empty")
    return out


def message_json_payload(ctx: ActionContext, payload: dict[str, Any]) -> dict[str, Any]:
    from ...protocol.message import Message

    message = Message.from_payload(payload)
    out = {
        "message_id": message.message_id,
        "message_id_str": str(message.message_id),
        "real_id": message.message_id,
        "message_seq": message.seq,
        "time": message.time,
        "message_type": message.chat_type,
        "sender": {
            "user_id": message.sender_uin,
            "nickname": message.sender_name,
            "card": message.card,
            "role": message.role,
        },
        "message": [E.to_onebot(element) for element in message.elements],
        "raw_message": E.to_cq_string(message.elements),
        "font": 0,
    }
    if message.is_group:
        out["group_id"] = message.chat_id
    else:
        out["user_id"] = message.chat_id
    return out


def message_json(ctx: ActionContext, message: Any) -> dict[str, Any]:
    return message_json_payload(ctx, message.to_dict())


def member_json(group_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """OneBot v11 group member object.

    Optional fields are emitted only when the source actually carries them, so a
    consumer can tell "unknown" from a fabricated default.
    """
    out: dict[str, Any] = {
        "group_id": group_id,
        "user_id": int(data.get("uin", data.get("user_id", 0))),
        "nickname": data.get("nickname", ""),
        "card": data.get("card", ""),
        "role": data.get("role", "member"),
        "join_time": data.get("join_time", 0),
        "last_sent_time": data.get("last_sent_time", 0),
        "level": str(data.get("level", "0")),
    }
    for key in ("sex", "age", "area", "unfriendly", "title", "title_expire_time", "card_changeable"):
        if data.get(key) is not None:
            out[key] = data[key]
    return out


async def media_info(ctx: ActionContext, kind: str, file_id: str) -> dict[str, Any]:
    if not file_id:
        raise ActionError(f"{kind}: file or file_id is required")
    data = await call(ctx, "media.url", {"kind": kind, "file_id": file_id}, f"{kind} url")
    return {"file": file_id, "url": data.get("url", ""), "expires_in": data.get("expires_in", 0)}


def forward_node_json(node: dict[str, Any]) -> dict[str, Any]:
    sender = node.get("sender") or {}
    return {
        "sender": {"user_id": sender.get("uin", 0), "nickname": sender.get("nickname", "")},
        "time": node.get("time", 0),
        "message": [E.to_onebot(E.normalize(item)) for item in node.get("elements", [])],
    }


def param(name: str, kind: str = "str", required: bool = False, default: Any = None, desc: str = "") -> dict[str, Any]:
    """Build a catalog parameter entry (used by generators and plugins)."""
    return {"name": name, "kind": kind, "required": required, "default": default, "desc": desc}


def to_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)
