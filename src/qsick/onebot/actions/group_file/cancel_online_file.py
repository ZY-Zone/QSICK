"""OneBot adapter for `cancel_online_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `cancel_online_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class CancelOnlineFileAction(PayloadAction):
    op = "cancel_online_file"
