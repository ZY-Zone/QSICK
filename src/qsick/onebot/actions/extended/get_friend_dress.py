"""OneBot adapter for `_get_friend_dress`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `_get_friend_dress` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetFriendDressAction(PayloadAction):
    op = "_get_friend_dress"
