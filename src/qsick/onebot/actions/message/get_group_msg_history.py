"""获取群历史消息."""

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
    group_id: int,
    message_seq: int = 0,
    count: int = 20,
    reverse_order: bool = False,
    reverseOrder: bool = False,
    **_: Any,
) -> dict[str, Any]:
    # `reverse_order` was declared by the OneBot contract but never forwarded, so both orderings returned the
    # same list -- measured: [1308, 1309, 1310] either way. NapCat accepts both spellings
    # (`reverse_order` and the older `reverseOrder`), so both are passed through.
    data = await call(
        ctx,
        "history.group",
        {
            "group_id": group_id,
            "message_seq": message_seq,
            "count": count,
            "reverse_order": bool(reverse_order or reverseOrder),
        },
        "group history",
    )
    messages = data.get("messages", [])
    return {"messages": [message_json_payload(ctx, item) for item in messages]}


class GetGroupMsgHistoryAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
