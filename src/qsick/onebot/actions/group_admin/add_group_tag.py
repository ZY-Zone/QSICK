"""OneBot adapter for `add_group_tag`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `add_group_tag` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class AddGroupTagAction(PayloadAction):
    op = "add_group_tag"
