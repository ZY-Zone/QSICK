"""OneBot adapter for `get_group_detail_info`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_group_detail_info` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGroupDetailInfoAction(PayloadAction):
    op = "get_group_detail_info"
