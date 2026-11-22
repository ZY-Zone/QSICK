"""Query cached native friend status codes without guessing online state."""
from typing import Any
from qsick.onebot.actions.base import Action, call

class GetFriendStatusListAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "friend.statuses", params, "get friend status list")
        return data if isinstance(data, list) else data.get("friends", [])
