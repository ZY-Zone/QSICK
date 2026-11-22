"""OneBot adapter for `rename_group_file_folder`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `rename_group_file_folder` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class RenameGroupFileFolderAction(PayloadAction):
    op = "rename_group_file_folder"
