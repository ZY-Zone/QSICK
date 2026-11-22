"""OneBot adapter for `get_online_file_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_online_file_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetOnlineFileMsgAction(PayloadAction):
    op = "get_online_file_msg"
