"""处理好友请求."""

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
    ctx: Any, flag: str = "", approve: bool = True, remark: str = "", **_: Any
) -> None:
    await call(
        ctx, "request.friend", {"flag": flag, "approve": approve, "remark": remark}, "handle friend request"
    )


class SetFriendAddRequestAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
