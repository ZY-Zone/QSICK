"""OneBot adapter for `get_qzone_msg_detail`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_qzone_msg_detail` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetQzoneMsgDetailAction(PayloadAction):
    op = "get_qzone_msg_detail"
