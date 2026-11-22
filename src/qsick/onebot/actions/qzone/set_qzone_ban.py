"""空间黑名单（`set_qzone_ban`）。"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client


class SetQzoneBanAction(QzoneAction):
    async def handle(self, ctx: Any, target_uin: str = "", banned: bool = True, **_ignored: Any) -> Any:
        return await client(ctx).set_ban(str(target_uin or ""), banned=bool(banned))
