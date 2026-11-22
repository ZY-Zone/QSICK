"""Read administrators from a complete native member list."""
from typing import Any
from qsick.onebot.actions.base import Action, call

class GetGroupAdminListAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "group.admins", params, "get group administrators")
        return data if isinstance(data, list) else data.get("members", [])
