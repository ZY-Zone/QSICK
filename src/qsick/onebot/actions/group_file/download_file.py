"""OneBot adapter for `download_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `download_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DownloadFileAction(PayloadAction):
    op = "download_file"
