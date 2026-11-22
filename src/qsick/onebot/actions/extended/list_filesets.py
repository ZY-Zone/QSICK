"""OneBot adapter for `list_filesets`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `list_filesets` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class ListFilesetsAction(PayloadAction):
    op = "list_filesets"
