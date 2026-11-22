"""OneBot adapter for `set_friends_category`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_friends_category` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetFriendsCategoryAction(PayloadAction):
    op = "set_friends_category"
