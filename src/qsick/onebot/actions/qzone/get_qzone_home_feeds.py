"""按 QQ 号取说说列表（`get_qzone_home_feeds`）。"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client


class GetQzoneHomeFeedsAction(QzoneAction):
    async def handle(self, ctx: Any, target_uin: str = "", count: Any = 10, pos: Any = 0,
                     cursor: str = "", **_ignored: Any) -> Any:
        return await client(ctx).shuoshuo_list(uin=str(target_uin or ""), count=int(count or 10) or 10,
                                               pos=int(pos or 0), cursor=str(cursor or ""))
