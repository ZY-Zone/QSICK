"""获取群文件地址."""

from __future__ import annotations

from typing import Any

from qsick.onebot import models
from qsick.onebot.actions.base import (
    Action,
    ActionError,
    call,
    chat,
    forward_node_json,
    media_info,
    member_json,
    message_json,
    message_json_payload,
    message_to_elements,
    send_message,
)
from qsick.protocol import elements as E


async def _handle(
    ctx: Any, group_id: int, file_id: str = "", busid: int = 0,
    url_type: int | None = None, file_name: str = "", **_: Any
) -> dict[str, Any]:
    """Ask OIDB for the download URL of one group file.

    `media.url` cannot serve this: it resolves ids through the media cache the *receive* path builds, so it answers
    nothing for a file that was stored earlier. `group.file.download` takes the group and the file id directly and
    needs no cache.

    `file_id` here is QQ's `fileUuid` -- the value an incoming file element carries as `file_id`, including its
    leading slash. The old `busid` argument is accepted and ignored: the OIDB request has its own fixed bus id, and
    silently forwarding a caller's value would change which server answers.
    """
    params: dict[str, Any] = {"group_id": group_id, "file_id": file_id}
    if url_type is not None:
        params["url_type"] = url_type
    if file_name:
        # Ends up as `?fname=`, which is the name the file is saved under.
        params["file_name"] = file_name
    data = await call(ctx, "group.file.download", params, "file url")
    url = ""
    if isinstance(data, dict):
        url = data.get("url") or data.get("download_url") or ""
    return {"url": str(url), "file_id": file_id}


class GetGroupFileUrlAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
