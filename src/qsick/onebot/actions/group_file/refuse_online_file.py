"""OneBot adapter for `refuse_online_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `refuse_online_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class RefuseOnlineFileAction(PayloadAction):
    op = "refuse_online_file"
