"""获取语音 (OneBot v11).

Same contract as get_image: a local file path when the element can be
downloaded, otherwise a URL plus an explicit note. ``out_format`` is accepted and
echoed, but transcoding is not implemented, so a requested conversion is refused
rather than silently returning an unconverted file.
"""

from __future__ import annotations

from typing import Any

from qsick.onebot.actions.base import Action, ActionError, call, media_info

WHERE = "get record"
KIND = "record"


async def _handle(
    ctx: Any,
    file: str = "",
    file_id: str = "",
    out_format: str = "",
    message_id: Any = None,
    element_id: str = "",
    chat_type: str = "",
    chat_id: Any = None,
    **_: Any,
) -> dict[str, Any]:
    if out_format and out_format not in ("mp3", "amr", "wav", "silk", "flac", "ogg"):
        raise ActionError(f"{WHERE}: unsupported out_format {out_format!r}")
    identifier = str(file_id or file or "").strip()
    if message_id not in (None, "") and element_id and chat_type in ("group", "private") and chat_id not in (None, ""):
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
        result = {"file": data["file_path"], "file_path": data["file_path"], "file_size": data.get("file_size", 0)}
        if out_format:
            result["note"] = f"native audio is returned as-is; {out_format} transcoding is not implemented"
        return result
    if not identifier:
        raise ActionError(f"{WHERE}: requires file/file_id, or message_id+element_id+chat")
    info = await media_info(ctx, KIND, identifier)
    return {**info, "note": "remote URL only; no local element was downloaded"}


class GetRecordAction(Action):
    async def run(self, ctx: Any, **params: Any) -> Any:
        return await _handle(ctx, **params)
