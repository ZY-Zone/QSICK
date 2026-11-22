"""空友爱看顶/踩（up 控制）（`set_qzone_public_upvote`）。

走同一条 trpc 网关，只是路由与 body 不同。本账号各项 `public_qzone_*` 开关为 false、
`GetPublicRecomFeeds` 回 total 0 —— 即在白名单之外，所以这些调用可达但没有对象可操作，
返回什么就如实给出什么。
"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client, resolve_ref


class SetQzonePublicUpvoteAction(QzoneAction):
    async def handle(self, ctx: Any, **params: Any) -> Any:
        qzone = client(ctx)
        kind = "public_upvote"
        ref = await resolve_ref(ctx, params)
        up = params.get("up")
        up = True if up is None else bool(up)
        return await qzone.public_call("public_upvote" if up else "public_downvote", ref=ref)
