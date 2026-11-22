"""OneBot adapter for `send_ark_share`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `send_ark_share` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SendArkShareAction(PayloadAction):
    op = "send_ark_share"
