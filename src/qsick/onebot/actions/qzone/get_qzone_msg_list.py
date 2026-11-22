"""说说列表（`get_qzone_msg_list`），NapCat 的核心读操作。"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client


class GetQzoneMsgListAction(QzoneAction):
    async def handle(self, ctx: Any, target_uin: str = "", pos: int = 0, num: int = 10,
                     **_ignored: Any) -> Any:
        return await client(ctx).shuoshuo_list(
            uin=str(target_uin or ""), count=int(num or 10) or 10, pos=int(pos or 0))
