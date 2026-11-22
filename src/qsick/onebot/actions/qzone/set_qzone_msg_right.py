"""OneBot adapter for `set_qzone_msg_right`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_qzone_msg_right` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetQzoneMsgRightAction(PayloadAction):
    op = "set_qzone_msg_right"
