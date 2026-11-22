"""Count complete native group-member records."""
from typing import Any
from qsick.onebot.actions.base import Action, call

class GetGroupMemberCountAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await call(ctx, "group.member_count", params, "get group member count")
