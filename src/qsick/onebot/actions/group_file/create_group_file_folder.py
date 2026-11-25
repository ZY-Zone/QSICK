"""创建群文件夹"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call
from qsick.onebot import models
from qsick.onebot.registry import ActionError


class CreateGroupFileFolderAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "group.folder.create",
                          {"group_id": params["group_id"], "folder_name": params["name"]},
                          "create folder")
        folder_id = data.get("folder_id")
        if not isinstance(folder_id, str) or not folder_id.strip():
            raise ActionError(
                "create folder: response has no reliable folder_id; the folder may already "
                "exist, inspect the group files before retrying", models.UPSTREAM)
        return {"folder_id": folder_id}
