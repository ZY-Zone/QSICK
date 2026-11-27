"""获取待处理的群通知（入群申请、邀请、管理员变动）.

这是**唯一**能拿到两样东西的地方：

* 申请人的 uid —— `flag` 之外的另一个必需信息。OLPush 帧里只有 uid，而申请人通常不在任何我们
  在的群里，所以 uid→uin 表无法解析；这就是事件流里 `user_id` 曾经是 0 的原因。
* `flag`（内核叫 `seq`）—— 审批时必须回传的不透明字符串。

两者都来自内核的通知列表，不是从消息事件里推出来的。
"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


def _row(raw: dict[str, Any]) -> dict[str, Any]:
    """把 payload 的一行整理成调用方需要的形状，不猜缺失的字段。"""
    return {
        "flag": raw.get("flag", ""),
        "type": raw.get("type", 0),
        "status": raw.get("status", 0),
        "group_id": raw.get("group_id", 0),
        "group_name": raw.get("group_name", ""),
        # OneBot 的 `user_id` 是 QQ 号；这里给的是 uid，因为申请人的号还解析不出来。
        # 名字里带 _uid 就是为了不让调用方把它当成 QQ 号用。
        "user_id_uid": raw.get("user_id_uid", ""),
        "nickname": raw.get("nickname", ""),
        "inviter_uid": raw.get("inviter_uid", ""),
        "inviter_nickname": raw.get("inviter_nickname", ""),
        "comment": raw.get("comment", ""),
        "action_time": raw.get("action_time", ""),
    }


async def _handle(ctx: Any, count: Any = None, timeout_ms: Any = None,
                  **_: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if count not in (None, ""):
        params["count"] = int(count)
    if timeout_ms not in (None, ""):
        params["timeout_ms"] = int(timeout_ms)
    data = await call(ctx, "group.notices", params, "group notices")
    notifies = [_row(item) for item in (data.get("notifies") or []) if isinstance(item, dict)]
    return {
        "count": len(notifies),
        "doubt": bool(data.get("doubt")),
        "notifies": notifies,
    }


class GetGroupNoticesAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
