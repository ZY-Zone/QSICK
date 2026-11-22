"""OneBot adapter for `delete_group_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `delete_group_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DeleteGroupFileAction(PayloadAction):
    op = "delete_group_file"
