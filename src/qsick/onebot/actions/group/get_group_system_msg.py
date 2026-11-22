"""OneBot adapter for `get_group_system_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_group_system_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGroupSystemMsgAction(PayloadAction):
    op = "get_group_system_msg"
