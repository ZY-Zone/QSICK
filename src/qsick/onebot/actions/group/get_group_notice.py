"""OneBot adapter for `_get_group_notice`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `_get_group_notice` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGroupNoticeAction(PayloadAction):
    op = "_get_group_notice"
