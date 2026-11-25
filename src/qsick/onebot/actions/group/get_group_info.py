"""获取群信息."""

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


async def _handle(ctx: Any, group_id: int, no_cache: bool = False, **_: Any) -> dict[str, Any]:
    data = await call(ctx, "group.info", {"group_id": group_id, "no_cache": no_cache}, "get group info")
    return {
        **public_entity_data(data),
        "group_id": int(data.get("group_id", group_id)),
        "group_name": data.get("group_name", ""),
        "member_count": data.get("member_count", 0),
        "max_member_count": data.get("max_member_count", 0),
    }


class GetGroupInfoAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
