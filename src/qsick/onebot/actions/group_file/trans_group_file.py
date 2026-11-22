"""OneBot adapter for `trans_group_file`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `trans_group_file` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class TransGroupFileAction(PayloadAction):
    op = "trans_group_file"
