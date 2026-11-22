"""OneBot adapter for `delete_group_folder`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `delete_group_folder` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class DeleteGroupFolderAction(PayloadAction):
    op = "delete_group_folder"
