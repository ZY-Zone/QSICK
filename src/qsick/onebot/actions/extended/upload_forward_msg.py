"""OneBot adapter for `upload_forward_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `upload_forward_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class UploadForwardMsgAction(PayloadAction):
    op = "upload_forward_msg"
