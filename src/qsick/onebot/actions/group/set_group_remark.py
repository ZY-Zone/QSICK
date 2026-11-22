"""设置群备注"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class SetGroupRemarkAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        await call(ctx, "group.remark", {"group_id": params["group_id"], "remark": params.get("remark", "")}, "group remark")
        return None
