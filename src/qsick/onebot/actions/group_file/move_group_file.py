"""OneBot adapter for `move_group_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `move_group_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class MoveGroupFileAction(PayloadAction):
    op = "move_group_file"
