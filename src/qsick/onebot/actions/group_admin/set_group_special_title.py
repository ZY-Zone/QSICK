"""设置群头衔."""

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
    ctx: Any, group_id: int, user_id: int, special_title: str = "", duration: int = -1, **_: Any
) -> None:
    await call(
        ctx,
        "group.special_title",
        {"group_id": group_id, "user_id": user_id, "special_title": special_title, "duration": duration},
        "set special title",
    )


class SetGroupSpecialTitleAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
