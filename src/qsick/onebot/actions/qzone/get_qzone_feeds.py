"""好友动态（`get_qzone_feeds`）。

对齐 NapCat：`format` 参数存在，`native` 返回 H5 Feed 与 `feed_ref`。NapCat 的默认是 `snowluma`
（`feeds3_html_more` + HTML 解析），那条路尚未实现，所以选它时会明确说明，而不是悄悄给 native 的数据。
"""
from __future__ import annotations

from typing import Any

from ... import models
from ...registry import ActionError
from ._common import QzoneAction, client


class GetQzoneFeedsAction(QzoneAction):
    async def handle(self, ctx: Any, format: str = "native", count: Any = 10, page_num: Any = 1,
                     cursor: str = "", **_ignored: Any) -> Any:
        if str(format or "native") != "native":
            raise ActionError(
                "qzone: format=snowluma is not implemented yet (it parses feeds3_html_more HTML); "
                "use format=native, which returns the H5 feed and feed_ref", models.NOT_IMPLEMENTED)
        return await client(ctx).friend_feeds(count=int(count or 10) or 10, cursor=str(cursor or ""))
