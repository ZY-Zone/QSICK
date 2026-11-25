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
        try:
            result = await base.call(ctx, self.op, payload, self.name or self.op,
                                     timeout=base.MEDIA_SEND_TIMEOUT)
        except (ActionError, TimeoutError) as exc:
            # Measured: the upload completed but the payload call timed out, so the caller was told
            # "failed" for a file that is now in the group -- and a retry uploaded it twice. The
            # listing is the objective signal, so look for our file by name before deciding.
            recovered = await _find_uploaded(ctx, params, source.path.name)
            if recovered:
                log.debug("upload confirmed by listing after an uncertain call",
                          error=f"{type(exc).__name__}: {exc}", file_id=recovered[:40])
                source.cleanup()
                return {"file_id": recovered}
            raise
        # An accepted upload with file_id_pending is not a completed transfer.
        # Keep the source until explicit cache cleanup instead of racing QQ's reader.
        if result.get("file_id") and not result.get("file_id_pending"):
            source.cleanup()
        return result


#: How long to keep looking for the uploaded file when the call's own result was uncertain.
#:
#: Same measurement as the folder case: an object can take more than 30 s to appear in the group file
#: listing, so a short window reports a failure for a file that is already there. 60 s matches the
#: ceiling the operator picked for the folder lookup.
LOOKUP_ATTEMPTS = 40
LOOKUP_DELAY = 1.5


async def _find_uploaded(ctx: ActionContext, params: dict[str, Any], local_name: str) -> str:
    """The ID of a file we just uploaded, read back from the group listing."""
    group_id = params.get("group_id")
    if group_id is None:
        return ""
    folder = str(params.get("parent_folder_id") or "/")
    wanted = str(params.get("name") or params.get("file_name") or local_name or "")
    if not wanted:
        return ""
    for attempt in range(LOOKUP_ATTEMPTS):
        if attempt:
            await asyncio.sleep(LOOKUP_DELAY)
        try:
            data = await base.call(ctx, "get_group_files",
                                   {"group_id": group_id, "parent_folder_id": folder},
                                   "list group files")
        except Exception:  # noqa: BLE001 - not visible yet is not a hard error
            continue
        for entry in (data.get("files") or []):
            if not isinstance(entry, dict) or entry.get("is_folder"):
                continue
            if str(entry.get("file_name") or "") != wanted:
                continue
            file_id = str(entry.get("file_id") or "")
            if file_id:
                return file_id
    return ""


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
