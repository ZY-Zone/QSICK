"""获取群 @全体成员 剩余次数"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class GetGroupAtAllRemainAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "group.at_all_remain", {"group_id": params["group_id"]}, "at-all remain")
        return {
            "can_at_all": bool(data.get("can_at_all", True)),
            "remain_at_all_count_for_group": int(data.get("remain_at_all_count_for_group", 0)),
            "remain_at_all_count_for_uin": int(data.get("remain_at_all_count_for_uin", 0)),
        }
