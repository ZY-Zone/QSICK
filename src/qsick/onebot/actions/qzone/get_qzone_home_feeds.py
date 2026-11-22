"""某个人的说说列表（`get_qzone_home_feeds`），按 uin 取。"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client


class GetQzoneHomeFeedsAction(QzoneAction):
    async def handle(self, ctx: Any, target_uin: str = "", count: int = 10, page_num: int = 0,
                     **_ignored: Any) -> Any:
        return await client(ctx).shuoshuo_list(
            uin=str(target_uin or ""), count=int(count or 10) or 10, pos=int(page_num or 0) * (int(count or 10) or 10))
