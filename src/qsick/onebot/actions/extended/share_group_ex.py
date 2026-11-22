"""OneBot adapter for `share_group_ex`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `share_group_ex` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class ShareGroupExAction(PayloadAction):
    op = "share_group_ex"
