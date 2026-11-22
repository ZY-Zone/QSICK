"""重命名闪传文件"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class RenameFlashFileAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        await call(ctx, "flash.rename", {"fileset_id": params["fileset_id"], "file_id": params["file_id"], "name": params["name"]}, "flash rename")
        return None
