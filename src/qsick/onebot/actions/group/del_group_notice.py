"""OneBot adapter for `_del_group_notice`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `_del_group_notice` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DelGroupNoticeAction(PayloadAction):
    op = "_del_group_notice"
