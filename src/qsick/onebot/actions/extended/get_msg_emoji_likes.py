"""获取消息表情回应列表"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class GetMsgEmojiLikesAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "message.emoji_likes", {"message_id": params["message_id"], "count": params.get("count", 20)}, "emoji likes")
        return data if isinstance(data, list) else data.get("likes", [])
