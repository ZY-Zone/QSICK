"""OneBot adapter for `set_group_kick_members`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_group_kick_members` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetGroupKickMembersAction(PayloadAction):
    op = "set_group_kick_members"
