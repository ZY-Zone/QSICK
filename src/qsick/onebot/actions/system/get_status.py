"""获取运行状态."""

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
    return {
        "online": ctx.login.online and ctx.login.logged_in,
        "good": ctx.login.logged_in,
        "user_id": ctx.self_id,
        "nickname": ctx.login.nickname,
    }


class GetStatusAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
