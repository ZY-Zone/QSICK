"""最近会话列表 (QSICK 扩展, not OneBot v11 standard).

Returns contacts in QQ's own recency order. Only C2C and group chats are
representable; other native chat kinds are dropped rather than relabelled.
"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class GetRecentContactListAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "recent.list", params, "get recent contacts")
        return data if isinstance(data, list) else data.get("contacts", [])
