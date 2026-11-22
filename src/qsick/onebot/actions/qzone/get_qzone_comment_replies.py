"""评论下的回复（`get_qzone_comment_replies`）。"""
from __future__ import annotations

from typing import Any

from ... import models
from ...registry import ActionError
from ._common import QzoneAction, client, resolve_ref


class GetQzoneCommentRepliesAction(QzoneAction):
    async def handle(self, ctx: Any, comment_id: str = "", **params: Any) -> Any:
        if not comment_id:
            raise ActionError("qzone: comment_id is required to list replies", models.BAD_REQUEST)
        return await client(ctx).replies(await resolve_ref(ctx, params), str(comment_id))
