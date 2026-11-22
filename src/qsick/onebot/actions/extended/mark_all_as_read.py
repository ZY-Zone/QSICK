"""OneBot adapter for `_mark_all_as_read`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `_mark_all_as_read` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class MarkAllAsReadAction(PayloadAction):
    op = "_mark_all_as_read"
