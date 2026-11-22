"""获取登录号信息."""

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
    if ctx.login.logged_in and ctx.login.uin and ctx.login.uin != "0":
        return {"user_id": ctx.self_id, "nickname": ctx.login.nickname}
    # Cold start (or a payload that has not pushed LOGIN yet): ask the payload.
    # A failed lookup must surface as an error, not a fabricated user_id=0.
    data = await call(ctx, "login.info", {}, "login info")
    uin = int(data.get("uin", 0) or 0)
    if uin <= 0:
        raise ActionError("login info: not logged in")
    return {
        "user_id": uin,
        "nickname": str(data.get("nickname", "")),
    }


class GetLoginInfoAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
