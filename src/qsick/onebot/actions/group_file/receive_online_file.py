"""OneBot adapter for `receive_online_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `receive_online_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class ReceiveOnlineFileAction(PayloadAction):
    op = "receive_online_file"
