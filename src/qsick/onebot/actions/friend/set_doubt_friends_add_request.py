"""OneBot adapter for `set_doubt_friends_add_request`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_doubt_friends_add_request` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetDoubtFriendsAddRequestAction(PayloadAction):
    op = "set_doubt_friends_add_request"
