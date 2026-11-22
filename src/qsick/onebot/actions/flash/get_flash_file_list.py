"""获取闪传文件集列表"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class GetFlashFileListAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "flash.list", {"count": params.get("count", 20)}, "flash list")
        return {"filesets": data.get("filesets", [])}
