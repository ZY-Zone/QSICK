"""OneBot adapter for `set_group_member_invite_policy`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_group_member_invite_policy` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetGroupMemberInvitePolicyAction(PayloadAction):
    op = "set_group_member_invite_policy"
