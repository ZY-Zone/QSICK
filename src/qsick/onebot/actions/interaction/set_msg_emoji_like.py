"""设置消息表情回应.

`message.emoji_like` 需要 (会话, message_seq, message_id, emoji_id, set)，其中会话与序号取自
payload 的 `messageRefs` —— 那是客户端自己的索引，比这里任何一份缓存都权威。

所以本动作**只做补全，不做裁决**：能给的就补上（调用方显式传的优先，其次消息缓存），补不出来
就照常交给 payload，让它按自己的索引判断。之前这里在 Python 侧先拒绝
（"this message is not in the cache"），而 payload 的索引其实可能认得 —— 两处各拿一份缓存做
同一个判断，先拒的那一方就成了唯一的裁判。
"""

from __future__ import annotations

from typing import Any

from qsick.onebot import models
from qsick.onebot.actions.base import Action, ActionError, call


def _from_cache(ctx: Any, message_id: Any) -> dict[str, Any]:
    """会话与序号，能从消息缓存读出来就补上，读不到就返回空。"""
    try:
        cached = ctx.store.get(int(message_id))
    except (TypeError, ValueError):
        return {}
    except Exception:  # noqa: BLE001 - 缓存是可选设施，坏了不该让动作失败
        return {}
    if cached is None:
        return {}
    out: dict[str, Any] = {}
    if cached.chat_type == "group":
        out["group_id"] = cached.chat_id
    else:
        out["user_id"] = cached.chat_id
    if getattr(cached, "seq", 0):
        out["message_seq"] = str(cached.seq)
    return out


async def _handle(ctx: Any, message_id: Any = None, emoji_id: str = "",
                  set: bool = True, group_id: Any = None, user_id: Any = None,
                  message_seq: Any = None, **_: Any) -> dict[str, Any]:
    if message_id in (None, ""):
        raise ActionError("set_msg_emoji_like: message_id is required", models.BAD_REQUEST)

    payload: dict[str, Any] = {"message_id": str(message_id), "emoji_id": str(emoji_id or ""),
                               "set": bool(set)}
    # Explicit values first: a caller that just saw the message knows its sequence better than any
    # cache does.
    if group_id not in (None, ""):
        payload["group_id"] = group_id
    if user_id not in (None, ""):
        payload["user_id"] = user_id
    if message_seq not in (None, ""):
        payload["message_seq"] = str(message_seq)

    if not {"group_id", "user_id"} & set(payload):
        payload.update(_from_cache(ctx, message_id))

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
