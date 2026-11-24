"""创建群文件夹"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class CreateGroupFileFolderAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        # The host's op is `group.folder.create`; this used to call `group.files.create_folder`, a name no
        # layer declares, so every call came back unsupported. The host answers with `folder_name`, not a
        # folder id -- there is nothing to invent here, so the reply is passed through as-is.
        data = await call(ctx, "group.folder.create",
                          {"group_id": params["group_id"], "folder_name": params["name"]},
                          "create folder")
        return {"folder_id": str(data.get("folder_id", data.get("folder_name", "")))}
