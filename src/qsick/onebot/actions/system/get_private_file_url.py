"""获取私聊文件地址."""

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


async def _handle(ctx: Any, user_id: int, file_id: str = "", **_: Any) -> dict[str, Any]:
    data = await call(
        ctx, "media.url", {"kind": "file", "scope": "private", "user_id": user_id, "file_id": file_id}, "file url"
    )
    return {"url": data.get("url", "")}


class GetPrivateFileUrlAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
