"""OneBot adapter for `set_group_admin_permissions`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_group_admin_permissions` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetGroupAdminPermissionsAction(PayloadAction):
    op = "set_group_admin_permissions"
