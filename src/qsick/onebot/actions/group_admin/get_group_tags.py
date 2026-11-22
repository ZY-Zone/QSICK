"""OneBot adapter for `get_group_tags`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_group_tags` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGroupTagsAction(PayloadAction):
    op = "get_group_tags"
