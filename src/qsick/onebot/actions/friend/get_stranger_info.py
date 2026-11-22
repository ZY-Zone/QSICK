"""获取陌生人信息."""

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


async def _handle(ctx: Any, user_id: int, no_cache: bool = False, **_: Any) -> dict[str, Any]:
    data = await call(ctx, "friend.info", {"user_id": user_id, "no_cache": no_cache}, "get stranger info")
    return {
        "user_id": int(data.get("uin", user_id)),
        "nickname": data.get("nickname", ""),
        "sex": data.get("sex", "unknown"),
        "age": data.get("age", 0),
    }


class GetStrangerInfoAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
