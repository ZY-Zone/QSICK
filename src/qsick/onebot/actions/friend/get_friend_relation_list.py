"""Query exact cached relationship flags, not inferred mutual-friend status."""
from typing import Any
from qsick.onebot.actions.base import Action, call

class GetFriendRelationListAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "friend.relations", params, "get friend relationship flags")
        return data if isinstance(data, list) else data.get("friends", [])
