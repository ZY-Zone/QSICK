"""OneBot adapter for `get_ai_record`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_ai_record` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetAiRecordAction(PayloadAction):
    op = "get_ai_record"
