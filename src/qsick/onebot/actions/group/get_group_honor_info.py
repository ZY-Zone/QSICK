"""获取群荣誉信息"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class GetGroupHonorInfoAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "group.honor", {"group_id": params["group_id"], "type": params.get("type", "all")}, "group honor")
        return data
