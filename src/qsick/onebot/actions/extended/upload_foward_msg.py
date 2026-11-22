"""OneBot adapter for `upload_foward_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `upload_foward_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class UploadFowardMsgAction(PayloadAction):
    op = "upload_foward_msg"
