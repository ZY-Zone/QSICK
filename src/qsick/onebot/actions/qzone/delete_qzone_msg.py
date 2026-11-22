"""OneBot adapter for `delete_qzone_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `delete_qzone_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DeleteQzoneMsgAction(PayloadAction):
    op = "delete_qzone_msg"
