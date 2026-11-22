"""获取最近会话."""

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


async def _handle(ctx: Any, count: int = 10, **_: Any) -> list[dict[str, Any]]:
    data = await call(ctx, "recent.contacts", {"count": count}, "recent contacts")
    contacts = data if isinstance(data, list) else data.get("contacts", [])
    return list(contacts)


class GetRecentContactAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
