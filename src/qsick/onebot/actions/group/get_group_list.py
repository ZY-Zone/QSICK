"""获取群列表."""

from __future__ import annotations

from typing import Any

from qsick.onebot import models
from qsick.onebot.actions.base import (
    Action,
    ActionError,
    call,
    public_entity_data,
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
        cached = ctx.cache.get("group.list")
        if cached is not None:
            return cached
    data = await call(ctx, "group.list", {}, "get group list")
    groups = data if isinstance(data, list) else data.get("groups", [])
    result = [
        {
            **public_entity_data(group),
            "group_id": int(group.get("group_id", 0)),
            "group_name": group.get("group_name", ""),
            "member_count": group.get("member_count", 0),
            "max_member_count": group.get("max_member_count", 0),
        }
        for group in groups
    ]
    if ttl > 0:
        ctx.cache.set("group.list", result, ttl)
    return result


class GetGroupListAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
