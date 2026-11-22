"""OneBot adapter for `forward_friend_single_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `forward_friend_single_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class ForwardFriendSingleMsgAction(PayloadAction):
    op = "forward_friend_single_msg"
