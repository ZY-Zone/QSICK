"""OneBot adapter for `get_group_admin_permissions`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_group_admin_permissions` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGroupAdminPermissionsAction(PayloadAction):
    op = "get_group_admin_permissions"
