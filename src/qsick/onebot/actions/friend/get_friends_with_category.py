"""获取好友分组"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class GetFriendsWithCategoryAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "friend.categories", {}, "friend categories")
        return data if isinstance(data, list) else data.get("categories", [])
