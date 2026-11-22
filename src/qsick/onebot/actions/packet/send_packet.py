"""发送原始 SSO 包."""

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


async def _handle(ctx: Any, cmd: str = "", data: str = "", body_hex: str = "", **_: Any) -> dict[str, Any]:
    if not cmd:
        raise ActionError("send_packet requires cmd")
    raw = data or body_hex or ""
    if raw and not re.fullmatch(r"[0-9a-fA-F]*", raw):
        raise ActionError("data must be hex")
    result = await call(ctx, "raw.call", {"cmd": cmd, "body_hex": raw}, "send packet")
    return {
        "cmd": cmd,
        "status": result.get("status", 0),
        "data": result.get("body_hex", ""),
    }


class SendPacketAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
