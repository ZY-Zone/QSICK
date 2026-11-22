"""OneBot adapter for `send_online_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `send_online_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class SendOnlineFileAction(PayloadAction):
    op = "send_online_file"
