"""OneBot adapter for `get_doubt_friends_add_request`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_doubt_friends_add_request` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetDoubtFriendsAddRequestAction(PayloadAction):
    op = "get_doubt_friends_add_request"
