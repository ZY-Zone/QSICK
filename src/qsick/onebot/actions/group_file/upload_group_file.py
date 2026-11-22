"""OneBot adapter for `upload_group_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `upload_group_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class UploadGroupFileAction(PayloadAction):
    op = "upload_group_file"
