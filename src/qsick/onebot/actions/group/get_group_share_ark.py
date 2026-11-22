"""Query a native group share card; does not send it."""
from typing import Any
from qsick.onebot.actions.base import Action, call

class GetGroupShareArkAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await call(ctx, "group.share_ark", params, "get group share card")
