"""设置消息表情回应.

`message.emoji_like` 需要 (会话, message_seq, message_id, emoji_id, set)。`message_refs` 里只
有**读过历史**的消息，而表情回应几乎总是针对刚收到的消息 —— 所以这里从消息缓存补出会话与
序号，让"收到就回应"这条最常用的路径不需要调用方重复提供。
"""

from __future__ import annotations

from typing import Any

from qsick.onebot import models
from qsick.onebot.actions.base import Action, ActionError, call


async def _handle(ctx: Any, message_id: Any = None, emoji_id: str = "",
                  set: bool = True, group_id: Any = None, user_id: Any = None,
                  message_seq: Any = None, **_: Any) -> dict[str, Any]:
    if message_id in (None, ""):
        raise ActionError("set_msg_emoji_like: message_id is required", models.BAD_REQUEST)

    payload: dict[str, Any] = {"message_id": str(message_id), "emoji_id": str(emoji_id or ""),
                               "set": bool(set)}
    # Explicit values win; anything still missing is taken from the cache.
    if group_id not in (None, ""):
        payload["group_id"] = group_id
    if user_id not in (None, ""):
        payload["user_id"] = user_id
    if message_seq not in (None, ""):
        payload["message_seq"] = str(message_seq)

    if "group_id" not in payload and "user_id" not in payload:
        cached = None
        try:
            cached = ctx.store.get(int(message_id))
        except (TypeError, ValueError):
            raise ActionError("set_msg_emoji_like: message_id must be an integer",
                              models.BAD_REQUEST) from None
        except Exception:  # noqa: BLE001 - the cache is optional infrastructure
            cached = None
        if cached is not None:
            if cached.chat_type == "group":
                payload["group_id"] = cached.chat_id
            else:
                payload["user_id"] = cached.chat_id
            if "message_seq" not in payload and getattr(cached, "seq", 0):
                payload["message_seq"] = str(cached.seq)

    if "message_seq" not in payload and "group_id" not in payload and "user_id" not in payload:
        # Nothing to disambiguate with; let the payload say so in its own words rather than guessing.
        raise ActionError(
            "set_msg_emoji_like: this message is not in the cache and no group_id/user_id + "
            "message_seq was given", models.NOT_FOUND)

    result = await call(ctx, "message.emoji_like", payload, "emoji like")
    return {
        "message_id": result.get("message_id", str(message_id)),
        "emoji_id": result.get("emoji_id", payload["emoji_id"]),
        "set": result.get("set", payload["set"]),
        "chat_type": result.get("chat_type", ""),
    }


class SetMsgEmojiLikeAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
