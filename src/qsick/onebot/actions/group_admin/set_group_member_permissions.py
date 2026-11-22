"""OneBot adapter for `set_group_member_permissions`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_group_member_permissions` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetGroupMemberPermissionsAction(PayloadAction):
    op = "set_group_member_permissions"
