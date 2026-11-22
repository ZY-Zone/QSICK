"""获取合并转发内容 (OneBot v11).

Accepts either a numeric message ID (the forward's own message) or a forward
resource ID. Native retrieval is the verified `getMultiMsg` path; children are
already canonical messages, so they are returned as standard `node` segments.
"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, ActionError, call, forward_node_json

WHERE = "get forward message"


async def _handle(ctx: Any, id: str = "", message_id: Any = None, chat_type: str = "", chat_id: Any = None, **_: Any) -> dict[str, Any]:
    res_id = str(id or message_id or "").strip()
    if not res_id:
        raise ActionError("get_forward_msg requires id or message_id")
    kind = str(chat_type or "").strip()
    target = chat_id
    if not kind or target in (None, ""):
        # Fall back to the stored message, which knows its own conversation.
        stored = _lookup(ctx, res_id)
        if stored is not None:
            kind = stored.chat_type
            target = stored.chat_id
    if kind not in ("group", "private") or target in (None, ""):
        raise ActionError("get_forward_msg requires chat_type and chat_id, or a known message id")
    chat = {"type": kind, "id": str(target)}
    data = await call(ctx, "forward.get", {"res_id": res_id, "chat": chat, **({"root_msg_id": str(id)} if id else {})}, WHERE)
    nodes = data.get("message") or []
    if not isinstance(nodes, list) or not nodes:
        raise ActionError(f"{WHERE}: forward returned no readable messages")
    # Keep a legacy key for existing QSICK callers, but `message` is the v11 shape.
    return {"message": nodes, "messages": [forward_node_json({"sender": {"uin": n["data"].get("user_id", 0), "nickname": n["data"].get("nickname", "")}, "time": n["data"].get("time", 0), "elements": n["data"].get("content", [])}) for n in nodes]}


def _lookup(ctx: Any, raw: str) -> Any:
    try:
        message_id = int(raw)
    except (TypeError, ValueError):
        return None
    store = getattr(ctx, "store", None)
    return store.get(message_id) if store is not None else None


class GetForwardMsgAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
