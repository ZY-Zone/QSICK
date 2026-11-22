"""OneBot adapter for `get_essence_msg_list`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_essence_msg_list` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetEssenceMsgListAction(PayloadAction):
    op = "get_essence_msg_list"
