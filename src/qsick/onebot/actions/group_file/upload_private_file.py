"""OneBot adapter for `upload_private_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `upload_private_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class UploadPrivateFileAction(PayloadAction):
    op = "upload_private_file"
