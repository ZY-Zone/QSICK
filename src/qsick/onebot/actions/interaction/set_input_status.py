"""设置输入状态（正在输入）"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class SetInputStatusAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        await call(ctx, "input.status", {"user_id": params["user_id"], "event_type": params.get("event_type", 1)}, "input status")
        return None
