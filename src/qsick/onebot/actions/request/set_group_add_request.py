"""处理群请求（同意 / 拒绝 / 忽略）.

OneBot 的 `flag` 就是内核通知对象的 `seq`，审批时原样回传。它**不在**入群申请的事件里 ——
事件只有群号、申请人 uid 与附言，所以要先 `get_group_notices` 查一次拿到 `flag`，再调这里。

本次修复的两处：

* 老实现调 `request.group`，而 payload 里没有这个 op —— 每次调用都回
  `Unsupported operation: request.group`。这与 `download_file`、`oidb_call` 是同一类缺陷：
  动作声明了一个不存在的 op，"已实现"只体现在目录里。
* `type` 在老实现里是必填的字符串，可它只用来区分 add / invite；内核的 `operateSysNotify`
  要的是通知的类型码。现在把它当可选的数值透传，缺省 0 由内核按 `seq` 判定。
"""

from __future__ import annotations

from typing import Any

from qsick.onebot import models
from qsick.onebot.actions.base import Action, ActionError, call

#: OneBot 的 `sub_type` / `type` -> 内核操作。`NTGroupRequestOperateTypes`：1 同意 / 2 拒绝 / 3 忽略。
_ACTIONS = {"agree": "agree", "approve": "agree", "refuse": "refuse", "reject": "refuse",
            "ignore": "ignore"}


async def _handle(ctx: Any, flag: str = "", sub_type: str = "", approve: Any = True,
                  reason: str = "", type: Any = "", group_id: Any = None,
                  **_: Any) -> dict[str, Any]:
    if not flag:
        raise ActionError("set_group_add_request: flag is required "
                          "(read it from get_group_notices)", models.BAD_REQUEST)
    request_type = sub_type or (str(type) if isinstance(type, str) else "")
    agreed = approve in (True, "true", 1, "1")
    payload: dict[str, Any] = {"seq": str(flag), "action": _ACTIONS["agree" if agreed else "refuse"]}
    # `type` doubles as the OneBot sub-type string and the kernel's numeric notify type. A numeric
    # value is passed through; a string is not, because it means add/invite rather than a notify type.
    if isinstance(type, int) and not isinstance(type, bool):
        payload["type"] = type
    if group_id not in (None, ""):
        payload["group_code"] = str(group_id)
    if reason:
        payload["postscript"] = str(reason)
    result = await call(ctx, "group.request.operate", payload, "handle group request")
    out: dict[str, Any] = {"flag": str(flag), "action": payload["action"]}
    if request_type:
        out["sub_type"] = request_type
    if result.get("result") is not None:
        out["result"] = result["result"]
    return out


class SetGroupAddRequestAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
