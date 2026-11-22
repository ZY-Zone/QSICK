"""OneBot adapter for `get_group_signed_list`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_group_signed_list` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGroupSignedListAction(PayloadAction):
    op = "get_group_signed_list"
