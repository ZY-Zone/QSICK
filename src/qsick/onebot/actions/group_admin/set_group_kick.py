"""踢出群成员."""

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
    ctx: Any, group_id: int, user_id: int, reject_add_request: bool = False, **_: Any
) -> None:
    await call(
        ctx,
        "group.kick",
        {"group_id": group_id, "user_id": user_id, "reject_add_request": reject_add_request},
        "kick member",
    )


class SetGroupKickAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
