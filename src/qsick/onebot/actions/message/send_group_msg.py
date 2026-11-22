"""发送群消息."""

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


async def _handle(ctx: Any, group_id: int, message: Any, auto_escape: bool = False, **_: Any) -> dict[str, Any]:
    return await send_message(ctx, chat("group", group_id), message, auto_escape)


class SendGroupMsgAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
