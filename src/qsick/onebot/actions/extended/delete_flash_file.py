"""删除闪传文件"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class DeleteFlashFileAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        await call(ctx, "flash.delete", {"fileset_id": params["fileset_id"], "file_id": params.get("file_id", "")}, "flash delete")
        return None
