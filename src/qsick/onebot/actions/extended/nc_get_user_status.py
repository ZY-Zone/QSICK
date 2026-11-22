"""OneBot adapter for `nc_get_user_status`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `nc_get_user_status` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class NcGetUserStatusAction(PayloadAction):
    op = "nc_get_user_status"
