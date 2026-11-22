"""获取群文件地址."""

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
    ctx: Any, group_id: int, file_id: str = "", busid: int = 0, **_: Any
) -> dict[str, Any]:
    data = await call(
        ctx, "media.url", {"kind": "file", "scope": "group", "group_id": group_id, "file_id": file_id, "busid": busid}, "file url"
    )
    return {"url": data.get("url", "")}


class GetGroupFileUrlAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
