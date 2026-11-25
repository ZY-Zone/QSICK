"""创建群文件夹。"""

from __future__ import annotations

import asyncio
from typing import Any

from qsick.onebot.actions.base import Action, call
from qsick.onebot import models
from qsick.onebot.registry import ActionError

#: How long to keep looking for the new folder in the listing after the service call reports no ID.
#:
#: Measured on a live group, three times: the folder showed up in the file listing **6.1 s**, **16.8 s**
#: and **more than 30 s** after creation. The lag is not a constant and not even bounded by 30 s, which
#: makes a read-back inherently unreliable -- see 实测说明.md. 60 s is the operator's chosen ceiling:
#: long enough for the slower observations, and it only runs when the service returned no ID at all,
#: so an ordinary create never waits.
LOOKUP_ATTEMPTS = 48
LOOKUP_DELAY = 1.25


async def _folders(ctx: Any, group_id: int) -> dict[str, str]:
    """`folder name -> folder id` for the group root. Empty on a failed listing."""
    try:
        data = await call(ctx, "get_group_files",
                          {"group_id": group_id, "parent_folder_id": "/"},
                          "list group files")
    except Exception:  # noqa: BLE001 - a failed listing is "not visible", not a hard error
        return {}
    found: dict[str, str] = {}
    for entry in (data.get("files") or []):
        if not isinstance(entry, dict) or not entry.get("is_folder"):
            continue
        name = str(entry.get("file_name") or "")
        folder_id = str(entry.get("file_id") or "")
        if name and folder_id:
            found.setdefault(name, folder_id)
    return found


class CreateGroupFileFolderAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        group_id = params["group_id"]
        name = params["name"]
        # Read the folder list *before* creating: measured, the service creates the folder but its
        # response carries no ID, and without this "before" snapshot a folder that already existed
        # under the same name is indistinguishable from the one just created.
        before = await _folders(ctx, group_id)
        data = await call(ctx, "group.folder.create",
                          {"group_id": group_id, "folder_name": name},
                          "create folder")
        folder_id = data.get("folder_id")
        if isinstance(folder_id, str) and folder_id.strip():
            return {"folder_id": folder_id}
        if name in before:
            # The name was already taken, so creation could not have produced a *new* folder; the
            # service call carries no ID, and returning the existing one would claim ownership of
            # something this call did not create.
            raise ActionError(
                f"create folder: a folder named {name!r} already exists (id {before[name]!r}) and the "
                "service returned no ID for this call; inspect the group files",
                models.UPSTREAM)
        for attempt in range(LOOKUP_ATTEMPTS):
            if attempt:
                await asyncio.sleep(LOOKUP_DELAY)
            now = await _folders(ctx, group_id)
            if name in now:
                return {"folder_id": now[name]}
        raise ActionError(
            f"create folder: the service accepted {name!r} but it did not appear in the group file "
            "listing; the folder may exist but cannot be identified, inspect the group files",
            models.UPSTREAM)
