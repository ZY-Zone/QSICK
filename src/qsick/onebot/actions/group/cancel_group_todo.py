"""OneBot adapter for `cancel_group_todo`.

Core side: parameter validation + retcode mapping.
Payload side: implement the `cancel_group_todo` op (see docs/payload-ops.md).
"""

from __future__ import annotations

from qsick.onebot.actions.base import PayloadAction


class CancelGroupTodoAction(PayloadAction):
    op = "cancel_group_todo"
