"""说说列表（`get_qzone_msg_list`）。`msglist` 这个键名与 NapCat 一致。"""
from __future__ import annotations

from typing import Any

from ... import models
from ...registry import ActionError
from ._common import QzoneAction, client


class GetQzoneMsgListAction(QzoneAction):
    async def handle(self, ctx: Any, format: str = "native", target_uin: str = "", pos: Any = 0,
                     num: Any = 10, cursor: str = "", **_ignored: Any) -> Any:
        if str(format or "native") != "native":
            raise ActionError(
                "qzone: format=snowluma is not implemented yet (it parses feeds3_html_more HTML); "
                "use format=native", models.NOT_IMPLEMENTED)
        return await client(ctx).shuoshuo_list(uin=str(target_uin or ""), count=int(num or 10) or 10,
                                               pos=int(pos or 0), cursor=str(cursor or ""))
