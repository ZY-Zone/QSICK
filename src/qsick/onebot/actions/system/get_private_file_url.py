"""获取私聊文件下载地址."""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, call


async def _handle(ctx: Any, user_id: int, file_id: str = "",
                  file_hash: str = "", is_self_send: bool = False,
                  **_: Any) -> dict[str, Any]:
    """Return the file's download URL.

    Two sources, in the order the payload tries them: the URL a received message element already
    carried, then OIDB 0xE37_800. The second is not fully mapped yet -- report 15 documents the
    command number and that C2C (unlike group files) requires an md5, but not the request layout --
    so when it cannot answer, the payload fails with what it observed instead of a made-up URL.
    """
    data = await call(ctx, "private.file.download_url", {
        "user_id": user_id,
        "file_id": file_id,
        "file_hash": file_hash,
        "is_self_send": bool(is_self_send),
    }, "private file url")
    return {"url": data.get("url", "")}


class GetPrivateFileUrlAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
