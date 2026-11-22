"""OneBot adapter for `mark_private_msg_as_read`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `mark_private_msg_as_read` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class MarkPrivateMsgAsReadAction(PayloadAction):
    op = "mark_private_msg_as_read"
