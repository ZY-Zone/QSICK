"""评论列表（`get_qzone_comment_list`）。"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client, resolve_ref


class GetQzoneCommentListAction(QzoneAction):
    async def handle(self, ctx: Any, attach_info: str = "", **params: Any) -> Any:
        return await client(ctx).comments(await resolve_ref(ctx, params), attach_info=str(attach_info or ""))
