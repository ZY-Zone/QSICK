"""OneBot adapter for `download_file_record_stream`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `download_file_record_stream` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DownloadFileRecordStreamAction(PayloadAction):
    op = "download_file_record_stream"
