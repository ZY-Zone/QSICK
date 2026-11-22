"""获取群文件夹内容"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class GetGroupFilesByFolderAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "group.files.folder", {"group_id": params["group_id"], "folder_id": params["folder_id"]}, "folder files")
        return {"files": data.get("files", []), "folders": data.get("folders", [])}
