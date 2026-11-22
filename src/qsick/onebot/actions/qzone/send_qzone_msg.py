"""OneBot adapter for `send_qzone_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `send_qzone_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SendQzoneMsgAction(PayloadAction):
    op = "send_qzone_msg"
