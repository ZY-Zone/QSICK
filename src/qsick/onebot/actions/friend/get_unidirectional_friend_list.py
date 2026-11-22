"""获取单向好友列表"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class GetUnidirectionalFriendListAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "friend.unidirectional", {}, "unidirectional friends")
        return data if isinstance(data, list) else data.get("friends", [])
