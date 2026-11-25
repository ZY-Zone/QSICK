"""OneBot adapter for `upload_group_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `upload_group_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

import asyncio
from typing import Any

from qsick.onebot.actions import base
from qsick.onebot.context import ActionContext
from qsick.onebot.registry import ActionError
from qsick.onebot.upload_source import resolve_upload_source


class UploadGroupFileAction(base.PayloadAction):
    op = "upload_group_file"

    async def run(self, ctx: ActionContext, **params: Any) -> Any:
        reference = next((params[key] for key in ("file_path", "file_uri", "file")
                          if params.get(key) is not None), None)
        try:
            source = await asyncio.to_thread(resolve_upload_source, reference)
        except (OSError, ValueError) as exc:
            raise ActionError(f"upload_group_file: {exc}") from exc
        payload = {**params, "file_path": str(source.path), "file": str(source.path)}
        payload.pop("file_uri", None)
        # Never use finally here: transport timeouts/cancellation or a payload
        # failure can leave QQ still reading. Retain our file in temp/uploads
        # for explicit cache cleanup when completion cannot be established.
        result = await base.call(ctx, self.op, payload, self.name or self.op,
                                 timeout=base.MEDIA_SEND_TIMEOUT)
        # An accepted upload with file_id_pending is not a completed transfer.
        # Keep the source until explicit cache cleanup instead of racing QQ's reader.
        if result.get("file_id") and not result.get("file_id_pending"):
            source.cleanup()
        return result
