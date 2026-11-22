"""OneBot adapter for `upload_file_stream`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `upload_file_stream` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class UploadFileStreamAction(PayloadAction):
    op = "upload_file_stream"
