"""空友爱看关注/取关（follow 控制）（`set_qzone_public_follow`）。

走同一条 trpc 网关，只是路由与 body 不同。本账号各项 `public_qzone_*` 开关为 false、
`GetPublicRecomFeeds` 回 total 0 —— 即在白名单之外，所以这些调用可达但没有对象可操作，
返回什么就如实给出什么。
"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client, resolve_ref


class SetQzonePublicFollowAction(QzoneAction):
    async def handle(self, ctx: Any, **params: Any) -> Any:
        qzone = client(ctx)
        kind = "public_follow"
        ref = params.get("feed_ref") if isinstance(params.get("feed_ref"), dict) else None
        follow = params.get("follow")
        follow = True if follow is None else bool(follow)
        target = str(params.get("target_uin") or "")
        return await qzone.public_call(
            "public_follow" if follow else "public_unfollow",
            extra={"uin": target} if target else None,
        )
