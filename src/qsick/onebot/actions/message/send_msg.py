"""发送消息."""

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
    message: Any,
    message_type: str = "",
    user_id: int = 0,
    group_id: int = 0,
    auto_escape: bool = False,
    **_: Any,
) -> dict[str, Any]:
    if message_type == "group" or (not message_type and group_id):
        return await send_message(ctx, chat("group", group_id), message, auto_escape)
    if message_type == "private" or (not message_type and user_id):
        return await send_message(ctx, chat("private", user_id), message, auto_escape)
    raise ActionError("send_msg requires message_type or user_id/group_id")


class SendMsgAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
