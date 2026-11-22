"""设置好友备注."""

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


async def _handle(ctx: Any, user_id: int, remark: str = "", **_: Any) -> None:
    await call(ctx, "friend.remark", {"user_id": user_id, "remark": remark}, "set friend remark")


class SetFriendRemarkAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
