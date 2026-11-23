"""评论下的回复（`get_qzone_comment_replies`）。

参数与 NapCat 一致：传**完整的父评论对象** `comment`（读详情或评论列表时拿到的那份），而不是只给 id ——
服务端续页需要评论自带的回复游标。`comment_id` 仍接受，作为只想要某一楼时的简化写法。
"""
from __future__ import annotations

from typing import Any

from ... import models
from ...registry import ActionError
from ._common import QzoneAction, client, resolve_ref


def _comment_id(params: dict[str, Any]) -> str:
    parent = params.get("comment")
    if isinstance(parent, dict):
        for key in ("id", "comment_id", "commentid"):
            value = parent.get(key)
            if value not in (None, ""):
                return str(value)
    return str(params.get("comment_id") or "").strip()


class GetQzoneCommentRepliesAction(QzoneAction):
    async def handle(self, ctx: Any, **params: Any) -> Any:
        comment_id = _comment_id(params)
        if not comment_id:
            raise ActionError(
                "qzone: pass the parent `comment` object (as returned by a read), or its `comment_id`",
                models.BAD_REQUEST)
        return await client(ctx).replies(await resolve_ref(ctx, params), comment_id)
