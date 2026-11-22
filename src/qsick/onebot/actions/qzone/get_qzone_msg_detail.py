"""单条说说（`get_qzone_msg_detail`）。"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client, resolve_ref


class GetQzoneMsgDetailAction(QzoneAction):
    async def handle(self, ctx: Any, **params: Any) -> Any:
        return await client(ctx).feed_detail(await resolve_ref(ctx, params))
