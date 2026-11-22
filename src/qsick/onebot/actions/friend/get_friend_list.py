"""获取好友列表."""

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


async def _handle(ctx: Any, **_: Any) -> list[dict[str, Any]]:
    ttl = ctx.config.onebot.cache_seconds
    if ttl > 0:
        cached = ctx.cache.get("friend.list")
        if cached is not None:
            return cached
    data = await call(ctx, "friend.list", {}, "get friend list")
    friends = data if isinstance(data, list) else data.get("friends", [])
    result = [
        {
            "user_id": int(friend.get("uin", friend.get("user_id", 0))),
            "nickname": friend.get("nickname", ""),
            "remark": friend.get("remark", ""),
        }
        for friend in friends
    ]
    if ttl > 0:
        ctx.cache.set("friend.list", result, ttl)
    return result


class GetFriendListAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
