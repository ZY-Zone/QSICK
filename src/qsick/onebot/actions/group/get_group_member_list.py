"""获取群成员列表."""

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


async def _handle(ctx: Any, group_id: int, **_: Any) -> list[dict[str, Any]]:
    data = await call(ctx, "group.members", {"group_id": group_id}, "get group members")
    members = data if isinstance(data, list) else data.get("members", [])
    return [member_json(group_id, member) for member in members]


class GetGroupMemberListAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
