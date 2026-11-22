"""OneBot adapter for `forward_group_single_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `forward_group_single_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class ForwardGroupSingleMsgAction(PayloadAction):
    op = "forward_group_single_msg"
