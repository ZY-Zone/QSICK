"""OneBot adapter for `get_group_info_ex`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_group_info_ex` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGroupInfoExAction(PayloadAction):
    op = "get_group_info_ex"
