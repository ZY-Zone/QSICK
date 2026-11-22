"""OneBot adapter for `download_file_stream`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `download_file_stream` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DownloadFileStreamAction(PayloadAction):
    op = "download_file_stream"
