"""OneBot adapter for `_send_group_notice`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `_send_group_notice` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SendGroupNoticeAction(PayloadAction):
    op = "_send_group_notice"
