"""OneBot adapter for `delete_essence_msg`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `delete_essence_msg` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DeleteEssenceMsgAction(PayloadAction):
    op = "delete_essence_msg"
