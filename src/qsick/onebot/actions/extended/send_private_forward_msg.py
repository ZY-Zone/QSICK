"""OneBot adapter for `send_private_forward_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `send_private_forward_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SendPrivateForwardMsgAction(PayloadAction):
    op = "send_private_forward_msg"
