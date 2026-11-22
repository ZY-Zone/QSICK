"""创建群文件夹"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class CreateGroupFileFolderAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "group.files.create_folder", {"group_id": params["group_id"], "name": params["name"]}, "create folder")
        return {"folder_id": str(data.get("folder_id", ""))}
