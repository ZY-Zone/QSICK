"""获取文件 URL."""

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


async def _handle(ctx: Any, file: str = "", file_id: str = "", **_: Any) -> dict[str, Any]:
    return await media_info(ctx, "file", file or file_id)


class GetFileAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
