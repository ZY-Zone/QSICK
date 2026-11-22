"""检查能否发送图片."""

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


async def _handle(ctx: Any, **_: Any) -> dict[str, Any]:
    # Only transports with a verified media send path may claim this. The
    # headless bridge converts no image elements, and the shipped native pipe
    # scaffold rejects every action, so both must answer false rather than
    # advertising a capability that would fail at send time.
    return {"yes": getattr(ctx.transport, "name", "") == "mock"}


class CanSendImageAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
