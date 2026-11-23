"""单条说说 + 评论（`get_qzone_msg_detail`）。形状同 NapCat 的 `QzoneDetailReturnSchema`。"""
from __future__ import annotations

from typing import Any

from ... import models
from ...registry import ActionError
from ._common import QzoneAction, client, resolve_ref


class GetQzoneMsgDetailAction(QzoneAction):
    async def handle(self, ctx: Any, format: str = "native", cursor: str = "", **params: Any) -> Any:
        if str(format or "native") != "native":
            raise ActionError(
                "qzone: format=snowluma is not implemented yet; use format=native", models.NOT_IMPLEMENTED)
        return await client(ctx).feed_detail(await resolve_ref(ctx, params), cursor=str(cursor or ""))
