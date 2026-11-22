"""点赞（`like_qzone`）。"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client, resolve_ref


class LikeQzoneAction(QzoneAction):
    async def handle(self, ctx: Any, **params: Any) -> Any:
        return await client(ctx).like(await resolve_ref(ctx, params))
