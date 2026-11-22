"""获取群禁言列表"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class GetGroupShutListAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "group.shut_list", {"group_id": params["group_id"]}, "mute list")
        return data if isinstance(data, list) else data.get("members", [])
