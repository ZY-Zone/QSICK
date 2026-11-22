"""同 `get_qzone_msg_detail`；NapCat 之外的名字，调用方用哪个都行。"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client, resolve_ref


class GetQzoneFeedDetailAction(QzoneAction):
    async def handle(self, ctx: Any, **params: Any) -> Any:
        return await client(ctx).feed_detail(await resolve_ref(ctx, params))
