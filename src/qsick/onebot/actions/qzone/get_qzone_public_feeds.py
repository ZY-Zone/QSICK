"""空友爱看推荐流（`get_qzone_public_feeds`）。"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client


class GetQzonePublicFeedsAction(QzoneAction):
    async def handle(self, ctx: Any, count: int = 10, **_ignored: Any) -> Any:
        return await client(ctx).public_feeds(count=int(count or 10) or 10)
