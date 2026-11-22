"""OneBot adapter for `set_diy_online_status`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `set_diy_online_status` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SetDiyOnlineStatusAction(PayloadAction):
    op = "set_diy_online_status"
