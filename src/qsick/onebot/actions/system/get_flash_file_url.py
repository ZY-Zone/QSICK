"""获取闪传下载地址."""

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


async def _handle(ctx: Any, file_set_id: str = "", **_: Any) -> dict[str, Any]:
    if not file_set_id:
        raise ActionError("get_flash_file_url requires file_set_id")
    data = await call(ctx, "media.url", {"kind": "flash_file", "file_set_id": file_set_id}, "flash url")
    return {"url": data.get("url", ""), "file_set_id": file_set_id}


class GetFlashFileUrlAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
