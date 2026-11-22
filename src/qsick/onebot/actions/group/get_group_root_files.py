"""获取群文件根目录"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class GetGroupRootFilesAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        data = await call(ctx, "group.files.root", {"group_id": params["group_id"]}, "group files")
        return {"files": data.get("files", []), "folders": data.get("folders", [])}
