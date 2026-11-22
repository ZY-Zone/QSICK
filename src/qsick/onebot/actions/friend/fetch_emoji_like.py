"""拉取消息表情回应（增量）"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class FetchEmojiLikeAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "message.emoji_like.fetch", {"message_id": params["message_id"]}, "fetch emoji likes")
        return {"emoji_likes": data.get("likes", [])}
