"""OneBot adapter for `upload_group_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `upload_group_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

from qsick import log as logmod
from qsick.onebot.actions import base
from qsick.onebot.context import ActionContext
from qsick.onebot.registry import ActionError
from qsick.onebot.upload_source import resolve_upload_source

log = logmod.get_logger("OneBot.UploadGroupFile")


class UploadGroupFileAction(base.PayloadAction):
    op = "upload_group_file"

    async def run(self, ctx: ActionContext, **params: Any) -> Any:
        reference = next((params[key] for key in ("file_path", "file_uri", "file")
                          if params.get(key) is not None), None)
        try:
            source = await asyncio.to_thread(resolve_upload_source, reference)
        except (OSError, ValueError) as exc:
            # A bare local path that is not visible from this process keeps the old
            # behaviour: hand it to the host, which resolves it inside QQ. Callers used
            # to send paths relative to the client's working directory, and failing here
            # turned uploads that used to work into an error from the wrong side.
            if _is_plain_path(reference):
                log.debug("upload source left to the host", reference=str(reference)[:120],
                          error=f"{type(exc).__name__}: {exc}")
                return await base.call(ctx, self.op, dict(params), self.name or self.op,
                                       timeout=base.MEDIA_SEND_TIMEOUT)
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


def _is_plain_path(reference: Any) -> bool:
    """True for a bare filesystem path (no URI scheme), which the host can resolve itself.

    A scheme means the caller asked for something only this side understands (`base64://`,
    `http(s)://`); those must fail here rather than be forwarded unreadable to QQ. A Windows
    drive letter is a path, not a scheme.
    """
    if not isinstance(reference, str) or not reference or "\x00" in reference:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", reference):
        return True
    return urlsplit(reference).scheme == ""
