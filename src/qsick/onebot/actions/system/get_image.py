"""获取图片 (OneBot v11).

The standard returns a local file path. QSICK asks the host to download the
element, which confirms through QQ's own completion event; when the caller only
supplies a bare file identifier there is no element to download, so the response
falls back to a URL with an explicit note instead of a fabricated path.
"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, ActionError, call, media_info

WHERE = "get image"
KIND = "image"


async def _handle(
    ctx: Any,
    file: str = "",
    file_id: str = "",
    message_id: Any = None,
    element_id: str = "",
    chat_type: str = "",
    chat_id: Any = None,
    **_: Any,
) -> dict[str, Any]:
    identifier = str(file_id or file or "").strip()
    if message_id not in (None, "") and element_id and chat_type in ("group", "private") and chat_id not in (None, ""):
        try:
            data = await call(
                ctx,
                "media.download",
                {
                    "message_id": str(message_id),
                    "element_id": element_id,
                    "chat": {"type": chat_type, "id": str(chat_id)},
                    "file_path": "",
                },
                WHERE,
            )
            return {"file": data["file_path"], "file_path": data["file_path"], "file_size": data.get("file_size", 0)}
        except ActionError:
            # A failed download is reported honestly: no invented local path.
            raise
    if not identifier:
        raise ActionError(f"{WHERE}: requires file/file_id, or message_id+element_id+chat")
    info = await media_info(ctx, KIND, identifier)
    return {**info, "note": "remote URL only; no local element was downloaded"}


class GetImageAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
