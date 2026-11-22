"""改空间资料（`set_qzone_profile`）。"""
from __future__ import annotations

from typing import Any

from ._common import QzoneAction, client


class SetQzoneProfileAction(QzoneAction):
    async def handle(self, ctx: Any, name: str = "", description: str = "", **_ignored: Any) -> Any:
        return await client(ctx).set_profile(name=str(name or ""), description=str(description or ""))
