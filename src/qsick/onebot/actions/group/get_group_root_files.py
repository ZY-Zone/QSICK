"""获取群文件根目录"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


class GetGroupRootFilesAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        # The op name is the host's, not an invented one: it declares `group.file.list`, while this used to
        # call `group.files.root`, which the host rejects as unsupported. The root folder is "/" -- an empty
        # id returns an empty listing with retCode 0, so the call looks successful while showing nothing.
        data = await call(ctx, "group.file.list",
                          {"group_id": params["group_id"], "folder_id": "/"}, "group files")
        return {"files": data.get("files", []), "folders": data.get("folders", [])}
