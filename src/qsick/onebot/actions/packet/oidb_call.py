"""调用任意 OIDB."""

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


async def oidbcall(
    ctx: Any, command: int = 0, sub_command: int = 0, body_hex: str = "", **_: Any
) -> dict[str, Any]:
    if command <= 0:
        raise ActionError("oidb_call requires command")
    if body_hex and not re.fullmatch(r"[0-9a-fA-F]*", body_hex):
        raise ActionError("body_hex must be hex")
    result = await call(
        ctx,
        "oidb.call",
        {"command": command, "sub_command": sub_command, "body_hex": body_hex},
        "oidb call",
    )
    return {
        "wire": result.get("wire", ""),
        "status": result.get("status", 0),
        "body_hex": result.get("body_hex", ""),
    }


class OidbCallAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
