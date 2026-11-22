"""好友动态（`get_qzone_feeds`）。

Body shape: `GetFriendFeeds` answered `retcode 0` for `{}` as well as for plausible parameters, so the server is
tolerant and nothing here is load-bearing except the read itself.
"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client


class GetQzoneFeedsAction(QzoneAction):
    async def handle(self, ctx: Any, count: int = 10, page_num: int = 0, **_ignored: Any) -> Any:
        return await client(ctx).friend_feeds(count=int(count or 10) or 10, pos=int(page_num or 0) * (int(count or 10) or 10))
