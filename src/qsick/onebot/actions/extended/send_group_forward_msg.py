"""OneBot adapter for `send_group_forward_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `send_group_forward_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SendGroupForwardMsgAction(PayloadAction):
    op = "send_group_forward_msg"
