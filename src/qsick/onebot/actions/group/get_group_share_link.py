"""OneBot adapter for `get_group_share_link`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_group_share_link` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGroupShareLinkAction(PayloadAction):
    op = "get_group_share_link"
