"""评论（`comment_qzone`）。"""
from __future__ import annotations

from typing import Any

from ... import models
from ...registry import ActionError
from ._common import QzoneAction, client, resolve_ref


class CommentQzoneAction(QzoneAction):
    async def handle(self, ctx: Any, content: str = "", **params: Any) -> Any:
        text = str(content or "").strip()
        if not text:
            raise ActionError("qzone: content cannot be empty", models.BAD_REQUEST)
        return await client(ctx).add_comment(await resolve_ref(ctx, params), text)
