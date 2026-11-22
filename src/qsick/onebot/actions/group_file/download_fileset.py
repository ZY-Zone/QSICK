"""OneBot adapter for `download_fileset`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `download_fileset` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DownloadFilesetAction(PayloadAction):
    op = "download_fileset"
