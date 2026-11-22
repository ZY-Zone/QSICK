"""OneBot adapter for `mark_group_msg_as_read`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `mark_group_msg_as_read` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class MarkGroupMsgAsReadAction(PayloadAction):
    op = "mark_group_msg_as_read"
