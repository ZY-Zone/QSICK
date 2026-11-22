"""OneBot adapter for `get_group_ignored_notifies`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `get_group_ignored_notifies` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class GetGroupIgnoredNotifiesAction(PayloadAction):
    op = "get_group_ignored_notifies"
