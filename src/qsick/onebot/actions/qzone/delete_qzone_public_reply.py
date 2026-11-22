"""OneBot adapter for `delete_qzone_public_reply`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `delete_qzone_public_reply` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DeleteQzonePublicReplyAction(PayloadAction):
    op = "delete_qzone_public_reply"
