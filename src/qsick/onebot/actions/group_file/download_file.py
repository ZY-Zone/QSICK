"""OneBot adapter for `download_file`: fetch a message's file element to a local path.

Core side: map the caller's parameters onto the payload's `media.download` op. Payload side: that op
already exists and calls `Msg.downloadRichMedia`, which handles group **and** private chats --
`chatType: 1` with a resolved peer uid for C2C.

This action used to declare `op = "download_file"`, which the payload does not implement, so every
call answered `Unsupported operation: download_file`. `media.download` was implemented but reachable
from no action at all: `get_file`/`get_image`/`get_record` go through `media.url`, which is
deliberately unresolved.

A private file has no download URL we can produce (the OIDB layout for `0xE37_800` is unmapped, see
`headless_privatefile.cjs`), so downloading the element is the route that actually works today.
"""
from __future__ import annotations

from typing import Any

from qsick.onebot import models
from qsick.onebot.actions.base import ActionContext, PayloadAction, call
from qsick.onebot.registry import ActionError


class DownloadFileAction(PayloadAction):
    op = "media.download"

    async def run(self, ctx: ActionContext, **params: Any) -> Any:
        chat_type = str(params.get("chat_type") or "").strip()
        chat_id = params.get("chat_id")
        # Allow the two shapes a caller may naturally reach for: an explicit chat, or the usual
        # `group_id` / `user_id` pair. The payload wants one chat object either way.
        if not chat_type or chat_id in (None, ""):
            if params.get("group_id") not in (None, ""):
                chat_type, chat_id = "group", params["group_id"]
            elif params.get("user_id") not in (None, ""):
                chat_type, chat_id = "private", params["user_id"]
        if chat_type not in ("group", "private"):
            raise ActionError("download_file: chat_type must be group or private", models.BAD_REQUEST)
        if chat_id in (None, ""):
            raise ActionError("download_file: chat_id is required", models.BAD_REQUEST)
        message_id = params.get("message_id")
        element_id = params.get("element_id")
        if message_id in (None, ""):
            raise ActionError("download_file: message_id is required", models.BAD_REQUEST)
        if element_id in (None, ""):
            # The correlation key the completion event carries; without it the payload cannot tell
            # which download finished, and it fails rather than guessing.
            raise ActionError(
                "download_file: element_id is required (read it from the message's file element)",
                models.BAD_REQUEST)
        payload = {
            "message_id": str(message_id),
            "element_id": str(element_id),
            "chat": {"type": chat_type, "id": int(chat_id)},
            # Absent means "let the client choose"; a caller that wants a specific location says so.
            **({"file_path": str(params["file_path"])} if params.get("file_path") else {}),
        }
        return await call(ctx, self.op, payload, self.name or self.op, timeout=120.0)
