"""处理群请求."""

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
    ctx: Any, flag: str = "", sub_type: str = "", approve: bool = True, reason: str = "", type: str = "", **_: Any
) -> None:
    # v11 names the field `type`; older QSICK clients send `sub_type`.
    request_type = type or sub_type
    if not flag:
        raise ActionError("set_group_add_request requires flag")
    if not request_type:
        raise ActionError("set_group_add_request requires type (add or invite)")
    await call(
        ctx,
        "request.group",
        {"flag": flag, "sub_type": request_type, "type": request_type, "approve": approve, "reason": reason},
        "handle group request",
    )


class SetGroupAddRequestAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
