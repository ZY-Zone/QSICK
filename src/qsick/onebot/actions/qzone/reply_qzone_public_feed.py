"""空友爱看回复评论（`reply_qzone_public_feed`）。

走同一条 trpc 网关，只是路由与 body 不同。本账号各项 `public_qzone_*` 开关为 false、
`GetPublicRecomFeeds` 回 total 0 —— 即在白名单之外，所以这些调用可达但没有对象可操作，
返回什么就如实给出什么。
"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client, resolve_ref


class ReplyQzonePublicFeedAction(QzoneAction):
    async def handle(self, ctx: Any, **params: Any) -> Any:
        qzone = client(ctx)
        kind = "public_reply"
        ref = await resolve_ref(ctx, params)
        text = str(params.get("content") or "").strip()
        parent = params.get("comment") if isinstance(params.get("comment"), dict) else None
        return await qzone.add_comment(ref, text, parent=parent)
