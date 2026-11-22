"""获取消息."""

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


async def _handle(ctx: Any, message_id: int, **_: Any) -> dict[str, Any]:
    stored = ctx.store.get(message_id)
    if stored is not None:
        return message_json(ctx, stored)
    data = await call(ctx, "message.get", {"message_id": message_id}, "get message")
    return message_json_payload(ctx, data)


class GetMsgAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
