"""OneBot adapter for `upload_private_file`.

Core side: resolve the upload source to a path the injected client can read, then hand it to the
payload op. Payload side: send a FILE element to `chatType: 1` (see `upload_private_file` in
`headless_actions.cjs`).

This used to be a bare `PayloadAction`, which forwarded `file_uri` straight through -- and the
payload reads `file_path`, so the only way it worked was to call it with a *non-spec* parameter
name. The Milky spec's field is `file_uri`, so a spec-shaped call failed with
"undefined must be a string without NUL". The group upload path already solved this; the two now
share the same resolution.
"""
from __future__ import annotations

import asyncio
from typing import Any

from qsick.onebot import models
from qsick.onebot.actions import base
from qsick.onebot.registry import ActionError
from qsick.onebot.upload_source import resolve_upload_source


def _is_plain_path(reference: Any) -> bool:
    """True for a bare filesystem path (no URI scheme), which the host can resolve itself."""
    value = str(reference or "")
    return bool(value) and "://" not in value


class UploadPrivateFileAction(base.PayloadAction):
    op = "upload_private_file"

    async def run(self, ctx: base.ActionContext, **params: Any) -> Any:
        reference = next((params[key] for key in ("file_path", "file_uri", "file")
                          if params.get(key) is not None), None)
        try:
            source = await asyncio.to_thread(resolve_upload_source, reference)
        except (OSError, ValueError) as exc:
            # A bare local path this process cannot see is left to the host, which resolves it
            # inside QQ -- same rule as the group upload, for the same reason: a caller used to
            # sending paths relative to the client's directory should not start failing here.
            if _is_plain_path(reference):
                return await base.call(ctx, self.op, dict(params), self.name or self.op,
                                       timeout=base.MEDIA_SEND_TIMEOUT)
            raise ActionError(f"upload_private_file: {exc}", models.BAD_REQUEST) from exc
        payload = {**params, "file_path": str(source.path), "file": str(source.path)}
        payload.pop("file_uri", None)
        # No `finally`: a transport timeout can leave QQ still reading the file, so the source is
        # only cleaned up once the call is known to have finished with it.
        result = await base.call(ctx, self.op, payload, self.name or self.op,
                                 timeout=base.MEDIA_SEND_TIMEOUT)
        if result.get("file_id") and not result.get("file_id_pending"):
            source.cleanup()
        return result
