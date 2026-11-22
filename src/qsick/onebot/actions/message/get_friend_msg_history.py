"""获取私聊历史消息."""

from __future__ import annotations

from typing import Any

from qsick.onebot import models
from qsick.onebot.actions.base import (
    Action,
    ActionError,
    call,
    chat,
    forward_node_json,
    media_info,
    member_json,
    message_json,
    message_json_payload,
    message_to_elements,
    send_message,
)
from qsick.protocol import elements as E


async def _handle(
    ctx: Any,
    user_id: int,
    message_seq: int = 0,
    count: int = 20,
    reverse_order: bool = False,
    reverseOrder: bool = False,
    **_: Any,
) -> dict[str, Any]:
    # Same as the group variant: `reverse_order` was accepted by the OneBot contract and silently ignored, so
    # both orderings returned the same page.
    data = await call(
        ctx,
        "history.private",
        {
            "user_id": user_id,
            "message_seq": message_seq,
            "count": count,
            "reverse_order": bool(reverse_order or reverseOrder),
        },
        "private history",
    )
    messages = data.get("messages", [])
    return {"messages": [message_json_payload(ctx, item) for item in messages]}


class GetFriendMsgHistoryAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
