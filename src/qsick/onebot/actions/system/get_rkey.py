"""获取下载 rkey."""

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


async def _handle(ctx: Any, **_: Any) -> list[dict[str, Any]]:
    account = str(getattr(ctx, 'account', '') or '')
    fetched = await ctx.rkey.refresh(account)
    entries = []
    for scope in ("private", "group"):
        token = ctx.rkey.cached(scope, account)
        if token:
            entries.append({"type": 10 if scope == "private" else 20, "rkey": token, "ttl": 0, "create_time": 0})
    if not entries and fetched:
        entries = [{"type": 10 if scope == "private" else 20, "rkey": value} for scope, value in fetched.items()]
    if not entries:
        # A failed fetch used to look like a successful empty list; surface it.
        raise ActionError("get_rkey: no rkey available from the current transport")
    return entries


class GetRkeyAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
