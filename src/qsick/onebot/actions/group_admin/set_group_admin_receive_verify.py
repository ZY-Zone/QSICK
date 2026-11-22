"""OneBot adapter for `set_group_admin_receive_verify`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_group_admin_receive_verify` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetGroupAdminReceiveVerifyAction(PayloadAction):
    op = "set_group_admin_receive_verify"
