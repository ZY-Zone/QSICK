"""OneBot adapter for `send_flash_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `send_flash_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SendFlashMsgAction(PayloadAction):
    op = "send_flash_msg"
