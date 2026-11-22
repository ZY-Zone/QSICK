"""OneBot adapter for `send_group_ai_record`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `send_group_ai_record` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SendGroupAiRecordAction(PayloadAction):
    op = "send_group_ai_record"
