"""设置消息表情回应."""

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


async def _handle(ctx: Any, message_id: int, emoji_id: str = "", set: bool = True, **_: Any) -> None:
    await call(
        ctx,
        "message.emoji_like",
        {"message_id": message_id, "emoji_id": emoji_id, "set": set},
        "emoji like",
    )


class SetMsgEmojiLikeAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
