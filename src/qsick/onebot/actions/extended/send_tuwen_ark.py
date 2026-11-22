"""OneBot adapter for `send_tuwen_ark`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `send_tuwen_ark` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SendTuwenArkAction(PayloadAction):
    op = "send_tuwen_ark"
